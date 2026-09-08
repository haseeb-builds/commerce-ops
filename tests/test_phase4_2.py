"""Phase 4.2: evidence-cycle work reconciliation and atomic operational writes."""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
from commerceops import actions, case, case_engine, core, followups, human_task, operational, policy


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {"now": "2026-06-15T08:00:00+00:00"}
    for module in (core, actions, case, human_task, followups):
        monkeypatch.setattr(module, "utcnow", lambda: clock["now"])
    path = str(tmp_path / "ops.db")
    conn = core.connect(path)
    core.import_source(conn, "tracking_no,postex_remark\nCYCLE,RFD")
    sid = conn.execute("SELECT id FROM shipment").fetchone()["id"]
    cid = case_engine.evaluate_shipment(conn, sid).case_id
    yield conn, sid, cid, clock, path
    conn.close()


def pending(conn, cid):
    return [t for t in human_task.get_tasks_for_case(conn, cid) if t["status"] == "PENDING"]


def observation(conn, sid, clock, at, code="RFD", occurred_at=None):
    """Append distinct source evidence; same schema contract as existing Phase 4 tests."""
    clock["now"] = at
    eid = f"courier-{at}-{code}"
    conn.execute(
        "INSERT INTO tracking_event (id, shipment_id, raw_code, raw_text, occurred_at, imported_at, event_key) "
        "VALUES (?,?,?,?,?,?,?)",
        (eid, sid, code, "Distinct courier observation", occurred_at, at, eid),
    )
    core.refresh_state(conn, sid)
    conn.commit()
    return eid


def complete(conn, tid, **result):
    response = operational.complete_human_task_with_evidence(conn, tid, result)
    assert response["success"], response["error"]
    return response


def decision_task(conn, cid):
    complete(conn, pending(conn, cid)[0]["id"], customer_response="I did not refuse")
    return pending(conn, cid)[0]["id"]


def test_three_full_cycles_on_one_case(env):
    conn, sid, cid, clock, _ = env
    for cycle in range(3):
        if cycle:
            observation(conn, sid, clock, f"2026-06-{15 + cycle}T08:00:00+00:00")
            assert case_engine.evaluate_shipment(conn, sid).case_id == cid
        task = pending(conn, cid)[0]
        assert task["type"] == "VERIFY_CUSTOMER"
        response = complete(conn, task["id"], customer_response="I did not refuse the parcel")
        assert response["next_evaluation"].capability == "DECIDE_ACTION"
        task = pending(conn, cid)[0]
        response = complete(conn, task["id"], decision_type="reattempt_requested")
        assert response["next_evaluation"].disposition == "MONITOR"
        assert case_engine.evaluate_case(conn, cid).disposition == "MONITOR"
        assert pending(conn, cid) == []
    assert conn.execute("SELECT COUNT(*) FROM case_entity").fetchone()[0] == 1
    assert len(human_task.get_tasks_for_case(conn, cid)) == 6
    assert len(actions.list_customer_confirmations(conn, sid)) == 3
    assert len(actions.list_operator_actions(conn, sid)) == 3
    assert case.get_case(conn, cid)["status"] == "OPEN"


@pytest.mark.parametrize("stage", ["VERIFY_CUSTOMER", "DECIDE_ACTION", "MONITOR"])
def test_notes_followups_and_unchanged_evidence_do_not_create_work(env, stage):
    conn, sid, cid, _, _ = env
    if stage != "VERIFY_CUSTOMER":
        tid = decision_task(conn, cid)
        if stage == "MONITOR":
            complete(conn, tid, decision_type="reattempt_requested")
    before = human_task.get_tasks_for_case(conn, cid)
    actions.record_operator_action(conn, sid, "note", note="Operator interpretation, not a decision")
    followups.create_follow_up(conn, sid, "2026-06-20", "Check manually")
    for _ in range(3):
        result = case_engine.evaluate_case(conn, cid)
    assert (result.capability or result.disposition) == stage
    assert human_task.get_tasks_for_case(conn, cid) == before


