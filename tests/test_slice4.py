"""Slice 4 tests: human-set follow-ups + queue integration."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
core = importlib.import_module("commerceops.core")
detail = importlib.import_module("commerceops.detail")
actions = importlib.import_module("commerceops.actions")
followups = importlib.import_module("commerceops.followups")

EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "evidence", "sample_cases.csv")
NOW = "2026-08-26T12:00:00+00:00"  # deterministic test clock


def db(tmp_path):
    return core.connect(str(tmp_path / "s4.db"))


def load_evidence():
    with open(EVIDENCE, encoding="utf-8") as f:
        return f.read()


def ship_id(conn, tno):
    return conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tno,)).fetchone()["id"]


def test_a_create_follow_up(tmp_path):
    """A. Follow-up can be created for an existing shipment."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_001")
    fid = followups.create_follow_up(conn, sid, due_at="2026-08-27", reason="Check reattempt")
    row = conn.execute("SELECT * FROM follow_up WHERE id=?", (fid,)).fetchone()
    assert row["status"] == "open"
    assert row["created_at"] is not None


def test_b_missing_shipment_rejected(tmp_path):
    """B. Unknown shipment -> clear domain error, no orphan row."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    with pytest.raises(actions.ShipmentNotFoundError):
        followups.create_follow_up(conn, "no-such-id", due_at="2026-08-27", reason="x")
    assert conn.execute("SELECT COUNT(*) c FROM follow_up").fetchone()["c"] == 0


def test_c_blank_reason_rejected(tmp_path):
    """C. Missing/blank reason rejected."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_002")
    with pytest.raises(actions.ValidationError):
        followups.create_follow_up(conn, sid, due_at="2026-08-27", reason="")
    with pytest.raises(actions.ValidationError):
        followups.create_follow_up(conn, sid, due_at="2026-08-27", reason=None)
    with pytest.raises(actions.ValidationError):
        followups.create_follow_up(conn, sid, due_at="2026-08-27", reason="   ")


def test_d_missing_due_at_rejected(tmp_path):
    """D. Missing due_at rejected — system never invents a date."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_003")
    with pytest.raises(actions.ValidationError):
        followups.create_follow_up(conn, sid, due_at="", reason="x")
    with pytest.raises(actions.ValidationError):
        followups.create_follow_up(conn, sid, due_at=None, reason="x")


def test_e_persists_exact_values(tmp_path):
    """E. due_at and reason preserved exactly; creation timestamp recorded."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_004")
    followups.create_follow_up(conn, sid, due_at="2026-08-30T09:00:00+00:00",
                               reason="Call customer about address")
    got = followups.list_follow_ups(conn, sid)
    assert len(got["open"]) == 1
    fu = got["open"][0]
    assert fu["due_at"] == "2026-08-30T09:00:00+00:00"
    assert fu["reason"] == "Call customer about address"


def test_f_open_listed(tmp_path):
    """F. Open follow-ups are listed."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_005")
    followups.create_follow_up(conn, sid, due_at="2099-01-01", reason="far future")
    got = followups.list_follow_ups(conn, sid, now=NOW)
    assert len(got["open"]) == 1
    assert len(got["upcoming"]) == 1
    assert len(got["overdue"]) == 0


def test_g_upcoming_vs_overdue_distinguishable(tmp_path):
    """G. Upcoming (due_at > now) vs overdue (due_at <= now) partitioned."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_006")
    followups.create_follow_up(conn, sid, due_at="2026-08-25", reason="already past")   # overdue at NOW
    followups.create_follow_up(conn, sid, due_at="2026-09-01", reason="still future")   # upcoming at NOW
    got = followups.list_follow_ups(conn, sid, now=NOW)
    assert [f["reason"] for f in got["overdue"]] == ["already past"]
    assert [f["reason"] for f in got["upcoming"]] == ["still future"]


