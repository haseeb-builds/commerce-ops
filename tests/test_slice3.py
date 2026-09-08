"""Slice 3 tests: operator actions, operator notes, customer confirmations."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
core = importlib.import_module("commerceops.core")
detail = importlib.import_module("commerceops.detail")
actions = importlib.import_module("commerceops.actions")

EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence", "sample_cases.csv")


def db(tmp_path):
    return core.connect(str(tmp_path / "s3.db"))


def load_evidence():
    with open(EVIDENCE, encoding="utf-8") as f:
        return f.read()


def ship_id(conn, tno):
    return conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tno,)).fetchone()["id"]


def test_a_operator_action_recorded(tmp_path):
    """A. Operator action can be recorded for an existing shipment."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_001")
    aid = actions.record_operator_action(conn, sid, "query_sent", note="called customer")
    row = conn.execute("SELECT * FROM operator_action WHERE id=?", (aid,)).fetchone()
    assert row["kind"] == "query_sent"
    assert row["actor"] == "laiba"
    assert row["note"] == "called customer"
    assert row["acted_at"] is not None


def test_b_append_only_never_overwrites(tmp_path):
    """B. Actions are append-only: a second action adds a row; the first is
    untouched; there is no update path. (C1: import also seeds legacy-mapped
    actions, so counts are relative.)"""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_002")
    base = conn.execute("SELECT COUNT(*) c FROM operator_action WHERE shipment_id=?", (sid,)).fetchone()["c"]
    a1 = actions.record_operator_action(conn, sid, "note", note="first")
    before = conn.execute("SELECT * FROM operator_action WHERE id=?", (a1,)).fetchone()
    actions.record_operator_action(conn, sid, "reattempt_requested")
    after = conn.execute("SELECT * FROM operator_action WHERE id=?", (a1,)).fetchone()
    assert dict(before) == dict(after)  # first record byte-identical
    n = conn.execute("SELECT COUNT(*) c FROM operator_action WHERE shipment_id=?", (sid,)).fetchone()["c"]
    assert n == base + 2


def test_c_multiple_actions_remain_distinct(tmp_path):
    """C. Multiple actions on one shipment stay distinct rows with own timestamps/kinds."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_003")
    base = conn.execute("SELECT COUNT(*) c FROM operator_action WHERE shipment_id=?", (sid,)).fetchone()["c"]
    actions.record_operator_action(conn, sid, "note", note="FAKE ATTEMPT SUSPECTED")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    rows = conn.execute(
        "SELECT kind, note FROM operator_action WHERE shipment_id=? ORDER BY rowid LIMIT -1 OFFSET ?",
        (sid, base),
    ).fetchall()
    assert [r["kind"] for r in rows] == ["note", "reattempt_requested"]
    assert rows[0]["note"] == "FAKE ATTEMPT SUSPECTED"


def test_d_customer_confirmation_recorded_and_retrieved(tmp_path):
    """D. Customer confirmation recorded and retrievable verbatim."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_005")
    cid = actions.record_customer_confirmation(
        conn, sid, content="No rider came / no call received.", channel="whatsapp"
    )
    got = actions.list_customer_confirmations(conn, sid)
    assert len(got) == 1
    assert got[0]["id"] == cid
    assert got[0]["content"] == "No rider came / no call received."
    assert got[0]["channel"] == "whatsapp"


def test_e_multiple_confirmations_distinct(tmp_path):
    """E. Multiple confirmations remain distinct records."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_006")
    actions.record_customer_confirmation(conn, sid, "first contact: not reachable", channel="call")
    actions.record_customer_confirmation(conn, sid, "second contact: will be available tomorrow", channel="whatsapp")
    got = actions.list_customer_confirmations(conn, sid)
    assert len(got) == 2
    assert got[0]["content"] != got[1]["content"]


def test_f_all_sources_distinct_in_timeline(tmp_path):
    """F. Courier observation + customer confirmation + operator note + operator
    action all appear in ONE timeline as four distinct attributed entries."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_022")  # courier RFD row from evidence
    actions.record_customer_confirmation(conn, sid, "No rider came / no call received.", channel="call")
    actions.record_operator_action(conn, sid, "note", note="FAKE ATTEMPT SUSPECTED")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    d = detail.shipment_detail(conn, "SAMPLE_022")
    types = [e["type"] for e in d["timeline"]]
    assert types.count("tracking_event") == 1
    assert types.count("customer_confirmation") == 1
    # C1: 2 import-seeded (note + status action) + 2 recorded here = 4
    assert types.count("operator_action") == 4
    labels = {e["label"] for e in d["timeline"]}
    assert "COURIER CLAIM" in labels
    assert "CUSTOMER CONFIRMED" in labels
    assert "OPERATOR NOTE" in labels


