"""Post-audit remediation regression tests (C1, C2, C3, H1)."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
core = importlib.import_module("commerceops.core")
detail = importlib.import_module("commerceops.detail")
actions = importlib.import_module("commerceops.actions")
followups = importlib.import_module("commerceops.followups")
outcomes = importlib.import_module("commerceops.outcomes")

EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence", "sample_cases.csv")


def db(tmp_path):
    return core.connect(str(tmp_path / "rem.db"))


def load_evidence():
    with open(EVIDENCE, encoding="utf-8") as f:
        return f.read()


def ship_id(conn, tno):
    return conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tno,)).fetchone()["id"]


# ------------------------- C1: legacy attribution -------------------------

def test_c1_evidence_notes_are_operator_actions(tmp_path):
    """C1.1: evidence CSV our_remark -> operator_action(kind='note'), verbatim;
    courier tracking_event carries NO operator text."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    notes = conn.execute(
        "SELECT oa.note FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE s.tracking_no='SAMPLE_022' AND oa.kind='note'"
    ).fetchall()
    assert len(notes) == 1
    assert notes[0]["note"] == "FAKE ATTEMPT THA REATTEMPT KARWAYN"
    ev = conn.execute(
        "SELECT te.raw_code, te.raw_text FROM tracking_event te"
        " JOIN shipment s ON s.id=te.shipment_id WHERE s.tracking_no='SAMPLE_022'"
    ).fetchone()
    assert ev["raw_code"] == "RFD"      # courier claim untouched
    assert ev["raw_text"] is None       # no operator text on courier record
    # all 25 evidence rows produced exactly one note each
    total_notes = conn.execute("SELECT COUNT(*) c FROM operator_action WHERE kind='note'").fetchone()["c"]
    assert total_notes == 25


def test_c1_notes_render_as_operator_not_courier(tmp_path):
    """C1.2: legacy notes render as OPERATOR in the timeline; the same text
    never appears under COURIER CLAIM."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    d = detail.shipment_detail(conn, "SAMPLE_022")
    courier_entries = [e for e in d["timeline"] if e["label"] == "COURIER CLAIM"]
    assert len(courier_entries) == 1
    assert courier_entries[0]["raw_text"] is None
    assert "FAKE ATTEMPT THA REATTEMPT KARWAYN" not in [e.get("raw_text") or "" for e in courier_entries]
    operator_notes = [e for e in d["timeline"]
                      if e["type"] == "operator_action"
                      and e.get("note") == "FAKE ATTEMPT THA REATTEMPT KARWAYN"]
    assert len(operator_notes) == 1
    assert operator_notes[0]["label"] == "OPERATOR NOTE"


def test_c1_legacy_status_mapping(tmp_path):
    """C1.3: status REATTEMPT -> reattempt_requested; CANCEL -> cancel_decided
    with our_remark as cancel_reason."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    ra = conn.execute(
        "SELECT COUNT(*) c FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE oa.kind='reattempt_requested'"
    ).fetchone()["c"]
    cd = conn.execute(
        "SELECT oa.cancel_reason FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE oa.kind='cancel_decided'"
    ).fetchall()
    assert ra == 23   # evidence: 23 REATTEMPT rows
    assert len(cd) == 2  # evidence: 2 CANCEL rows
    reasons = {r["cancel_reason"] for r in cd}
    assert "CANCEL (SELF COLLECT NH KRENGY)" in reasons
    assert "CANCEL (NH CHAIYE)" in reasons


def test_c1_raw_source_text_untouched(tmp_path):
    """C1: raw_source_text preserved exactly as before the fix."""
    conn = db(tmp_path)
    original = load_evidence()
    core.import_source(conn, original)
    stored = conn.execute("SELECT raw_source_text FROM import_batch").fetchone()[0]
    assert stored == original