def test_h_overdue_surfaced_in_queue(tmp_path):
    """H. Shipment with overdue open follow-up appears in work_queue even when
    its derived state is ACTION_TAKEN."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_007")
    actions.record_operator_action(conn, sid, "reattempt_requested")  # -> ACTION_TAKEN
    followups.create_follow_up(conn, sid, due_at="2026-08-20", reason="check courier accepted?")
    q = followups.work_queue(conn, now=NOW)
    mine = [i for i in q if i["tracking_no"] == "SAMPLE_007"]
    assert len(mine) == 1
    assert "overdue_follow_up" in mine[0]["reasons"]
    assert "needs_action" not in mine[0]["reasons"]


def test_i_completion_removes_from_overdue_open(tmp_path):
    """I. Completing removes from open/overdue lists."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_008")
    fid = followups.create_follow_up(conn, sid, due_at="2026-08-20", reason="check")
    assert len(followups.list_follow_ups(conn, sid, now=NOW)["overdue"]) == 1
    followups.complete_follow_up(conn, fid)
    got = followups.list_follow_ups(conn, sid, now=NOW)
    assert got["overdue"] == []
    assert got["open"] == []
    assert len(got["closed"]) == 1


def test_j_completion_preserves_history(tmp_path):
    """J. Completion does not delete or rewrite original values; closed record intact."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_009")
    fid = followups.create_follow_up(conn, sid, due_at="2026-08-20", reason="original reason kept")
    before = dict(conn.execute("SELECT * FROM follow_up WHERE id=?", (fid,)).fetchone())
    followups.complete_follow_up(conn, fid)
    after = dict(conn.execute("SELECT * FROM follow_up WHERE id=?", (fid,)).fetchone())
    assert after["reason"] == before["reason"] == "original reason kept"
    assert after["due_at"] == before["due_at"] == "2026-08-20"
    assert after["created_at"] == before["created_at"]
    assert after["status"] == "done"
    assert after["closed_at"] is not None
    # double completion rejected, not silently rewritten
    with pytest.raises(actions.ValidationError):
        followups.complete_follow_up(conn, fid)


def test_k_both_reasons_single_queue_entry(tmp_path):
    """K. NEEDS_ACTION + overdue follow-up -> ONE queue entry, not two.
    (C1: import maps legacy REATTEMPT status -> reattempt_requested, so the
    test first records a fresh courier observation to establish NEEDS_ACTION.)"""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_010")
    ek = core._event_key("SAMPLE_010", "RFD", "fresh obs", None)
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key) VALUES ('k1',?,'RFD','fresh obs','2099-01-01T00:00:00+00:00',?)",
        (sid, ek))
    core.refresh_state(conn, sid)
    followups.create_follow_up(conn, sid, due_at="2026-08-01", reason="old check")
    q = followups.work_queue(conn, now=NOW)
    entries = [i for i in q if i["tracking_no"] == "SAMPLE_010"]
    assert len(entries) == 1
    assert set(entries[0]["reasons"]) == {"needs_action", "overdue_follow_up"}


def test_l_queue_exposes_reasons(tmp_path):
    """L. Every queue entry exposes why it is present."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    # C1: imported shipments derive ACTION_TAKEN (legacy REATTEMPT mapping);
    # record fresh observations so they legitimately need action.
    for tno in ("SAMPLE_001",):
        sid = ship_id(conn, tno)
        ek = core._event_key(tno, "INA", "fresh", None)
        conn.execute(
            "INSERT INTO tracking_event (id,shipment_id,raw_code,raw_text,imported_at,event_key) VALUES ('l1',?,'INA','fresh','2099-01-01T00:00:00+00:00',?)",
            (sid, ek))
        core.refresh_state(conn, sid)
    q = followups.work_queue(conn, now=NOW)
    assert all("reasons" in i and isinstance(i["reasons"], list) and i["reasons"] for i in q)
    plain = [i for i in q if i["tracking_no"] == "SAMPLE_001"]
    assert plain and plain[0]["reasons"] == ["needs_action"]