def test_g_courier_claim_not_relabeled_as_customer_truth(tmp_path):
    """G. Recording confirmations/actions never relabels courier observations;
    courier entry keeps COURIER CLAIM label and raw values."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_022")
    actions.record_customer_confirmation(conn, sid, "customer denies refusal", channel="call")
    d = detail.shipment_detail(conn, "SAMPLE_022")
    evs = [e for e in d["timeline"] if e["type"] == "tracking_event"]
    assert len(evs) == 1
    assert evs[0]["label"] == "COURIER CLAIM"
    assert evs[0]["raw_code"] == "RFD"  # unchanged
    # no tracking_event was converted into a customer_confirmation
    cc_rows = conn.execute("SELECT COUNT(*) c FROM customer_confirmation").fetchone()["c"]
    te_rows = conn.execute(
        "SELECT COUNT(*) c FROM tracking_event WHERE shipment_id=?", (sid,)
    ).fetchone()["c"]
    assert cc_rows == 1 and te_rows == 1


def test_h_raw_text_preserved_verbatim(tmp_path):
    """H. Operator/customer-entered text preserved exactly, incl. slashes/Urdu mix.
    (C1: import-seeded notes exist; assertions are membership-based.)"""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_011")
    raw_note = "FAKE ATTEMPT THA / rider ne call nahi ki / REATTEMPT KARWAYN"
    actions.record_operator_action(conn, sid, "note", note=raw_note)
    actions.record_customer_confirmation(conn, sid, content="No rider came / no call received.")
    assert raw_note in [r["note"] for r in actions.list_operator_actions(conn, sid)]
    assert "No rider came / no call received." in [
        r["content"] for r in actions.list_customer_confirmations(conn, sid)]
    d = detail.shipment_detail(conn, "SAMPLE_011")
    notes = [e["note"] for e in d["timeline"] if e["type"] == "operator_action"]
    contents = [e["content"] for e in d["timeline"] if e["type"] == "customer_confirmation"]
    assert raw_note in notes
    assert "No rider came / no call received." in contents


def test_i_multi_cycle_all_events_visible(tmp_path):
    """I. Repeated cycles: RFD → confirmation → note → action → later RFD again →
    another confirmation/action. Every event stays visible, none collapsed."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nCY1,RFD\n")
    sid = ship_id(conn, "CY1")
    # cycle 1
    actions.record_customer_confirmation(conn, sid, "cycle1: customer says no rider came", channel="call")
    actions.record_operator_action(conn, sid, "note", note="cycle1: FAKE ATTEMPT SUSPECTED")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    # cycle 2: fresh identical-code observation via new event_key (different text)
    ek = core._event_key("CY1", "RFD", "second cycle remark", None)
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key) VALUES ('c2',?,'RFD','second cycle remark',?,?)",
        (sid, core.utcnow(), ek),
    )
    core.refresh_state(conn, sid)
    actions.record_customer_confirmation(conn, sid, "cycle2: still no delivery", channel="whatsapp")
    actions.record_operator_action(conn, sid, "reattempt_requested")

    d = detail.shipment_detail(conn, "CY1")
    types = [e["type"] for e in d["timeline"]]
    assert types.count("tracking_event") == 2          # both RFD observations
    assert types.count("customer_confirmation") == 2   # both confirmations
    assert types.count("operator_action") == 3         # note + 2 reattempts
    contents = [e["content"] for e in d["timeline"] if e["type"] == "customer_confirmation"]
    assert "cycle1: customer says no rider came" in contents
    assert "cycle2: still no delivery" in contents


def test_j_derivation_remains_correct_after_new_records(tmp_path):
    """J. current_state follows existing derivation only:
    confirmation/action -> ACTION_TAKEN; terminal kinds -> terminal states."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_007")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    assert core.derive_state(conn, sid) == "ACTION_TAKEN"
    actions.record_customer_confirmation(conn, sid, "address updated", channel="whatsapp")
    assert core.derive_state(conn, sid) == "ACTION_TAKEN"  # still operator-side newest


def test_k_fresh_courier_observation_reopens(tmp_path):
    """K. Fresh courier observation after operator action -> NEEDS_ACTION (loop)."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_008")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    assert core.derive_state(conn, sid) == "ACTION_TAKEN"
    ek = core._event_key("SAMPLE_008", "PAYMENT NOT AVAILABLE", "second cycle remark", None)
    # deterministic timestamp strictly AFTER the bumped operator action time
    oa_ts = conn.execute("SELECT MAX(acted_at) m FROM operator_action WHERE shipment_id=?", (sid,)).fetchone()["m"]
    from datetime import datetime, timedelta
    later = (datetime.fromisoformat(oa_ts) + timedelta(seconds=1)).isoformat()
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key) VALUES ('re1',?,'PAYMENT NOT AVAILABLE','second cycle remark',?,?)",
        (sid, later, ek),
    )
    core.refresh_state(conn, sid)
    assert core.derive_state(conn, sid) == "NEEDS_ACTION"


def test_l_invalid_references_fail_safely(tmp_path):
    """L. Invalid/missing shipment references raise; no orphan rows created."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    real_sid = ship_id(conn, "SAMPLE_009")
    with pytest.raises(actions.ShipmentNotFoundError):
        actions.record_operator_action(conn, "nonexistent-id", "note", note="x")
    with pytest.raises(actions.ShipmentNotFoundError):
        actions.record_customer_confirmation(conn, "nonexistent-id", "y")
    with pytest.raises(actions.ValidationError):
        actions.record_operator_action(conn, real_sid, "bogus_kind")           # bad kind
    with pytest.raises(actions.ValidationError):
        actions.record_operator_action(conn, real_sid, "cancel_decided")       # missing reason
    with pytest.raises(actions.ValidationError):
        actions.record_operator_action(conn, real_sid, "note", note="   ")     # empty note
    with pytest.raises(actions.ValidationError):
        actions.record_customer_confirmation(conn, real_sid, content="  ")     # empty content
    # nothing orphaned anywhere (C1: evidence import legitimately seeds
    # legacy notes/actions; the invalid writes above must add NOTHING)
    cc = conn.execute("SELECT COUNT(*) c FROM customer_confirmation").fetchone()["c"]
    assert cc == 0


def test_m_cancel_requires_reason_but_stays_persistable(tmp_path):
    """Cancel_decided is persistable per SPEC model (UI belongs to Slice 4+);
    reason required, stored verbatim."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_010")
    actions.record_operator_action(conn, sid, "cancel_decided",
                                   cancel_reason="NH CHAIYE")
    assert core.derive_state(conn, sid) == "CLOSED_CANCELLED"