def test_c1_legacy_reimport_no_duplicates(tmp_path):
    """C1.5: re-importing identical legacy data creates zero duplicate
    shipments/events/notes/status-actions."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    before = {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("shipment", "tracking_event", "operator_action")
    }
    res = core.import_source(conn, load_evidence(), source_label="re-import")
    assert res["shipments_created"] == 0
    assert res["events_created"] == 0
    assert res["actions_created"] == 0
    after = {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("shipment", "tracking_event", "operator_action")
    }
    assert before == after


def test_c1_unknown_legacy_status_quarantined_not_guessed(tmp_path):
    """C1: unmapped legacy status values are quarantined verbatim — never guessed."""
    conn = db(tmp_path)
    text = ("tracking_no,postex_remark,our_remark,status\n"
            "U1,RFD,some remark,RETURNED\n")  # RETURNED is not a mapped legacy status
    res = core.import_source(conn, text)
    q = conn.execute(
        "SELECT line_text, reason FROM quarantine_row WHERE reason LIKE 'unmapped%'"
    ).fetchall()
    assert len(q) == 1
    assert "RETURNED" in q[0]["reason"]
    kinds = conn.execute(
        "SELECT DISTINCT kind FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE s.tracking_no='U1'"
    ).fetchall()
    assert [k[0] for k in kinds] == ["note"]  # only the note was created


# ------------------- C2: back-dated terminal rejection -------------------

def test_c2_backdated_terminal_rejected(tmp_path):
    """C2.1+C2.3: back-dated terminal action raises ValidationError and leaves
    NO partial terminal record behind."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nBD1,RFD\n")
    sid = ship_id(conn, "BD1")
    with pytest.raises(actions.ValidationError) as exc:
        outcomes.mark_delivered(conn, sid, acted_at="2020-01-01T00:00:00+00:00")
    assert "predates a newer event" in str(exc.value)
    n = conn.execute(
        "SELECT COUNT(*) c FROM operator_action WHERE shipment_id=? AND kind='delivered_confirmed'",
        (sid,),
    ).fetchone()["c"]
    assert n == 0  # nothing persisted
    # same protection for cancel and returned
    with pytest.raises(actions.ValidationError):
        outcomes.mark_cancelled(conn, sid, cancel_reason="x", acted_at="2020-01-01T00:00:00+00:00")
    with pytest.raises(actions.ValidationError):
        outcomes.mark_returned(conn, sid, acted_at="2020-01-01T00:00:00+00:00")
    assert conn.execute(
        "SELECT COUNT(*) c FROM operator_action WHERE shipment_id=?", (sid,)
    ).fetchone()["c"] == 0  # still zero terminal records


def test_c2_no_misleading_state_after_rejection(tmp_path):
    """C2.2: state is unchanged by a rejected back-dated terminal attempt."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nBD2,RFD\n")
    sid = ship_id(conn, "BD2")
    before = core.derive_state(conn, sid)
    with pytest.raises(actions.ValidationError):
        outcomes.mark_cancelled(conn, sid, cancel_reason="late entry",
                                acted_at="2020-01-01T00:00:00+00:00")
    assert core.derive_state(conn, sid) == before


def test_c2_chronological_terminal_still_works(tmp_path):
    """C2.4: valid chronological (current-time) terminal actions still work."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nBD3,RFD\n")
    sid = ship_id(conn, "BD3")
    outcomes.mark_delivered(conn, sid)          # acted_at=None -> monotonic now
    assert core.derive_state(conn, sid) == "CLOSED_DELIVERED"
    # explicit acted_at AFTER all events also works
    ek = core._event_key("BD3", "RFD", "later remark", None)
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key)"
        " VALUES ('bd3b',?,'RFD','later remark','2099-01-01T00:00:00+00:00',?)", (sid, ek))
    core.refresh_state(conn, sid)
    assert core.derive_state(conn, sid) == "NEEDS_ACTION"  # reopened first
    outcomes.mark_delivered(conn, sid, acted_at="2099-01-02T00:00:00+00:00")
    assert core.derive_state(conn, sid) == "CLOSED_DELIVERED"