def test_new_observation_supersedes_pending_and_in_progress_work(env):
    conn, sid, cid, clock, _ = env
    old = pending(conn, cid)[0]
    human_task.update_human_task_status(conn, old["id"], "IN_PROGRESS")
    eid = observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    case_engine.evaluate_case(conn, cid)
    new = pending(conn, cid)[0]
    assert new["payload"]["evidence_ids"] == [eid]
    retired = human_task.get_human_task(conn, old["id"])
    assert retired["status"] == "CANCELLED"
    assert retired["payload"]["evidence_ids"] == old["payload"]["evidence_ids"]
    assert retired["payload"]["superseded"]["replacement_task_id"] == new["id"]
    assert retired["payload"]["superseded"]["reason"]
    case_engine.evaluate_case(conn, cid)
    assert len(human_task.get_tasks_for_case(conn, cid)) == 2


def test_external_confirmation_and_contradictory_response_replace_work(env):
    conn, sid, cid, _, _ = env
    first = pending(conn, cid)[0]["id"]
    actions.record_customer_confirmation(conn, sid, "I refused the parcel")
    case_engine.evaluate_case(conn, cid)
    old_decision = pending(conn, cid)[0]["id"]
    assert human_task.get_human_task(conn, first)["status"] == "CANCELLED"
    actions.record_customer_confirmation(conn, sid, "I did not refuse")
    result = case_engine.evaluate_case(conn, cid)
    assert "contradiction" in result.reason
    assert human_task.get_human_task(conn, old_decision)["status"] == "CANCELLED"
    assert len(pending(conn, cid)) == 1
    assert pending(conn, cid)[0]["id"] != old_decision
    assert len(actions.list_customer_confirmations(conn, sid)) == 2


@pytest.mark.parametrize("change", ["courier", "customer"])
def test_stale_decision_rejected_even_before_re_evaluation(env, change):
    conn, sid, cid, clock, _ = env
    tid = decision_task(conn, cid)
    if change == "courier":
        observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    else:
        actions.record_customer_confirmation(conn, sid, "I refused the parcel")
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, tid, {"decision_type": "reattempt_requested"})
    assert not result["success"] and "stale" in result["error"]
    assert list(conn.iterdump()) == before
    case_engine.evaluate_case(conn, cid)
    assert human_task.get_human_task(conn, tid)["status"] == "CANCELLED"


def test_irrelevant_latest_response_requires_new_verification_not_old_decision(env):
    conn, sid, cid, _, _ = env
    tid = decision_task(conn, cid)
    actions.record_customer_confirmation(conn, sid, "Please call later")
    assert case_engine.evaluate_case(conn, cid).capability == "VERIFY_CUSTOMER"
    assert human_task.get_human_task(conn, tid)["status"] == "CANCELLED"
    assert len(pending(conn, cid)) == 1


def test_late_import_uses_receipt_time_and_default_response_follows_it(env):
    conn, sid, cid, clock, _ = env
    tid = decision_task(conn, cid)
    complete(conn, tid, decision_type="reattempt_requested")
    received = "2026-06-16T08:00:00+00:00"
    observation(conn, sid, clock, received, occurred_at="2026-06-14T01:00:00+00:00")
    result = case_engine.evaluate_case(conn, cid)
    assert result.capability == "VERIFY_CUSTOMER"
    assert case.get_case(conn, cid)["latest_evidence_at"] == received
    response = complete(conn, pending(conn, cid)[0]["id"], customer_response="I did not refuse")
    assert response["next_evaluation"].capability == "DECIDE_ACTION"
    assert actions.list_customer_confirmations(conn, sid)[-1]["confirmed_at"] > received


def test_policy_compares_timezones_without_rewriting_evidence(env):
    conn, sid, cid, _, _ = env
    content = "  I did not refuse the parcel.\n"
    # 13:30 +05 is 08:30 UTC, after the 08:00 UTC courier observation.
    complete(conn, pending(conn, cid)[0]["id"], customer_response=content,
             verified_at="2026-06-15T13:30:00+05:00")
    assert pending(conn, cid)[0]["type"] == "DECIDE_ACTION"
    stored = actions.list_customer_confirmations(conn, sid)[0]
    assert stored["content"] == content
    assert stored["confirmed_at"] == "2026-06-15T13:30:00+05:00"


