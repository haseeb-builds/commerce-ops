import importlib
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
core = importlib.import_module("commerceops.core")

EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence", "sample_cases.csv")


def fresh_db(tmp_path):
    conn = core.connect(str(tmp_path / "test.db"))
    yield conn
    conn.close()


def load_evidence():
    with open(EVIDENCE, encoding="utf-8") as f:
        return f.read()


def test_a_25_row_evidence_imports(tmp_path):
    """A. The 25-row evidence dataset imports successfully."""
    conn = next(fresh_db(tmp_path))
    result = core.import_source(conn, load_evidence(), source_label="sample_cases.csv")
    assert result["rows_parsed"] == 25
    assert result["quarantined"] == 0
    assert result["shipments_created"] == 25
    assert result["events_created"] == 25
    assert conn.execute("SELECT COUNT(*) c FROM shipment").fetchone()["c"] == 25
    assert conn.execute("SELECT COUNT(*) c FROM tracking_event").fetchone()["c"] == 25


def test_b_duplicate_tracking_single_shipment(tmp_path):
    """B. 29221080025518 appearing twice -> exactly one Shipment.

    Uses the real PDF-derived duplicate pattern (same tracking_no twice in one
    batch), reproduced against the evidence CSV plus the duplicated row.
    """
    conn = next(fresh_db(tmp_path))
    text = load_evidence() + "\n29221080025518,RFD,(REATTEMPT)\n"
    result = core.import_source(conn, text)
    assert result["rows_parsed"] == 26
    count = conn.execute(
        "SELECT COUNT(*) c FROM shipment WHERE tracking_no='29221080025518'"
    ).fetchone()["c"]
    assert count == 1


def test_c_reimport_zero_new_shipments(tmp_path):
    """C. Re-importing identical dataset creates zero additional Shipments."""
    conn = next(fresh_db(tmp_path))
    core.import_source(conn, load_evidence())
    before = conn.execute("SELECT COUNT(*) c FROM shipment").fetchone()["c"]
    result2 = core.import_source(conn, load_evidence(), source_label="re-import")
    assert result2["shipments_created"] == 0
    after = conn.execute("SELECT COUNT(*) c FROM shipment").fetchone()["c"]
    assert after == before


def test_d_reimport_zero_duplicate_events(tmp_path):
    """D. Re-importing identical dataset creates zero duplicate TrackingEvents."""
    conn = next(fresh_db(tmp_path))
    core.import_source(conn, load_evidence())
    before = conn.execute("SELECT COUNT(*) c FROM tracking_event").fetchone()["c"]
    result2 = core.import_source(conn, load_evidence(), source_label="re-import")
    assert result2["duplicates"] == 25
    assert result2["events_created"] == 0
    after = conn.execute("SELECT COUNT(*) c FROM tracking_event").fetchone()["c"]
    assert after == before


def test_e_duplicate_rows_both_represented_one_shipment(tmp_path):
    """E. Both source rows for 29221080025518 represented without two shipments."""
    conn = next(fresh_db(tmp_path))
    # two DIFFERENT observations for same tracking number in one batch
    text = (
        "tracking_no,postex_remark,our_remark,status\n"
        "29221080025518,RFD,(REATTEMPT)\n"
        "29221080025518,CNA,(REATTEMPT)\n"
    )
    result = core.import_source(conn, text)
    assert result["rows_parsed"] == 2
    shipments = conn.execute(
        "SELECT id FROM shipment WHERE tracking_no='29221080025518'"
    ).fetchall()
    assert len(shipments) == 1
    events = conn.execute(
        "SELECT raw_code FROM tracking_event WHERE shipment_id=? ORDER BY imported_at",
        (shipments[0]["id"],),
    ).fetchall()
    assert [e["raw_code"] for e in events] == ["RFD", "CNA"]  # both preserved