def test_c2_post_terminal_reopen_unchanged(tmp_path):
    """C2.5: fresh courier observation after a terminal outcome reopens per the
    unchanged existing derivation (regression guard for C2's validation)."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nBD4,RFD\n")
    sid = ship_id(conn, "BD4")
    outcomes.mark_cancelled(conn, sid, cancel_reason="NH CHAIYE")
    assert core.derive_state(conn, sid) == "CLOSED_CANCELLED"
    ek = core._event_key("BD4", "RFD", "post-cancel remark", None)
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key)"
        " VALUES ('pt',?,'RFD','post-cancel remark','2099-06-01T00:00:00+00:00',?)", (sid, ek))
    core.refresh_state(conn, sid)
    assert core.derive_state(conn, sid) == "NEEDS_ACTION"


# ------------------------- C3: one queue -------------------------

def test_c3_queue_entry_points_identical(tmp_path):
    """C3: both public queue entry points produce IDENTICAL results — one
    authoritative behavior (fresh derivation + follow-up integration)."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_001")
    actions.record_customer_confirmation(conn, sid, "addr updated", channel="call")
    followups.create_follow_up(conn, sid, due_at="2020-01-01", reason="old")
    q_core = core.work_queue(conn)
    q_followups = followups.work_queue(conn)
    assert q_core == q_followups


def test_c3_queue_uses_fresh_derivation_not_cache(tmp_path):
    """C3: stale cached current_state cannot hide a shipment from the queue.
    (Raw-SQL writer bypasses refresh_state; queue must still see NEEDS_ACTION.)"""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nST1,RFD\n")
    sid = ship_id(conn, "ST1")
    conn.execute(
        "INSERT INTO operator_action (id,shipment_id,kind,acted_at) VALUES ('stale',?,'reattempt_requested','2026-01-01T00:00:00+00:00')",
        (sid,))
    # cache now says ACTION_TAKEN (refreshed at insert time), but a LATER raw
    # courier observation makes truth NEEDS_ACTION without refresh:
    ek = core._event_key("ST1", "RFD", "later remark", None)
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key)"
        " VALUES ('later',?,'RFD','later remark','2099-01-01T00:00:00+00:00',?)", (sid, ek))
    conn.execute("UPDATE shipment SET current_state='ACTION_TAKEN' WHERE id=?", (sid,))
    q = core.work_queue(conn)
    entry = [i for i in q if i["tracking_no"] == "ST1"]
    assert entry and "needs_action" in entry[0]["reasons"]  # truth, not cache


# ------------------------- H1: HTML escaping -------------------------

def test_h1_html_escaped_everywhere(tmp_path):
    """H1: hostile values in tracking numbers, codes, notes, confirmations,
    follow-up reasons and actor names render escaped — no executable markup."""
    conn = db(tmp_path)
    hostile_tno = '<script>alert(1)</script>'
    core.import_source(conn, f"{hostile_tno},<img src=x onerror=alert(2)>\n")
    sid = conn.execute("SELECT id FROM shipment").fetchone()["id"]
    actions.record_operator_action(conn, sid, "note", note="<b>bold & 'quoted'</b>")
    actions.record_operator_action(conn, sid, "reattempt_requested", note="<i>x</i>")
    actions.record_customer_confirmation(conn, sid, content='<a href="http://evil">click</a>')
    followups.create_follow_up(conn, sid, due_at="2099-01-01", reason="A & B <test>")
    d = detail.shipment_detail(conn, hostile_tno)
    html = detail.render_detail_html(d)
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(2)>" not in html
    assert "<b>bold & 'quoted'</b>" not in html
    assert "<a href=\"http://evil\">" not in html
    assert "A & B <test>" not in html
    # but the ESCAPED forms are present so data is still visible
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;bold &amp; &#x27;quoted&#x27;&lt;/b&gt;" in html or "&amp;" in html
    assert "A &amp; B &lt;test&gt;" in html