@pytest.mark.parametrize("stage", ["verify", "decide"])
def test_backdated_task_completion_rejected_without_evidence(env, stage):
    conn, _, cid, _, _ = env
    if stage == "verify":
        tid = pending(conn, cid)[0]["id"]
        payload = {"customer_response": "I did not refuse", "verified_at": "2026-06-15T08:00:00+00:00"}
    else:
        tid = decision_task(conn, cid)
        payload = {"decision_type": "reattempt_requested", "decided_at": "2026-06-15T08:00:00+00:00"}
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, tid, payload)
    assert not result["success"] and "later" in result["error"]
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("stage", ["verify", "decide", "follow_up"])
def test_failure_after_evidence_write_rolls_back_entire_operation(env, monkeypatch, nested, stage):
    conn, sid, cid, _, path = env
    if stage == "verify":
        tid = pending(conn, cid)[0]["id"]
        payload = {"customer_response": "I did not refuse"}
    elif stage == "decide":
        tid = decision_task(conn, cid)
        payload = {"decision_type": "reattempt_requested"}
    else:
        fid = followups.create_follow_up(conn, sid, "2026-06-15", "Check response")
        tid = human_task.create_human_task(conn, cid, "FOLLOW_UP_ACTION", {"follow_up_id": fid})
        conn.commit()
        payload = {"completion_notes": "Checked", "completed_at": "2026-06-15T09:00:00+00:00"}
    if nested:
        conn.execute("BEGIN")
        actions.record_operator_action(conn, sid, "note", note="Caller-owned unrelated work")
    before = list(conn.iterdump())
    def fail(*args, **kwargs):
        raise RuntimeError("failure after authoritative evidence insert")
    monkeypatch.setattr(case_engine, "evaluate_case", fail)
    result = operational.complete_human_task_with_evidence(conn, tid, payload)
    assert not result["success"] and "failure after" in result["error"]
    assert conn.in_transaction is nested
    assert list(conn.iterdump()) == before
    assert human_task.get_human_task(conn, tid)["status"] == "PENDING"
    if nested:
        conn.commit()
    other = core.connect(path)
    try:
        assert human_task.get_human_task(other, tid)["status"] == "PENDING"
        if stage == "follow_up":
            assert other.execute("SELECT status FROM follow_up WHERE id=?", (fid,)).fetchone()[0] == "open"
    finally:
        other.close()


def test_success_inside_caller_transaction_does_not_commit_it(env):
    conn, sid, cid, _, path = env
    tid = pending(conn, cid)[0]["id"]
    conn.execute("BEGIN")
    complete(conn, tid, customer_response="I did not refuse")
    assert conn.in_transaction
    other = core.connect(path)
    try:
        assert actions.list_customer_confirmations(other, sid) == []
        assert human_task.get_human_task(other, tid)["status"] == "PENDING"
    finally:
        other.close()
    conn.rollback()
    assert human_task.get_human_task(conn, tid)["status"] == "PENDING"
    assert actions.list_customer_confirmations(conn, sid) == []


def test_reconciliation_failure_rolls_back_replacement_and_cancellation(env, monkeypatch):
    conn, sid, cid, clock, _ = env
    observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    before = list(conn.iterdump())
    original = human_task.supersede_human_task
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("supersession failed")
    monkeypatch.setattr(human_task, "supersede_human_task", fail)
    with pytest.raises(RuntimeError, match="supersession failed"):
        case_engine.evaluate_case(conn, cid)
    assert list(conn.iterdump()) == before


def test_legacy_unbound_task_is_adopted_without_duplicate(env):
    conn, _, cid, _, _ = env
    existing = pending(conn, cid)[0]
    conn.execute("UPDATE human_task SET payload=? WHERE id=?", (json.dumps({"legacy": "keep"}), existing["id"]))
    conn.commit()
    case_engine.evaluate_case(conn, cid)
    tasks = pending(conn, cid)
    assert len(tasks) == 1 and tasks[0]["id"] == existing["id"]
    assert tasks[0]["payload"]["legacy"] == "keep"
    assert tasks[0]["payload"]["evidence_ids"] == existing["payload"]["evidence_ids"]