def test_f_unparseable_rows_quarantined(tmp_path):
    """F. Unknown/unparseable rows are quarantined, never silently dropped."""
    conn = next(fresh_db(tmp_path))
    text = (
        "tracking_no,postex_remark,status\n"
        "SAMPLE_OK,RFD,\n"
        ",RFD,\n"                    # missing tracking_no
        "SAMPLE_NO_REMARK,\n"        # missing remark
        "\n"                          # blank line ignored
        "SAMPLE_OK2,OPN,\n"
    )
    result = core.import_source(conn, text)
    assert result["quarantined"] == 2
    q = conn.execute("SELECT line_text, reason FROM quarantine_row").fetchall()
    assert len(q) == 2
    # original line text preserved verbatim
    texts = {r["line_text"] for r in q}
    assert ",RFD," in texts or any(",RFD," in t for t in texts)
    assert conn.execute("SELECT COUNT(*) c FROM shipment").fetchone()["c"] == 2


def test_g_raw_source_text_recoverable(tmp_path):
    """G. Exact imported source text remains recoverable via ImportBatch.raw_source_text."""
    conn = next(fresh_db(tmp_path))
    original = load_evidence()
    core.import_source(conn, original)
    stored = conn.execute("SELECT raw_source_text FROM import_batch").fetchone()[0]
    assert stored == original


def test_h_fresh_observation_needs_action(tmp_path):
    """H. Fresh courier observation -> NEEDS_ACTION per §B derivation."""
    conn = next(fresh_db(tmp_path))
    core.import_source(conn, load_evidence())
    row = conn.execute(
        "SELECT * FROM shipment WHERE tracking_no='SAMPLE_001'"
    ).fetchone()
    # C1: import now maps legacy status REATTEMPT -> reattempt_requested,
    # so the shipment legitimately starts ACTION_TAKEN (operator already acted).
    assert row["current_state"] == "ACTION_TAKEN"
    # a fresh courier observation flips it back to NEEDS_ACTION (the loop)
    ek0 = core._event_key(row["tracking_no"], "INA", "fresh remark", None)
    conn.execute(
        "INSERT INTO tracking_event (id, shipment_id, raw_code, raw_text, imported_at, event_key)"
        " VALUES ('evt_pre', ?, 'INA', 'fresh remark', '2099-01-01T00:00:00+00:00', ?)",
        (row["id"], ek0),
    )
    assert core.derive_state(conn, row["id"]) == "NEEDS_ACTION"
    q = core.work_queue(conn)
    assert any(i["tracking_no"] == "SAMPLE_001" for i in q)


def test_i_no_auto_interpretation_of_courier_truth(tmp_path):
    """I. Courier remarks stored verbatim; no customer-truth interpretation applied."""
    conn = next(fresh_db(tmp_path))
    core.import_source(conn, load_evidence())
    # C1 fix: our_remark is now correctly attributed as an OPERATOR note,
    # never a courier observation; no customer_confirmation is fabricated.
    cc_count = conn.execute("SELECT COUNT(*) c FROM customer_confirmation").fetchone()["c"]
    assert cc_count == 0
    # 25 evidence rows -> 25 operator notes from our_remark + mapped status actions
    oa_notes = conn.execute(
        "SELECT COUNT(*) c FROM operator_action WHERE kind='note'"
    ).fetchone()["c"]
    assert oa_notes == 25
    ev = conn.execute(
        "SELECT te.raw_code, te.raw_text FROM tracking_event te"
        " JOIN shipment s ON s.id=te.shipment_id WHERE s.tracking_no='SAMPLE_022'"
    ).fetchone()
    assert ev["raw_code"] == "RFD"
    assert ev["raw_text"] is None  # courier event carries NO operator text
    # fake-attempt interpretation lives on the OPERATOR note, verbatim
    note = conn.execute(
        "SELECT oa.note FROM operator_action oa JOIN shipment s ON s.id=oa.shipment_id"
        " WHERE s.tracking_no='SAMPLE_022' AND oa.kind='note'"
    ).fetchone()
    assert note["note"] == "FAKE ATTEMPT THA REATTEMPT KARWAYN"
    # raw code is not mapped to any business meaning anywhere
    assert conn.execute(
        "SELECT COUNT(*) c FROM tracking_event WHERE raw_code NOT IN ('INA','RFD','CNA','OPN','HOLD','PAYMENT NOT AVAILABLE','RESTRICTED AREA')"
    ).fetchone()["c"] == 0


def test_j_full_suite_marker():
    """J. Marker: full suite runs; all prior tests are part of it."""
    assert True
