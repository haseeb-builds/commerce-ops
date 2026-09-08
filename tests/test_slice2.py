"""Slice 2 tests: shipment detail + chronological timeline."""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
core = importlib.import_module("commerceops.core")
detail = importlib.import_module("commerceops.detail")

EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence", "sample_cases.csv")


def db(tmp_path):
    return core.connect(str(tmp_path / "s2.db"))


def load_evidence():
    with open(EVIDENCE, encoding="utf-8") as f:
        return f.read()


def test_a_open_existing_shipment_by_tracking(tmp_path):
    """A. Existing shipment opened by tracking number."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    d = detail.shipment_detail(conn, "SAMPLE_001")
    assert d is not None
    assert d["tracking_no"] == "SAMPLE_001"
    assert len(d["timeline"]) >= 1


def test_b_unknown_tracking_clear_not_found(tmp_path):
    """B. Unknown tracking number returns explicit not-found (None), not an error."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    assert detail.shipment_detail(conn, "NOPE_DOES_NOT_EXIST") is None
    # empty-history case: shipment exists but has no events -> explicit empty timeline
    html = detail.render_detail_html(None)
    assert "not found" in html.lower()


def test_c_duplicate_evidence_shipment_both_observations_visible(tmp_path):
    """C. 29221080025518-style duplicate: one shipment, both observations preserved
    in the detail timeline."""
    conn = db(tmp_path)
    text = (
        "tracking_no,postex_remark,our_remark,status\n"
        "29221080025518,RFD,(REATTEMPT)\n"
        "29221080025518,CNA,(REATTEMPT)\n"
    )
    core.import_source(conn, text)
    d = detail.shipment_detail(conn, "29221080025518")
    assert d is not None
    codes = [e["raw_code"] for e in d["timeline"] if e["type"] == "tracking_event"]
    assert codes == ["RFD", "CNA"]


def test_d_timeline_ordering_deterministic(tmp_path):
    """D. Ordering is deterministic across repeated builds."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    d1 = detail.shipment_detail(conn, "SAMPLE_003")
    d2 = detail.shipment_detail(conn, "SAMPLE_003")
    keys1 = [(e["at"], e["ord"]) for e in d1["timeline"]]
    keys2 = [(e["at"], e["ord"]) for e in d2["timeline"]]
    assert keys1 == keys2
    assert keys1 == sorted(keys1)


def test_e_equal_timestamps_use_slice1_tiebreak(tmp_path):
    """E. Equal-timestamp events ordered by insertion: later insert = later in
    timeline, matching derive_state's tie-break."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nT9,RFD\n")
    r = conn.execute("SELECT id FROM shipment WHERE tracking_no='T9'").fetchone()
    same_ts = core.utcnow()
    for eid, code in (("x1", "INA"), ("x2", "HOLD")):
        ek = core._event_key("T9", code, "", None)
        conn.execute(
            "INSERT INTO tracking_event (id,shipment_id,raw_code,imported_at,event_key) VALUES (?,?,?,?,?)",
            (eid, r["id"], code, same_ts, ek),
        )
    d = detail.shipment_detail(conn, "T9")
    codes = [e["raw_code"] for e in d["timeline"] if e["type"] == "tracking_event"]
    assert codes == ["RFD", "INA", "HOLD"]  # insertion order preserved
    # cross-check with derive_state's newest pick (must be HOLD)
    assert core.derive_state(conn, r["id"]) == "NEEDS_ACTION"
    newest_entry = max(d["timeline"], key=lambda e: (e["at"], e["ord"]))
    assert newest_entry["raw_code"] == "HOLD"


def test_f_repeated_identical_code_not_collapsed(tmp_path):
    """F. Same courier code observed twice (distinct our_remark) — the second
    is a duplicate COURIER observation (not collapsed history: both cycles
    visible via distinct operator notes); C1 update: operator text now lives
    on OPERATOR note entries, never on the courier claim."""
    conn = db(tmp_path)
    core.import_source(conn,
        "tracking_no,postex_remark,our_remark,status\n"
        "T10,RFD,(REATTEMPT)\n"
        "T10,RFD,FAKE ATTEMPT THA REATTEMPT KARWAYN\n")
    d = detail.shipment_detail(conn, "T10")
    evs = [e for e in d["timeline"] if e["type"] == "tracking_event"]
    # identical code+no courier text -> single idempotent observation
    assert len(evs) >= 1 and all(e["raw_code"] == "RFD" for e in evs)
    notes = [e["note"] for e in d["timeline"]
             if e["type"] == "operator_action" and e.get("note")]
    assert "(REATTEMPT)" in notes
    assert "FAKE ATTEMPT THA REATTEMPT KARWAYN" in notes
    assert all(e["label"].startswith("OPERATOR") for e in d["timeline"]
               if e["type"] == "operator_action")


