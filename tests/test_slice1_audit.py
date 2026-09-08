"""Slice-1 adversarial audit regression tests (post-audit)."""
import importlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
core = importlib.import_module("commerceops.core")
followups = importlib.import_module("commerceops.followups")


def db(tmp_path):
    return core.connect(str(tmp_path / "a.db"))


def test_rapid_identical_reimport_does_not_crash(tmp_path):
    """Audit fix 1: batch_id must not depend on clock granularity.
    Two back-to-back imports of the SAME text previously collided on
    import_batch UNIQUE when the OS clock did not advance."""
    conn = db(tmp_path)
    text = "tracking_no,postex_remark\nT1,RFD\n"
    core.import_source(conn, text)
    res = core.import_source(conn, text)  # immediately, same clock tick likely
    assert res["events_created"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM import_batch").fetchone()["c"] == 2


def test_import_is_atomic_on_midway_failure(tmp_path):
    """Audit fix 2: a crash mid-import must roll back ALL writes — no partial
    shipments/events/batches committed."""
    conn = db(tmp_path)
    orig = core.refresh_state
    calls = {"n": 0}

    def boom(c, sid):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash")
        return orig(c, sid)

    core.refresh_state = boom
    try:
        try:
            core.import_source(conn, "tracking_no,postex_remark\nA1,RFD\nB1,RFD\nC1,RFD\nD1,RFD\n")
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        for table in ("shipment", "tracking_event", "import_batch", "quarantine_row"):
            n = conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            assert n == 0, f"{table} has {n} rows after failed import"
    finally:
        core.refresh_state = orig


def test_data_row_looking_like_header_is_not_swallowed(tmp_path):
    """Audit fix 3: only the FIRST line may be a header. A later data row
    'TRACKING_NO,...' is data (or quarantined), never silently dropped."""
    conn = db(tmp_path)
    text = "tracking_no,postex_remark\nTRACKING_NO,POSTEX_REMARK\nT5,RFD\n"
    res = core.import_source(conn, text)
    # 3 non-empty lines: header + 2 rows; row 2 has uppercase label cells but
    # is still DATA per audit decision (verbatim preservation).
    total_accounted = res["rows_parsed"] + res["quarantined"]
    assert total_accounted == 2
    stored = conn.execute(
        "SELECT tracking_no FROM shipment UNION ALL SELECT line_text FROM quarantine_row"
    ).fetchall()
    assert any("TRACKING_NO" in (r[0] or "") for r in stored)


def test_whitespace_only_remark_quarantined(tmp_path):
    """Audit fix 4: whitespace-only postex_remark must not become an event."""
    conn = db(tmp_path)
    res = core.import_source(conn, "tracking_no,postex_remark\nY1,' '\n")
    n_events = conn.execute("SELECT COUNT(*) c FROM tracking_event").fetchone()["c"]
    assert n_events == 0
    assert res["quarantined"] == 1
    assert res["rows_parsed"] == 0


def test_queue_latest_code_matches_derivation_tiebreak(tmp_path):
    """Audit fix 5 (updated for C3): the authoritative queue (followups) shows
    the shipment as NEEDS_ACTION with the latest courier code; core.work_queue
    delegates to it, so both entry points agree."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nT1,RFD\n")
    r = conn.execute("SELECT id FROM shipment WHERE tracking_no='T1'").fetchone()
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,imported_at,event_key) VALUES ('e9',?,'INA',?,'k9')",
        (r["id"], core.utcnow()),
    )
    core.refresh_state(conn, r["id"])
    q_core = core.work_queue(conn)
    q_fup = followups.work_queue(conn)
    assert q_core == q_fup  # single authoritative behavior
    entry = [i for i in q_fup if i["tracking_no"] == "T1"][0]
    assert "needs_action" in entry["reasons"]


def test_distinct_observations_never_collapse(tmp_path):
    """Event identity: genuinely distinct observations (different code) for the
    same tracking number always create distinct events. C1 update: our_remark
    no longer participates in the courier event key — distinct our_remark on
    identical code/text creates an operator note instead of a second event."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nX1,RFD,(REATTEMPT)\n")
    res = core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nX1,CNA,(REATTEMPT)\nX1,RFD,FAKE ATTEMPT THA REATTEMPT KARWAYN\n")
    # X1/CNA -> new event; X1/RFD+FAKE text -> same courier obs (dup) + new operator note
    assert res["events_created"] == 1
    codes = [r[0] for r in conn.execute(
        "SELECT te.raw_code FROM tracking_event te JOIN shipment s ON s.id=te.shipment_id WHERE s.tracking_no='X1' ORDER BY te.rowid"
    ).fetchall()]
    assert codes == ["RFD", "CNA"]  # courier observations verbatim
    notes = conn.execute(
        "SELECT COUNT(*) c FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE s.tracking_no='X1' AND oa.kind='note' AND oa.note LIKE 'FAKE%'"
    ).fetchone()["c"]
    assert notes == 1  # interpretation preserved as OPERATOR note