def test_non_open_case_rejects_completion_and_retires_work_without_new_case(env):
    conn, sid, cid, _, _ = env
    tid = pending(conn, cid)[0]["id"]
    case.update_case_status(conn, cid, "RESOLVED")
    conn.commit()
    result = operational.complete_human_task_with_evidence(conn, tid, {"customer_response": "I refused"})
    assert not result["success"]
    assert case_engine.evaluate_shipment(conn, sid).case_id == cid
    assert pending(conn, cid) == []
    assert conn.execute("SELECT COUNT(*) FROM case_entity").fetchone()[0] == 1


def test_follow_up_link_comes_from_stored_task_not_submission(env):
    conn, sid, cid, _, _ = env
    fid = followups.create_follow_up(conn, sid, "2026-06-15", "Original")
    other = followups.create_follow_up(conn, sid, "2026-06-15", "Other")
    tid = human_task.create_human_task(conn, cid, "FOLLOW_UP_ACTION", {"follow_up_id": fid})
    conn.commit()
    complete(conn, tid, completion_notes="Checked", original_task_payload={"follow_up_id": other})
    statuses = dict(conn.execute("SELECT id, status FROM follow_up"))
    assert statuses == {fid: "done", other: "open"}


def test_concurrent_completion_records_evidence_once(env):
    conn, sid, cid, _, path = env
    tid = pending(conn, cid)[0]["id"]
    barrier = Barrier(2)
    def run():
        worker = core.connect(path)
        try:
            barrier.wait(timeout=10)
            return operational.complete_human_task_with_evidence(worker, tid, {"customer_response": "I did not refuse"})
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(r["success"] for r in results) == [False, True]
    assert "not PENDING" in next(r["error"] for r in results if not r["success"])
    assert len(actions.list_customer_confirmations(conn, sid)) == 1
    assert len(pending(conn, cid)) == 1


def test_concurrent_initial_evaluation_creates_one_case_and_task(env):
    conn, _, _, _, path = env
    core.import_source(conn, "tracking_no,postex_remark\nSECOND,RFD")
    sid = conn.execute("SELECT id FROM shipment WHERE tracking_no='SECOND'").fetchone()[0]
    barrier = Barrier(2)
    def run():
        worker = core.connect(path)
        try:
            barrier.wait(timeout=10)
            return case_engine.evaluate_shipment(worker, sid).case_id
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: run(), range(2)))
    assert ids[0] == ids[1]
    assert len(pending(conn, ids[0])) == 1
    assert conn.execute("SELECT COUNT(*) FROM case_entity WHERE shipment_id=?", (sid,)).fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["note", "query_sent", "open_allowed"])
def test_non_decision_actions_cannot_falsely_complete_decision_work(env, kind):
    conn, sid, cid, _, _ = env
    tid = decision_task(conn, cid)
    result = operational.complete_human_task_with_evidence(
        conn, tid, {"decision_type": kind, "notes": "Not a decision"})
    assert not result["success"]
    assert human_task.get_human_task(conn, tid)["status"] == "PENDING"
    assert actions.list_operator_actions(conn, sid) == []


def test_default_timestamp_is_after_evidence_with_different_offsets(env):
    conn, sid, cid, clock, _ = env
    # 04:00 -05 is 09:00 UTC: later in real time, earlier lexicographically.
    complete(conn, pending(conn, cid)[0]["id"], customer_response="I did not refuse",
             verified_at="2026-06-15T04:00:00-05:00")
    clock["now"] = "2026-06-15T08:30:00+00:00"
    tid = pending(conn, cid)[0]["id"]
    result = complete(conn, tid, decision_type="reattempt_requested")
    assert result["next_evaluation"].disposition == "MONITOR"
    action = actions.list_operator_actions(conn, sid)[0]
    assert policy.evidence_timestamp(policy.Evidence(action["id"], "operator_action", dict(action))) > "2026-06-15T09:00:00+00:00"


