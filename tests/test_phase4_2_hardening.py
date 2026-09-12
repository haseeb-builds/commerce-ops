"""Focused Phase 4.2 hardening regressions recovered from the review contract."""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
from commerceops import actions, autonomous_action, case, case_engine, core, followups, human_task


@pytest.fixture
def db(tmp_path):
    conn = core.connect(str(tmp_path / "ops.db"))
    core.import_source(conn, "tracking_no,postex_remark\nA,RFD")
    sid = conn.execute("SELECT id FROM shipment").fetchone()["id"]
    cid = case_engine.evaluate_shipment(conn, sid).case_id
    yield conn, sid, cid
    conn.close()


def test_import_cancel_without_reason_is_quarantined(db):
    conn, _, _ = db
    result = core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nB,RFD,,CANCEL")
    assert result["quarantined"] == 1
    assert conn.execute("SELECT COUNT(*) FROM operator_action WHERE kind='cancel_decided'").fetchone()[0] == 0


def test_direct_cancel_requires_reason(db):
    value = ""

    conn, sid, _ = db
    with pytest.raises(actions.ValidationError):
        actions.record_operator_action(conn, sid, "cancel_decided", cancel_reason=value)
    assert conn.execute("SELECT COUNT(*) FROM operator_action").fetchone()[0] == 0


def test_malformed_human_task_payload_is_visible_not_authoritative(db):
    bad = "{"
    conn, _, cid = db
    task = human_task.get_tasks_for_case(conn, cid)[0]
    conn.execute("UPDATE human_task SET payload=? WHERE id=?", (bad, task["id"]))
    conn.commit()
    loaded = human_task.get_human_task(conn, task["id"])
    assert loaded["payload"] is None and loaded["payload_error"]
    case_engine.evaluate_case(conn, cid)
    recovered = human_task.get_human_task(conn, task["id"])
    assert recovered["status"] == "PENDING"
    assert recovered["payload"]["evidence_refs"]


def test_malformed_autonomous_payload_is_read_safe(db):
    bad = "{"
    conn, _, cid = db
    aid = autonomous_action.create_autonomous_action(conn, cid, "review", {})
    conn.execute("UPDATE autonomous_action SET payload=? WHERE id=?", (bad, aid))
    conn.commit()
    assert autonomous_action.get_autonomous_action(conn, aid)["payload_error"]
    assert autonomous_action.get_actions_for_case(conn, cid)[0]["payload"] is None


def test_malformed_case_resolution_does_not_break_evaluation(db):
    conn, _, cid = db
    conn.execute("UPDATE case_entity SET resolution='{' WHERE id=?", (cid,))
    conn.commit()
    assert case_engine.evaluate_case(conn, cid).case_id == cid


def test_foreign_task_key_is_not_returned(db):
    conn, _, cid = db
    core.import_source(conn, "tracking_no,postex_remark\nB,RFD")
    sid2 = conn.execute("SELECT id FROM shipment WHERE tracking_no='B'").fetchone()["id"]
    cid2 = case_engine.evaluate_shipment(conn, sid2).case_id
    first = human_task.create_human_task(conn, cid, "DECIDE_ACTION", {}, idempotency_key="occupied")
    second = human_task.create_human_task(conn, cid2, "DECIDE_ACTION", {}, idempotency_key="occupied")
    assert first != second
    assert human_task.get_human_task(conn, second)["case_id"] == cid2


def test_followup_completion_synchronizes_linked_task(db):
    conn, sid, cid = db
    fid = followups.create_follow_up(conn, sid, "2030-01-01T00:00:00Z", "review")
    tid = human_task.create_human_task(conn, cid, "FOLLOW_UP_ACTION", {"follow_up_id": fid})
    conn.commit()
    followups.complete_follow_up(conn, fid, completed_at="2029-01-01T00:00:00Z")
    task = human_task.get_human_task(conn, tid)
    assert task["status"] == "COMPLETED"
    assert task["payload"]["synchronized_follow_up_completion"]["source"] == "follow_up"