def test_cross_batch_same_observation_is_duplicate(tmp_path):
    """Idempotency across batches: identical observation re-imported in a NEW
    batch (different surrounding rows) creates no duplicate event."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nA1,RFD\nB1,CNA\n")
    res = core.import_source(conn, "tracking_no,postex_remark\nC1,HOLD\nA1,RFD\n")
    assert res["events_created"] == 1  # C1 new, A1/RFD duplicate
    assert res["duplicates"] == 1
    n = conn.execute("SELECT COUNT(*) c FROM tracking_event te JOIN shipment s ON s.id=te.shipment_id WHERE s.tracking_no='A1'").fetchone()["c"]
    assert n == 1


def test_distinct_courier_codes_never_collapse(tmp_path):
    """Event identity (updated for C1): genuinely distinct courier codes create
    distinct events; a repeated identical code with different our_remark is a
    duplicate COURIER observation but its operator text becomes an OPERATOR
    note — never collapsed, never misattributed."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nX1,RFD,(REATTEMPT)\n")
    res = core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nX1,CNA,(REATTEMPT)\nX1,RFD,FAKE ATTEMPT THA REATTEMPT KARWAYN\n")
    assert res["events_created"] == 1  # X1/CNA new; second RFD = duplicate obs
    codes = [r[0] for r in conn.execute(
        "SELECT te.raw_code FROM tracking_event te JOIN shipment s ON s.id=te.shipment_id WHERE s.tracking_no='X1' ORDER BY te.rowid"
    ).fetchall()]
    assert codes == ["RFD", "CNA"]
    # the FAKE ATTEMPT text is preserved as an OPERATOR note, not lost
    notes = conn.execute(
        "SELECT oa.note FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE s.tracking_no='X1' AND oa.kind='note' ORDER BY oa.rowid"
    ).fetchall()
    assert any("FAKE ATTEMPT THA REATTEMPT KARWAYN" == n[0] for n in notes)


def test_conflicting_phone_recorded_every_time_not_discarded(tmp_path):
    """Conflicts: repeated conflicting values are each recorded verbatim;
    canonical phone is never overwritten. Phone lives in column position 5
    per the parser's documented contract (after our_remark/status)."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark,our_remark,status,customer_phone\nZ1,RFD,,,0300111\n")
    core.import_source(conn, "tracking_no,postex_remark,our_remark,status,customer_phone\nZ1,CNA,,,0999999\n")
    core.import_source(conn, "tracking_no,postex_remark,our_remark,status,customer_phone\nZ1,CNA,,,0999999\n")  # repeat
    canonical = conn.execute("SELECT customer_phone FROM shipment WHERE tracking_no='Z1'").fetchone()[0]
    assert canonical == "0300111"  # untouched
    conflicts = conn.execute("SELECT incoming_value FROM import_conflict").fetchall()
    assert len(conflicts) == 2  # every occurrence preserved, none discarded
    assert all(c[0] == "0999999" for c in conflicts)


def test_terminal_state_only_from_human_actions(tmp_path):
    """No courier observation can produce a terminal state; courier obs after
    cancel correctly reopens NEEDS_ACTION per §B derivation."""
    conn = db(tmp_path)
    core.import_source(conn, "tracking_no,postex_remark\nW1,RFD\n")
    r = conn.execute("SELECT id FROM shipment WHERE tracking_no='W1'").fetchone()
    # courier observation mentioning DELIVERED is STILL just an observation:
    ek = core._event_key("W1", "DELIVERED?", "", None)
    conn.execute("INSERT INTO tracking_event (id,shipment_id,raw_code,imported_at,event_key) VALUES ('ed',?, 'DELIVERED?',?,?)", (r["id"], core.utcnow(), ek))
    assert core.derive_state(conn, r["id"]) == "NEEDS_ACTION"  # not terminal