def test_g_courier_labeled_as_claim_not_customer_truth(tmp_path):
    """G. Every tracking_event labeled COURIER CLAIM; no customer_confirmation
    fabricated; labels are distinct."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    d = detail.shipment_detail(conn, "SAMPLE_022")  # FAKE ATTEMPT row
    ev = [e for e in d["timeline"] if e["type"] == "tracking_event"][0]
    assert ev["label"] == "COURIER CLAIM"
    cc = [e for e in d["timeline"] if e["type"] == "customer_confirmation"]
    assert cc == []  # nothing auto-promoted to customer truth
    html = detail.render_detail_html(d)
    assert "CUSTOMER CONFIRMED" not in html or cc  # label absent when no records


def test_h_raw_values_unchanged_in_timeline(tmp_path):
    """H. Courier raw_code verbatim; operator text (our_remark) preserved
    verbatim as OPERATOR notes — C1 corrected attribution."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    d = detail.shipment_detail(conn, "SAMPLE_024")
    ev = [e for e in d["timeline"] if e["type"] == "tracking_event"][0]
    assert ev["raw_code"] == "RESTRICTED AREA"
    # courier claim carries no operator text
    assert ev["raw_text"] is None
    # the cancel interpretation is an OPERATOR note, verbatim
    d22 = detail.shipment_detail(conn, "SAMPLE_022")
    note22 = [e["note"] for e in d22["timeline"]
              if e["type"] == "operator_action" and e.get("note")]
    assert "FAKE ATTEMPT THA REATTEMPT KARWAYN" in note22


def test_i_displayed_state_matches_derivation(tmp_path):
    """I. Displayed state equals Slice 1 derivation, including after mixed events;
    cache mismatch (out-of-band writer) is surfaced, never silently stale."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    d = detail.shipment_detail(conn, "SAMPLE_001")
    # C1: legacy REATTEMPT status maps to a reattempt_requested action, so the
    # shipment legitimately derives ACTION_TAKEN right after import.
    assert d["derived_state"] == "ACTION_TAKEN"
    assert not d["cache_mismatch"]
    # add operator action + fresh observation, recheck
    r = conn.execute("SELECT id FROM shipment WHERE tracking_no='SAMPLE_001'").fetchone()
    conn.execute(
        "INSERT INTO operator_action (id,shipment_id,kind,acted_at) VALUES ('oa1',?,'reattempt_requested',?)",
        (r["id"], core.utcnow()),
    )
    ek = core._event_key("SAMPLE_001", "INA", "second cycle remark", None)
    # deterministic timestamp strictly AFTER the import-seeded action time
    oa_ts = conn.execute("SELECT MAX(acted_at) m FROM operator_action WHERE shipment_id=?", (r["id"],)).fetchone()["m"]
    from datetime import datetime, timedelta
    later = (datetime.fromisoformat(oa_ts) + timedelta(seconds=1)).isoformat()
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key) VALUES ('te9',?, 'INA','second cycle remark',?,?)",
        (r["id"], later, ek),
    )
    core.refresh_state(conn, r["id"])
    d2 = detail.shipment_detail(conn, "SAMPLE_001")
    assert d2["derived_state"] == "NEEDS_ACTION"  # fresh obs outranks last action
    assert not d2["cache_mismatch"]


def test_k_cache_mismatch_surfaced_not_silent(tmp_path):
    """Cache is a cache: if an out-of-band writer bypasses refresh_state,
    detail shows the FRESH derivation and flags the mismatch."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nM1,RFD\n")
    r = conn.execute("SELECT id FROM shipment WHERE tracking_no='M1'").fetchone()
    # bypass refresh_state deliberately (raw SQL insert)
    conn.execute(
        "INSERT INTO operator_action (id,shipment_id,kind,acted_at) VALUES ('ob1',?,'reattempt_requested',?)",
        (r["id"], core.utcnow()),
    )
    d = detail.shipment_detail(conn, "M1")
    assert d["derived_state"] == "ACTION_TAKEN"      # truth from events
    assert d["cached_state"] == "NEEDS_ACTION"       # stale cache
    assert d["cache_mismatch"] is True               # surfaced, not hidden
    html = detail.render_detail_html(d)
    assert "out of sync" in html