def test_followup_timestamp_is_preserved(db):
    value = "2026-01-01T00:00:00Z"

    conn, sid, _ = db
    fid = followups.create_follow_up(conn, sid, value, "preserve")
    assert conn.execute("SELECT due_at FROM follow_up WHERE id=?", (fid,)).fetchone()[0] == value


def test_nonterminal_actions_do_not_claim_terminal_authority(db):
    kind = "note"
    conn = core.connect(":memory:")
    core.import_source(conn, "tracking_no,postex_remark\nA,RFD")
    sid = conn.execute("SELECT id FROM shipment").fetchone()["id"]
    actions.record_operator_action(conn, sid, kind, note="recorded" if kind == "note" else None)
    assert core.derive_state(conn, sid) == "ACTION_TAKEN"


def test_source_qualified_work_identity_is_stored(db):
    conn, _, cid = db
    task = human_task.get_tasks_for_case(conn, cid)[0]
    assert task["payload"]["evidence_refs"]
    assert all(":" in ref for ref in task["payload"]["evidence_refs"])


def test_task_status_rows_remain_readable(db):
    status = "IN_PROGRESS"
    conn, _, cid = db
    tid = human_task.get_tasks_for_case(conn, cid)[0]["id"]
    conn.execute("UPDATE human_task SET status=? WHERE id=?", (status, tid))
    conn.commit()
    assert human_task.get_human_task(conn, tid)["status"] == status


def test_malformed_followup_due_is_visible_not_overdue(db):
    conn, sid, _ = db
    fid = followups.create_follow_up(conn, sid, "2030-01-01", "legacy")
    conn.execute("UPDATE follow_up SET due_at='bad-date' WHERE id=?", (fid,))
    conn.commit()
    listing = followups.list_follow_ups(conn, shipment_id=sid, now="2031-01-01")
    assert listing["invalid"][0]["id"] == fid and not listing["overdue"]


def test_case_history_malformed_resolution_is_visible(db):
    conn, _, cid = db
    case.update_case_status(conn, cid, "RESOLVED")
    conn.execute("UPDATE case_status_event SET resolution='{' WHERE case_entity_id=?", (cid,))
    conn.commit()
    assert case.get_case_history(conn, cid)[0]["resolution_error"]


def test_task_collision_retry_is_stable(db):
    conn, _, cid = db
    a = human_task.create_human_task(conn, cid, "DECIDE_ACTION", {}, idempotency_key="foreign")
    b = human_task.create_human_task(conn, cid, "VERIFY_CUSTOMER", {}, idempotency_key="foreign")
    c = human_task.create_human_task(conn, cid, "VERIFY_CUSTOMER", {}, idempotency_key="foreign")
    assert b == c and a != b


def test_followup_bad_timestamp_is_atomic(db):
    bad = "bad"
    conn, sid, _ = db
    before = conn.execute("SELECT COUNT(*) FROM follow_up").fetchone()[0]
    with pytest.raises(actions.ValidationError):
        followups.create_follow_up(conn, sid, bad, "bad")
    assert conn.execute("SELECT COUNT(*) FROM follow_up").fetchone()[0] == before


def test_completion_does_not_rewrite_source_timestamp(db):
    conn, sid, _ = db
    value = "2026-01-01T00:00:00-05:00"
    actions.record_customer_confirmation(conn, sid, "reported", confirmed_at=value)
    assert conn.execute("SELECT confirmed_at FROM customer_confirmation").fetchone()[0] == value


def test_case_authority_reuses_existing_case(db):
    conn, sid, cid = db
    assert case_engine.evaluate_shipment(conn, sid).case_id == cid
    assert conn.execute("SELECT COUNT(*) FROM case_entity WHERE shipment_id=?", (sid,)).fetchone()[0] == 1


def test_queue_read_does_not_mutate_coordination(db):
    conn, sid, _ = db
    before = list(conn.iterdump())
    core.work_queue(conn)
    assert list(conn.iterdump()) == before


