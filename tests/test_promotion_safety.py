"""Promotion gates reproduced from the adversarial Phase 4.2 review."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from commerceops import actions, capability, case, case_engine, core, detail, human_task, operational, outcomes
from commerceops_ui import app as ui
from test_phase4_2 import env, pending, decision_task, observation, complete


@pytest.mark.parametrize("stage", ["verify", "decide"])
@pytest.mark.parametrize("code", ["INA", "UNRECOGNIZED"])
def test_unclassified_evidence_replaces_not_discharges_work(env, stage, code):
    conn, sid, cid, clock, _ = env
    old = pending(conn, cid)[0]["id"] if stage == "verify" else decision_task(conn, cid)
    eid = observation(conn, sid, clock, "2026-06-16T08:00:00+00:00", code=code)
    result = case_engine.evaluate_case(conn, cid)
    assert result.disposition == "HUMAN_TASK_REQUIRED"
    assert result.capability == "DECIDE_ACTION"
    assert "review" in result.reason.lower()
    work = pending(conn, cid)
    assert len(work) == 1 and work[0]["id"] != old
    assert eid in work[0]["payload"]["evidence_ids"]
    assert human_task.get_human_task(conn, old)["payload"]["superseded"]["replacement_task_id"] == work[0]["id"]
    for _ in range(3):
        case_engine.evaluate_case(conn, cid)
    assert pending(conn, cid) == work
    stale = operational.complete_human_task_with_evidence(conn, old, {"customer_response": "I refused", "decision_type": "reattempt_requested"})
    assert not stale["success"]
    complete(conn, work[0]["id"], decision_type="reattempt_requested")
    assert case_engine.evaluate_case(conn, cid).disposition == "MONITOR"


def test_cancelled_requirement_reopens_with_fresh_id_and_stable_retries(env):
    conn, _, cid, _, _ = env
    ids = [pending(conn, cid)[0]["id"]]
    for _ in range(2):
        case.update_case_status(conn, cid, "RESOLVED")
        conn.commit()
        case_engine.evaluate_case(conn, cid)
        assert human_task.get_human_task(conn, ids[-1])["status"] == "CANCELLED"
        case.update_case_status(conn, cid, "OPEN")
        conn.commit()
        for _ in range(3):
            assert case_engine.evaluate_case(conn, cid).capability == "VERIFY_CUSTOMER"
        work = pending(conn, cid)
        assert len(work) == 1 and work[0]["id"] not in ids
        ids.append(work[0]["id"])
    assert len(human_task.get_tasks_for_case(conn, cid)) == 3
    assert len(set(ids)) == 3


@pytest.mark.parametrize("new_response", [False, True])
def test_truly_unbound_task_cannot_complete_or_be_retroactively_adopted(env, new_response):
    conn, sid, cid, _, _ = env
    decision_task(conn, cid)
    tid = human_task.create_human_task(conn, cid, "DECIDE_ACTION", {}, idempotency_key="legacy-unbound")
    conn.commit()
    if new_response:
        actions.record_customer_confirmation(conn, sid, "I refused the parcel")
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, tid, {"decision_type": "reattempt_requested"})
    assert not result["success"]
    assert list(conn.iterdump()) == before
    case_engine.evaluate_case(conn, cid)
    assert human_task.get_human_task(conn, tid)["status"] == "CANCELLED"
    assert "evidence_ids" not in human_task.get_human_task(conn, tid)["payload"]
    assert len(pending(conn, cid)) == 1


def test_capability_requests_bind_evidence_at_creation_not_completion(env):
    conn, sid, cid, clock, _ = env
    tid = capability.execute_capability(conn, cid, "VERIFY_CUSTOMER", {"channel": "call"})
    conn.commit()
    assert human_task.get_human_task(conn, tid)["payload"]["evidence_ids"]
    observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    result = operational.complete_human_task_with_evidence(conn, tid, {"customer_response": "I refused"})
    assert not result["success"]


@pytest.mark.parametrize("kind", ["delivered_confirmed", "cancel_decided", "returned_confirmed"])
def test_terminal_task_rejects_timestamp_older_than_excluded_note(env, kind):
    conn, sid, cid, _, _ = env
    tid = decision_task(conn, cid)
    actions.record_operator_action(conn, sid, "note", note="Later note", acted_at="2026-06-15T11:00:00+00:00")
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, tid, {
        "decision_type": kind, "decided_at": "2026-06-15T10:00:00+00:00",
        "cancel_reason": "Customer does not want it", "confirm_cancel": True,
    })
    assert not result["success"] and "predates" in result["error"]
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("kind,state", [("delivered_confirmed", "CLOSED_DELIVERED"), ("cancel_decided", "CLOSED_CANCELLED"), ("returned_confirmed", "RETURNED")])
def test_terminal_task_default_time_produces_real_terminal_state(env, kind, state):
    conn, sid, cid, _, _ = env
    tid = decision_task(conn, cid)
    actions.record_operator_action(conn, sid, "note", note="Later note", acted_at="2026-06-15T11:00:00+00:00")
    result = complete(conn, tid, decision_type=kind, cancel_reason="Customer choice", confirm_cancel=True)
    assert core.derive_state(conn, sid) == state
    assert result["next_evaluation"].disposition == "MONITOR"


def test_terminal_validation_and_insert_share_writer_reservation(env, monkeypatch):
    conn, sid, _, _, path = env
    competitor = core.connect(path)
    competitor.execute("PRAGMA busy_timeout=0")
    real = outcomes._validate_terminal_timestamp
    def validate(*args, **kwargs):
        assert conn.in_transaction
        real(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            actions.record_operator_action(competitor, sid, "note", note="Concurrent note")
    monkeypatch.setattr(outcomes, "_validate_terminal_timestamp", validate)
    try:
        outcomes.mark_delivered(conn, sid)
        assert core.derive_state(conn, sid) == "CLOSED_DELIVERED"
    finally:
        competitor.close()


@pytest.mark.parametrize("received", ["2026-06-15T08:00:00+00:00", "2026-06-14T08:00:00+00:00"])
def test_resolved_case_reopens_for_new_record_even_equal_or_backdated_time(env, received):
    conn, sid, cid, clock, _ = env
    case.set_case_resolution(conn, cid, {"outcome": "manual resolution"})
    case.update_case_status(conn, cid, "RESOLVED")
    conn.commit()
    case_engine.evaluate_case(conn, cid)
    assert case.get_case(conn, cid)["status"] == "RESOLVED"
    eid = observation(conn, sid, clock, received)
    result = case_engine.evaluate_shipment(conn, sid)
    assert result.case_id == cid and result.disposition == "HUMAN_TASK_REQUIRED"
    assert case.get_case(conn, cid)["status"] == "OPEN"
    assert eid in result.evidence_ids
    history = case.get_case_history(conn, cid)
    assert any(h["to_status"] == "OPEN" and h["from_status"] == "RESOLVED" for h in history)
    assert any(h["resolution"] == {"outcome": "manual resolution"} for h in history)
    assert case.get_case(conn, cid)["resolution"] is None
    count = len(history)
    case_engine.evaluate_case(conn, cid)
    assert len(case.get_case_history(conn, cid)) == count
    assert conn.execute("SELECT COUNT(*) FROM case_entity").fetchone()[0] == 1


def test_abandoned_is_not_automatically_reopened(env):
    conn, sid, cid, clock, _ = env
    case.update_case_status(conn, cid, "ABANDONED")
    conn.commit()
    observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    result = case_engine.evaluate_shipment(conn, sid)
    assert result.disposition == "MONITOR" and "ABANDONED" in result.reason
    assert case.get_case(conn, cid)["status"] == "ABANDONED"
    assert pending(conn, cid) == []


def test_malformed_legacy_timestamp_is_preserved_visible_and_fail_closed(env):
    conn, sid, cid, _, path = env
    conn.execute("INSERT INTO operator_action(id,shipment_id,kind,note,acted_at) VALUES ('bad',?,'note','Legacy note','bad-time')", (sid,))
    conn.commit()
    assert core.derive_state(conn, sid) == "NEEDS_ACTION"
    d = detail.shipment_detail(conn, "CYCLE")
    assert d["timestamp_issues"] and any(e["at"] == "bad-time" for e in d["timeline"])
    assert "timestamp_review" in core.work_queue(conn)[0]["reasons"]
    case_engine.evaluate_case(conn, cid)
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, pending(conn, cid)[0]["id"], {"decision_type": "delivered_confirmed"})
    assert not result["success"] and "timestamp" in result["error"].lower()
    assert list(conn.iterdump()) == before
    with pytest.raises(actions.ValidationError, match="timestamp"):
        outcomes.mark_delivered(conn, sid)
    assert conn.execute("SELECT acted_at FROM operator_action WHERE id='bad'").fetchone()[0] == "bad-time"


@pytest.mark.parametrize("value", ["bad-time", "", "2026-99-99T00:00:00Z"])
def test_new_malformed_timestamp_is_validation_error_and_atomic(env, value):
    conn, sid, _, _, _ = env
    before = list(conn.iterdump())
    with pytest.raises(actions.ValidationError, match="timestamp"):
        actions.record_customer_confirmation(conn, sid, "Exact response", confirmed_at=value)
    assert list(conn.iterdump()) == before


def test_current_in_progress_work_is_visible_and_completable(env, monkeypatch):
    conn, _, cid, _, path = env
    tid = pending(conn, cid)[0]["id"]
    human_task.update_human_task_status(conn, tid, "IN_PROGRESS")
    conn.commit()
    case_engine.evaluate_case(conn, cid)
    assert tid in [t["id"] for t in operational.get_pending_human_tasks_with_context(conn)]
    monkeypatch.setattr(ui, "DB_PATH", path)
    with TestClient(ui.app) as client:
        assert 'name="customer_response"' in client.get(f"/task/{tid}").text
        assert tid in client.get("/tasks/pending").text
        response = client.post(f"/tasks/{tid}/complete", data={"customer_response": "I did not refuse", "verification_method": "call"})
        assert response.status_code == 200
    assert human_task.get_human_task(conn, tid)["status"] == "COMPLETED"


@pytest.mark.parametrize("confirmation", [None, False, "no", "false", 1, 1.0, "true"])
def test_json_cancellation_requires_explicit_confirmation(env, monkeypatch, confirmation):
    conn, _, cid, _, path = env
    tid = decision_task(conn, cid)
    monkeypatch.setattr(ui, "DB_PATH", path)
    payload = {"decision_type": " cancel_decided ", "cancel_reason": "Customer choice"}
    if confirmation is not None:
        payload["confirm_cancel"] = confirmation
    before = list(conn.iterdump())
    with TestClient(ui.app) as client:
        response = client.post(f"/tasks/{tid}/complete", data={"completion_result": json.dumps(payload)})
        assert "confirmation is required" in response.text
    assert list(conn.iterdump()) == before


def test_json_cancellation_with_true_confirmation_succeeds(env, monkeypatch):
    conn, sid, cid, _, path = env
    tid = decision_task(conn, cid)
    monkeypatch.setattr(ui, "DB_PATH", path)
    with TestClient(ui.app) as client:
        response = client.post(f"/tasks/{tid}/complete", data={"completion_result": json.dumps({"decision_type": "cancel_decided", "cancel_reason": "Customer choice", "confirm_cancel": True})})
        assert "MONITOR" in response.text
    assert core.derive_state(conn, sid) == "CLOSED_CANCELLED"


def test_completed_work_does_not_suppress_explicitly_reopened_requirement(env):
    conn, _, cid, _, _ = env
    original_verify = pending(conn, cid)[0]["id"]
    decision = decision_task(conn, cid)
    complete(conn, decision, decision_type="reattempt_requested")
    case.update_case_status(conn, cid, "RESOLVED")
    case.update_case_status(conn, cid, "OPEN")
    conn.commit()
    result = case_engine.evaluate_case(conn, cid)
    assert result.disposition == "HUMAN_TASK_REQUIRED"
    work = pending(conn, cid)
    assert len(work) == 1 and work[0]["id"] != original_verify
    assert human_task.get_human_task(conn, original_verify)["status"] == "COMPLETED"
    assert case_engine.evaluate_case(conn, cid).capability == work[0]["type"]
    assert len(pending(conn, cid)) == 1
    complete(conn, work[0]["id"], customer_response="I did not refuse")
    assert pending(conn, cid)[0]["type"] == "DECIDE_ACTION"


def test_manual_reopen_invalidates_old_pending_task_even_without_intermediate_evaluation(env):
    conn, _, cid, _, _ = env
    tid = pending(conn, cid)[0]["id"]
    case.update_case_status(conn, cid, "ABANDONED")
    case.update_case_status(conn, cid, "OPEN")
    conn.commit()
    result = operational.complete_human_task_with_evidence(conn, tid, {"customer_response": "I refused"})
    assert not result["success"]
    case_engine.evaluate_case(conn, cid)
    assert pending(conn, cid)[0]["id"] != tid


def test_concurrent_recreation_of_cancelled_task_converges(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    conn, _, cid, _, path = env
    old = pending(conn, cid)[0]
    human_task.update_human_task_status(conn, old["id"], "CANCELLED")
    conn.commit()
    barrier = Barrier(2)
    def request():
        worker = core.connect(path)
        try:
            barrier.wait(timeout=10)
            return human_task.create_human_task(worker, cid, old["type"], old["payload"])
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(), range(2)))
    assert results[0] == results[1] != old["id"]
    assert len(pending(conn, cid)) == 1
    assert human_task.get_human_task(conn, old["id"])["status"] == "CANCELLED"
    assert pending(conn, cid)[0]["payload"]["replaces_cancelled_task_id"] == old["id"]


def test_concurrent_resolved_case_reopening_is_once_and_atomic(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    conn, sid, cid, clock, path = env
    case.update_case_status(conn, cid, "RESOLVED")
    observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    barrier = Barrier(2)
    def evaluate():
        worker = core.connect(path)
        try:
            barrier.wait(timeout=10)
            return case_engine.evaluate_shipment(worker, sid).case_id
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: evaluate(), range(2))) == [cid, cid]
    assert len(pending(conn, cid)) == 1
    assert [h["to_status"] for h in case.get_case_history(conn, cid)] == ["RESOLVED", "OPEN"]


@pytest.mark.parametrize("nested", [False, True])
def test_failed_reopening_rolls_back_status_audit_tasks_and_metadata(env, monkeypatch, nested):
    conn, sid, cid, clock, _ = env
    case.update_case_status(conn, cid, "RESOLVED")
    observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    if nested:
        conn.execute("BEGIN")
        actions.record_operator_action(conn, sid, "note", note="Caller-owned note")
    before = list(conn.iterdump())
    original = capability.execute_capability
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Failure after new task insertion")
    monkeypatch.setattr(capability, "execute_capability", fail)
    with pytest.raises(RuntimeError, match="Failure after"):
        case_engine.evaluate_case(conn, cid)
    assert list(conn.iterdump()) == before
    assert conn.in_transaction is nested


def test_reimport_of_same_source_does_not_reopen_resolved_case(env):
    conn, sid, cid, _, _ = env
    case.update_case_status(conn, cid, "RESOLVED")
    result = core.import_source(conn, "tracking_no,postex_remark\nCYCLE,RFD")
    assert result["events_created"] == 0
    result = case_engine.evaluate_shipment(conn, sid)
    assert result.disposition == "MONITOR"
    assert case.get_case(conn, cid)["status"] == "RESOLVED"
    assert len(case.get_case_history(conn, cid)) == 1


def test_legacy_resolved_case_without_audit_boundary_handles_new_evidence(env):
    conn, sid, cid, clock, _ = env
    conn.execute("UPDATE case_entity SET status='RESOLVED',updated_at='2026-06-15T09:00:00+00:00' WHERE id=?", (cid,))
    conn.commit()
    # Migration cannot infer a historical closure boundary from wall-clock time.
    # Even apparently older evidence must be reviewed, not silently consumed.
    assert case_engine.evaluate_case(conn, cid).disposition == "HUMAN_TASK_REQUIRED"
    assert "legacy" in case_engine.assess_case(conn, cid)[3]["blocked_reason"].lower()
    observation(conn, sid, clock, "2026-06-16T08:00:00+00:00")
    assert case_engine.evaluate_case(conn, cid).disposition == "HUMAN_TASK_REQUIRED"
    assert case.get_case(conn, cid)["status"] == "OPEN"
    assert case.get_case_history(conn, cid)[0]["from_status"] == "RESOLVED"
    ids = [t["id"] for t in pending(conn, cid)]
    case_engine.evaluate_case(conn, cid)
    assert [t["id"] for t in pending(conn, cid)] == ids


def test_customer_evidence_alone_reopens_same_resolved_case(env):
    conn, _, cid, _, _ = env
    case.update_case_status(conn, cid, "RESOLVED")
    actions.record_customer_confirmation(conn, case.get_case(conn, cid)["shipment_id"], "I did not refuse")
    result = case_engine.evaluate_case(conn, cid)
    assert result.capability == "DECIDE_ACTION" and result.case_id == cid
    assert case.get_case(conn, cid)["status"] == "OPEN"


def test_malformed_legacy_rows_do_not_break_http_or_other_shipments(env, monkeypatch):
    conn, sid, cid, _, path = env
    conn.execute("INSERT INTO operator_action(id,shipment_id,kind,note,acted_at) VALUES ('bad-http',?,'note','Verbatim','bad-time')", (sid,))
    conn.commit()
    core.import_source(conn, "tracking_no,postex_remark\nHEALTHY,RFD")
    healthy = conn.execute("SELECT id FROM shipment WHERE tracking_no='HEALTHY'").fetchone()[0]
    good_case = case_engine.evaluate_shipment(conn, healthy).case_id
    complete(conn, pending(conn, good_case)[0]["id"], customer_response="I did not refuse")
    monkeypatch.setattr(ui, "DB_PATH", path)
    with TestClient(ui.app, raise_server_exceptions=False) as client:
        for url in ("/", "/shipment/CYCLE", "/shipment/HEALTHY", "/tasks/pending"):
            assert client.get(url).status_code == 200
        page = client.get("/shipment/CYCLE").text
        assert "Timestamp review required" in page and "bad-time" in page
    assert conn.execute("SELECT acted_at FROM operator_action WHERE id='bad-http'").fetchone()[0] == "bad-time"


def test_legacy_naive_and_offset_timestamps_remain_verbatim_and_comparable(env):
    conn, sid, _, _, _ = env
    actions.record_customer_confirmation(conn, sid, "A response", confirmed_at="2026-06-15T09:00:00")
    actions.record_operator_action(conn, sid, "note", note="Later in real time", acted_at="2026-06-15T04:30:00-05:00")
    timeline = detail.build_timeline(conn, sid)
    assert timeline[-1]["note"] == "Later in real time"
    assert timeline[-1]["at"] == "2026-06-15T04:30:00-05:00"
    assert actions.list_customer_confirmations(conn, sid)[0]["confirmed_at"] == "2026-06-15T09:00:00"


def test_monitor_without_explicit_retirement_cannot_discard_obligations(env, monkeypatch):
    conn, _, cid, _, _ = env
    original = case_engine.policy.evaluate_policy
    def unclassified(evidence, coordination):
        assessment = original(evidence, coordination)
        return {**assessment, "disposition": "MONITOR", "capability": None, "retire_work": False}
    before = pending(conn, cid)
    monkeypatch.setattr(case_engine.policy, "evaluate_policy", unclassified)
    case_engine.evaluate_case(conn, cid)
    assert pending(conn, cid) == before


def test_in_progress_completion_retry_does_not_duplicate_customer_evidence(env):
    conn, sid, cid, _, _ = env
    tid = pending(conn, cid)[0]["id"]
    human_task.update_human_task_status(conn, tid, "IN_PROGRESS")
    conn.commit()
    assert human_task.get_human_task(conn, tid)["completed_at"] is None
    complete(conn, tid, customer_response="I did not refuse")
    response = operational.complete_human_task_with_evidence(conn, tid, {"customer_response": "I did not refuse"})
    assert not response["success"]
    assert len(actions.list_customer_confirmations(conn, sid)) == 1


def test_equal_instant_legacy_decision_cannot_settle_courier_claim_by_cross_table_rowid(env):
    conn, sid, cid, _, _ = env
    for index in range(3):
        actions.record_operator_action(conn, sid, "note", note=str(index), acted_at="2026-06-14T08:00:00Z")
    # Historical API/data can contain time ties: a larger operator rowid is not
    # proof that it happened after a courier row in a different table.
    actions.record_operator_action(conn, sid, "delivered_confirmed", acted_at="2026-06-15T13:00:00+05:00")
    assert core.derive_state(conn, sid) == "NEEDS_ACTION"
    assert case_engine.evaluate_case(conn, cid).disposition == "HUMAN_TASK_REQUIRED"
    assert pending(conn, cid)
    assert detail.shipment_detail(conn, "CYCLE")["derived_state"] == "NEEDS_ACTION"


def test_followup_dates_are_sorted_by_instant_and_bad_legacy_due_is_not_guessed_overdue(env):
    from commerceops import followups
    conn, sid, _, _, _ = env
    later = followups.create_follow_up(conn, sid, reason="Later", due_at="2026-06-17T01:00:00Z")
    earlier = followups.create_follow_up(conn, sid, reason="Earlier", due_at="2026-06-17T05:00:00+05:00")
    fus = followups.list_follow_ups(conn, shipment_id=sid, now="2026-06-16T00:00:00Z")
    assert [f["id"] for f in fus["upcoming"]] == [earlier, later]
    conn.execute("UPDATE follow_up SET due_at='bogus' WHERE id=?", (earlier,))
    conn.commit()
    fus = followups.list_follow_ups(conn, shipment_id=sid, now="2026-06-18T00:00:00Z")
    assert [f["id"] for f in fus["overdue"]] == [later]
    assert [f["id"] for f in fus["invalid"]] == [earlier]
    assert "timestamp_review" in followups.work_queue(conn)[0]["reasons"]


def test_legacy_closure_without_cursors_must_not_swallow_backdated_new_evidence(env):
    conn, sid, cid, clock, _ = env
    conn.execute("UPDATE case_entity SET status='RESOLVED',updated_at='2026-06-15T09:00:00+00:00' WHERE id=?", (cid,))
    conn.commit()
    observation(conn, sid, clock, "2026-06-14T08:00:00+00:00")
    result = case_engine.evaluate_case(conn, cid)
    assert result.disposition == "HUMAN_TASK_REQUIRED"
    assert case.get_case(conn, cid)["status"] == "OPEN"
    assert len(pending(conn, cid)) == 1
    _, _, _, assessment = case_engine.assess_case(conn, cid)
    assert "legacy" in assessment["blocked_reason"].lower()


@pytest.mark.parametrize("kind", ["delivered_confirmed", "cancel_decided", "returned_confirmed"])
@pytest.mark.parametrize("nested", [False, True])
def test_terminal_completion_late_failure_rolls_back_every_write(env, monkeypatch, kind, nested):
    conn, sid, cid, _, _ = env
    tid = decision_task(conn, cid)
    if nested:
        conn.execute("BEGIN")
        actions.record_operator_action(conn, sid, "note", note="Caller-owned work")
    before = list(conn.iterdump())
    original = case_engine.evaluate_case
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("After terminal write and task completion")
    monkeypatch.setattr(case_engine, "evaluate_case", fail)
    result = operational.complete_human_task_with_evidence(conn, tid, {
        "decision_type": kind, "cancel_reason": "Customer choice", "confirm_cancel": True,
    })
    assert not result["success"] and "After terminal write" in result["error"]
    assert list(conn.iterdump()) == before
    assert conn.in_transaction is nested


def test_legacy_boundary_review_requires_explicit_audited_acknowledgement(env):
    conn, _, cid, _, _ = env
    conn.execute("UPDATE case_entity SET status='RESOLVED' WHERE id=?", (cid,))
    conn.commit()
    case_engine.evaluate_case(conn, cid)
    before = list(conn.iterdump())
    result = operational.complete_human_task_with_evidence(conn, pending(conn, cid)[0]["id"], {"decision_type": "reattempt_requested"})
    assert not result["success"] and "Legacy closure" in result["error"]
    assert list(conn.iterdump()) == before
    # Operator reviews prior history and explicitly accepts the closure basis.
    case.update_case_status(conn, cid, "RESOLVED", reason="Operator reviewed and accepted historical evidence")
    case.update_case_status(conn, cid, "OPEN", reason="Operator requested a fresh exception review")
    case_engine.evaluate_case(conn, cid)
    assert not case_engine.assess_case(conn, cid)[3].get("blocked_reason")
    assert any(h["reason"] == "Operator reviewed and accepted historical evidence" for h in case.get_case_history(conn, cid))
    complete(conn, pending(conn, cid)[0]["id"], customer_response="I did not refuse")


def test_preclosure_timestamps_cannot_keep_post_reopen_decision_unsettled(env):
    conn, sid, cid, clock, _ = env
    # Previously accepted source time is far ahead of subsequently received data.
    actions.record_customer_confirmation(conn, sid, "Prior response", confirmed_at="2027-01-01T00:00:00Z")
    case.update_case_status(conn, cid, "RESOLVED")
    observation(conn, sid, clock, "2026-06-16T08:00:00Z")
    case_engine.evaluate_case(conn, cid)
    complete(conn, pending(conn, cid)[0]["id"], customer_response="I did not refuse", verified_at="2026-06-16T09:00:00Z")
    result = complete(conn, pending(conn, cid)[0]["id"], decision_type="reattempt_requested", decided_at="2026-06-16T10:00:00Z")
    assert result["next_evaluation"].disposition == "MONITOR"
    assert pending(conn, cid) == []