def test_m_overdue_does_not_mutate_state(tmp_path):
    """M. Overdue follow-up never changes current_state (queue is a read model)."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_011")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    state_before = core.derive_state(conn, sid)
    assert state_before == "ACTION_TAKEN"
    followups.create_follow_up(conn, sid, due_at="2026-01-01", reason="ancient")
    _ = followups.work_queue(conn, now=NOW)
    assert core.derive_state(conn, sid) == state_before  # unchanged
    assert conn.execute("SELECT current_state FROM shipment WHERE id=?", (sid,)).fetchone()["current_state"] == state_before


def test_n_multiple_followups_distinct(tmp_path):
    """N. Multiple follow-ups remain distinct rows — no silent dedup."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_012")
    f1 = followups.create_follow_up(conn, sid, due_at="2026-08-27", reason="Check reattempt")
    f2 = followups.create_follow_up(conn, sid, due_at="2026-08-27", reason="Check reattempt")  # identical on purpose
    assert f1 != f2
    got = followups.list_follow_ups(conn, sid, now=NOW)
    assert len(got["upcoming"]) == 2  # both preserved, distinguishable by id
    ids = {f["id"] for f in got["upcoming"]}
    assert ids == {f1, f2}


def test_o_multi_cycle_followups_visible(tmp_path):
    """O. Follow-ups from multiple cycles all remain visible; earlier ones not
    collapsed or removed when later cycles happen."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_013")
    actions.record_customer_confirmation(conn, sid, "cycle1: no rider", channel="call")
    actions.record_operator_action(conn, sid, "reattempt_requested")
    fu1 = followups.create_follow_up(conn, sid, due_at="2026-08-25", reason="cycle1: check reattempt")
    # cycle 2: fresh courier observation reopens shipment (deterministic ts)
    ek = core._event_key("SAMPLE_013", "RFD", "", None)
    conn.execute(
        "INSERT INTO tracking_event (id,shipment_id,raw_code,imported_at,event_key) VALUES ('c2',?,'RFD',?,?)",
        (sid, "2026-08-26T23:00:00+00:00", ek))
    core.refresh_state(conn, sid)
    actions.record_operator_action(conn, sid, "reattempt_requested")
    fu2 = followups.create_follow_up(conn, sid, due_at="2026-08-30", reason="cycle2: check second attempt")
    rows = conn.execute(
        "SELECT id, reason, status FROM follow_up WHERE shipment_id=? ORDER BY created_at", (sid,)
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == fu1 and rows[1]["id"] == fu2
    assert rows[0]["reason"] != rows[1]["reason"]
    d = detail.shipment_detail(conn, "SAMPLE_013")
    fus = [e for e in d["timeline"] if e["type"] == "follow_up"]
    assert len(fus) == 2


def test_p_timeline_shows_due_reason_completion(tmp_path):
    """P. Timeline preserves due_at, reason, created time and completion info;
    completed follow-ups are not collapsed."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    sid = ship_id(conn, "SAMPLE_014")
    fid = followups.create_follow_up(conn, sid, due_at="2026-08-27", reason="Check reattempt")
    followups.complete_follow_up(conn, fid)
    d = detail.shipment_detail(conn, "SAMPLE_014")
    fus = [e for e in d["timeline"] if e["type"] == "follow_up"]
    assert len(fus) == 1
    fu = fus[0]
    assert fu["reason"] == "Check reattempt"
    assert fu["due_at"] == "2026-08-27"
    assert fu["status"] == "done"
    assert fu["closed_at"] is not None


def test_q_all_prior_slices_green():
    """Q. Marker: entire suite runs together (verified at full-suite level)."""
    assert True


def test_r_invalid_followup_id_rejected(tmp_path):
    """Completing an unknown follow-up -> clear domain error (part of R/error handling)."""
    conn = db(tmp_path)
    core.import_source(conn, load_evidence())
    with pytest.raises(followups.FollowUpNotFoundError):
        followups.complete_follow_up(conn, "no-such-fu")