def test_malformed_task_cannot_complete(db):
    conn, sid, cid = db
    tid = human_task.get_tasks_for_case(conn, cid)[0]["id"]
    conn.execute("UPDATE human_task SET payload='{' WHERE id=?", (tid,))
    conn.commit()
    result = __import__("commerceops.operational", fromlist=["complete_human_task_with_evidence"]).complete_human_task_with_evidence(conn, tid, {"customer_response": "x"})
    assert not result["success"]


def test_followup_source_reference_is_stored(db):
    conn, sid, cid = db
    fid = followups.create_follow_up(conn, sid, "2030-01-01", "linked")
    tid = human_task.create_human_task(conn, cid, "FOLLOW_UP_ACTION", {"follow_up_id": fid})
    assert human_task.get_human_task(conn, tid)["payload"]["follow_up_id"] == fid


def test_unknown_import_status_is_quarantined(db):
    conn, _, _ = db
    result = core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nC,RFD,,UNKNOWN")
    assert result["quarantined"] == 1


def test_cancel_reason_verbatim_on_valid_import(db):
    conn, _, _ = db
    core.import_source(conn, "tracking_no,postex_remark,our_remark,status\nD,RFD,SELF COLLECT,CANCEL")
    row = conn.execute("SELECT cancel_reason FROM operator_action WHERE kind='cancel_decided'").fetchone()
    assert row[0] == "SELF COLLECT"


def test_empty_payload_not_mistaken_for_valid_work(db):
    conn, _, cid = db
    tid = human_task.create_human_task(conn, cid, "DECIDE_ACTION", {}, idempotency_key="empty")
    assert not case_engine.task_matches_work(human_task.get_human_task(conn, tid), {"disposition":"HUMAN_TASK_REQUIRED", "capability":"DECIDE_ACTION", "work_evidence_ids":["x"], "requirement_scope":None})


def test_malformed_history_preserves_raw_database_value(db):
    conn, _, cid = db
    case.update_case_status(conn, cid, "RESOLVED")
    conn.execute("UPDATE case_status_event SET resolution='raw-invalid' WHERE case_entity_id=?", (cid,))
    conn.commit()
    assert conn.execute("SELECT resolution FROM case_status_event WHERE case_entity_id=?", (cid,)).fetchone()[0] == "raw-invalid"


def test_invalid_completion_time_does_not_write_evidence(db):
    conn, sid, _ = db
    before = conn.execute("SELECT COUNT(*) FROM customer_confirmation").fetchone()[0]
    with pytest.raises(actions.ValidationError):
        actions.record_customer_confirmation(conn, sid, "x", confirmed_at="invalid")
    assert conn.execute("SELECT COUNT(*) FROM customer_confirmation").fetchone()[0] == before


def test_followup_completion_rejects_repeat(db):
    conn, sid, _ = db
    fid = followups.create_follow_up(conn, sid, "2030-01-01", "once")
    followups.complete_follow_up(conn, fid)
    with pytest.raises(actions.ValidationError):
        followups.complete_follow_up(conn, fid)


def test_cancelled_task_is_not_reused(db):
    conn, _, cid = db
    tid = human_task.get_tasks_for_case(conn, cid)[0]["id"]
    human_task.update_human_task_status(conn, tid, "CANCELLED")
    conn.commit()
    replacement = human_task.create_human_task(conn, cid, "VERIFY_CUSTOMER", {"evidence_ids":["new"]})
    assert replacement != tid


def test_historical_timestamps_are_not_normalized(db):
    conn, sid, _ = db
    raw = "2026-01-01T00:00:00+05:00"
    actions.record_operator_action(conn, sid, "note", note="raw", acted_at=raw)
    assert conn.execute("SELECT acted_at FROM operator_action WHERE note='raw'").fetchone()[0] == raw


def test_phase42_read_models_tolerate_damaged_rows(db):
    conn, sid, cid = db
    conn.execute("UPDATE human_task SET payload='not-json' WHERE case_entity_id=?", (cid,))
    conn.commit()
    assert isinstance(core.work_queue(conn), list)
    assert followups.list_follow_ups(conn, shipment_id=sid)["open"] == []