def test_timezone_ordering_agrees_across_policy_state_and_timeline(env):
    from commerceops import detail
    conn, sid, cid, clock, _ = env
    complete(conn, pending(conn, cid)[0]["id"], customer_response="I did not refuse",
             verified_at="2026-06-15T13:30:00+05:00")
    clock["now"] = "2026-06-15T08:00:00+00:00"
    result = complete(conn, pending(conn, cid)[0]["id"], decision_type="delivered_confirmed")
    assert result["next_evaluation"].disposition == "MONITOR"
    assert core.derive_state(conn, sid) == "CLOSED_DELIVERED"
    assert detail.build_timeline(conn, sid)[-1]["kind"] == "delivered_confirmed"


@pytest.mark.parametrize("source", ["courier", "customer"])
def test_equal_timestamp_latest_source_record_uses_append_order_not_uuid(env, source):
    conn, sid, cid, _, _ = env
    if source == "courier":
        old = pending(conn, cid)[0]["id"]
        conn.execute(
            "INSERT INTO tracking_event(id, shipment_id, raw_code, imported_at, event_key) VALUES (?,?,?,?,?)",
            ("000-new-courier", sid, "RFD", "2026-06-15T08:00:00+00:00", "another-distinct-source-event"))
        expected_id = "000-new-courier"
    else:
        actions.record_customer_confirmation(conn, sid, "I refused", confirmed_at="2026-06-15T09:00:00+00:00")
        case_engine.evaluate_case(conn, cid)
        old = pending(conn, cid)[0]["id"]
        conn.execute(
            "INSERT INTO customer_confirmation(id, shipment_id, content, confirmed_at) VALUES (?,?,?,?)",
            ("000-new-response", sid, "Call me later", "2026-06-15T09:00:00+00:00"))
        expected_id = "000-new-response"
    conn.commit()
    assert case_engine.evaluate_case(conn, cid).capability == "VERIFY_CUSTOMER"
    assert expected_id in pending(conn, cid)[0]["payload"]["evidence_ids"]
    assert human_task.get_human_task(conn, old)["status"] == "CANCELLED"


@pytest.mark.parametrize("writer", ["action", "confirmation", "follow_up", "import"])
def test_nested_writers_leave_commit_ownership_with_caller(env, writer):
    conn, sid, _, _, _ = env
    before = list(conn.iterdump())
    conn.execute("BEGIN")
    if writer == "action":
        actions.record_operator_action(conn, sid, "note", note="Not committed yet")
    elif writer == "confirmation":
        actions.record_customer_confirmation(conn, sid, "Not committed yet")
    elif writer == "follow_up":
        followups.create_follow_up(conn, sid, "2026-06-20", "Not committed yet")
    else:
        # Explicit protection for the Phase 4.1 core.py SAVEPOINT contract.
        core.import_source(conn, "tracking_no,postex_remark\nNESTED,RFD")
    assert conn.in_transaction
    conn.rollback()
    assert list(conn.iterdump()) == before


def test_terminal_timestamp_validation_uses_instant_not_offset_string(env):
    from commerceops import outcomes
    conn, sid, _, _, _ = env
    actions.record_customer_confirmation(conn, sid, "Customer response", confirmed_at="2026-06-15T13:30:00+05:00")
    with pytest.raises(actions.ValidationError, match="predates"):
        outcomes.mark_delivered(conn, sid, acted_at="2026-06-15T08:15:00+00:00")
    outcomes.mark_delivered(conn, sid, acted_at="2026-06-15T08:45:00+00:00")
    assert core.derive_state(conn, sid) == "CLOSED_DELIVERED"


def test_follow_up_task_cannot_close_another_shipments_record(env):
    conn, _, cid, _, _ = env
    core.import_source(conn, "tracking_no,postex_remark\nOTHER,RFD")
    other_sid = conn.execute("SELECT id FROM shipment WHERE tracking_no='OTHER'").fetchone()[0]
    fid = followups.create_follow_up(conn, other_sid, "2026-06-20", "Other shipment")
    tid = human_task.create_human_task(conn, cid, "FOLLOW_UP_ACTION", {"follow_up_id": fid})
    conn.commit()
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, tid, {"completion_notes": "Attempted"})
    assert not result["success"] and "does not belong" in result["error"]
    assert list(conn.iterdump()) == before
