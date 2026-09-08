"""Commerce Ops Phase 3 — Operational Capability Layer tests.

These tests verify that the capability layer correctly turns Case Engine decisions
into executable, auditable operational work and feeds resulting evidence back
into the authoritative evidence store.
"""

import tempfile
import os
from commerceops import core
from commerceops import case_engine
from commerceops import case
from commerceops import capability
from commerceops import human_task
from commerceops import autonomous_action
from commerceops import actions
from commerceops import followups

def ship_id(conn, tracking_no: str) -> str:
    row = conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)).fetchone()
    return row["id"] if row else None



def test_verify_customer_capability_creates_task():
    """VERIFY_CUSTOMER capability creates a human task for verification."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment with an RFD tracking event
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Execute VERIFY_CUSTOMER capability
        task_id = capability.execute_capability(
            conn, case_id, "VERIFY_CUSTOMER", {}
        )
        
        # Verify a human task was created
        task = human_task.get_human_task(conn, task_id)
        assert task is not None
        assert task["case_id"] == case_id
        assert task["type"] == "VERIFY_CUSTOMER"
        assert task["capability"] == "VERIFY_CUSTOMER"
        assert task["status"] == "PENDING"
        
        conn.close()


def test_decide_action_capability_creates_task():
    """DECIDE_ACTION capability creates a human task for decision-making."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Execute DECIDE_ACTION capability
        task_id = capability.execute_capability(
            conn, case_id, "DECIDE_ACTION", {}
        )
        
        # Verify a human task was created
        task = human_task.get_human_task(conn, task_id)
        assert task is not None
        assert task["case_id"] == case_id
        assert task["type"] == "DECIDE_ACTION"
        assert task["capability"] == "DECIDE_ACTION"
        assert task["status"] == "PENDING"
        
        conn.close()


def test_follow_up_action_creates_follow_up():
    """FOLLOW_UP_ACTION capability creates a human-set follow-up."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Get the case to access shipment_id for follow-up creation
        case_obj = case.get_case(conn, case_id)
        assert case_obj is not None
        
        # Execute FOLLOW_UP_ACTION capability
        follow_up_id = capability.execute_capability(
            conn,
            case_id,
            "FOLLOW_UP_ACTION",
            {
                "reason": "Customer needs callback",
                "due_at": "2026-12-31T10:00:00+00:00"
            }
        )
        
        # Verify a follow-up was created
        follow_up = followups.list_follow_ups(conn, shipment_id=case_obj["shipment_id"])
        assert len(follow_up["open"]) == 1
        created_follow_up = follow_up["open"][0]
        assert created_follow_up["reason"] == "Customer needs callback"
        assert created_follow_up["due_at"] == "2026-12-31T10:00:00+00:00"
        assert created_follow_up["status"] == "open"
        
        conn.close()


def test_plan_autonomous_action_creates_planned_action():
    """PLANNED autonomous action capability creates a planned action record."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Plan an autonomous action
        action_id = capability.plan_autonomous_action(
            conn,
            case_id,
            "SEND_REATTEMPT_REQUEST",
            {"message": "Prepare reattempt notice"}
        )
        
        # Verify a planned autonomous action was created
        action = autonomous_action.get_autonomous_action(conn, action_id)
        assert action is not None
        assert action["case_id"] == case_id
        assert action["action_type"] == "SEND_REATTEMPT_REQUEST"
        assert action["status"] == "PLANNED"
        assert action["payload"] == {"message": "Prepare reattempt notice"}
        
        conn.close()


def test_verify_customer_task_completion_records_evidence():
    """Completing a VERIFY_CUSTOMER task records customer confirmation evidence."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Execute VERIFY_CUSTOMER capability (creates tracking task)
        task_id = capability.execute_capability(
            conn, case_id, "VERIFY_CUSTOMER", {}
        )
        
        # Simulate operator performing verification and recording result
        verification_result = {
            "verification_method": "whatsapp",
            "customer_response": "I refused the parcel",
            "verified_at": "2026-06-15T14:30:00+00:00"
        }
        
        # Operator records the customer confirmation (authoritative evidence)
        confirmation_id = actions.record_customer_confirmation(
            conn,
            shipment_id,
            "I refused the parcel",
            channel="whatsapp",
            confirmed_at="2026-06-15T14:30:00+00:00"
        )
        
        # Operator completes the human task with the verification result
        capability.complete_human_task(conn, task_id, {
            "confirmation_id": confirmation_id,
            "verification_result": verification_result
        })
        
        # Verify the task is marked as completed
        task = human_task.get_human_task(conn, task_id)
        assert task["status"] == "COMPLETED"
        assert task["payload"]["confirmation_id"] == confirmation_id
        
        # Verify the customer confirmation evidence exists in authoritative store
        confirmations = actions.list_customer_confirmations(conn, shipment_id)
        assert len(confirmations) == 1
        confirmation = confirmations[0]
        assert confirmation["content"] == "I refused the parcel"
        assert confirmation["channel"] == "whatsapp"
        assert confirmation["confirmed_at"] == "2026-06-15T14:30:00+00:00"
        
        conn.close()


def test_decide_action_task_completion_records_evidence():
    """Completing a DECIDE_ACTION task records operator decision evidence."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Execute DECIDE_ACTION capability (creates tracking task)
        task_id = capability.execute_capability(
            conn, case_id, "DECIDE_ACTION", {}
        )
        
        # Simulate operator making decision and recording it
        decision_result = {
            "decision_type": "reattempt_requested",
            "decision_made_at": "2026-06-15T15:00:00+00:00",
            "notes": "Customer verified refusal, requesting reattempt"
        }
        
        # Operator records the decision (authoritative evidence)
        action_id = actions.record_operator_action(
            conn,
            shipment_id,
            "reattempt_requested",
            note="Customer verified refusal, requesting reattempt",
            acted_at="2026-06-15T15:00:00+00:00"
        )
        
        # Operator completes the human task with the decision details
        capability.complete_human_task(conn, task_id, {
            "action_id": action_id,
            "decision_result": decision_result
        })
        
        # Verify the task is marked as completed
        task = human_task.get_human_task(conn, task_id)
        assert task["status"] == "COMPLETED"
        assert task["payload"]["action_id"] == action_id
        
        # Verify the operator action evidence exists in authoritative store
        actions_list = actions.list_operator_actions(conn, shipment_id)
        assert len(actions_list) == 1
        action = actions_list[0]
        assert action["kind"] == "reattempt_requested"
        assert action["note"] == "Customer verified refusal, requesting reattempt"
        assert action["acted_at"] == "2026-06-15T15:00:00+00:00"
        
        conn.close()


def test_follow_up_action_completion_records_evidence():
    """Completing a follow-up action records follow-up completion evidence."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Execute FOLLOW_UP_ACTION capability (creates follow-up)
        follow_up_id = capability.execute_capability(
            conn,
            case_id,
            "FOLLOW_UP_ACTION",
            {
                "reason": "Address verification needed",
                "due_at": "2026-06-20T10:00:00+00:00"
            }
        )
        
        # Simulate operator completing the follow-up
        follow_up_result = {
            "completion_notes": "Customer confirmed correct address",
            "completed_by": "operator_user"
        }
        
        # Operator completes the follow-up (authoritative evidence)
        followups.complete_follow_up(conn, follow_up_id)
        
        # Verify the follow-up is marked as done
        follow_up = followups.list_follow_ups(conn, shipment_id=shipment_id)
        assert len(follow_up["closed"]) == 1
        completed_follow_up = follow_up["closed"][0]
        assert completed_follow_up["reason"] == "Address verification needed"
        assert completed_follow_up["due_at"] == "2026-06-20T10:00:00+00:00"
        assert completed_follow_up["status"] == "done"
        
        conn.close()


def test_capability_execution_is_idempotent():
    """Repeating the same capability request does not create duplicate work."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Execute VERIFY_CUSTOMER capability twice
        task_id1 = capability.execute_capability(
            conn, case_id, "VERIFY_CUSTOMER", {}
        )
        task_id2 = capability.execute_capability(
            conn, case_id, "VERIFY_CUSTOMER", {}
        )
        
        # Verify only one task was created (idempotency)
        assert task_id1 == task_id2
        
        # Verify the task exists and is pending
        task = human_task.get_human_task(conn, task_id1)
        assert task is not None
        assert task["status"] == "PENDING"
        
        # Verify only one human task exists for this case
        tasks = human_task.get_tasks_for_case(conn, case_id)
        verify_tasks = [t for t in tasks if t["type"] == "VERIFY_CUSTOMER"]
        assert len(verify_tasks) == 1
        
        conn.close()


def test_autonomous_action_planning_is_idempotent():
    """Repeating the same autonomous action planning does not create duplicates."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # Create a shipment
        tracking_no = "SN123"
        evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence)
        shipment_id = ship_id(conn, tracking_no)
        
        # Create a case
        case_id = case.create_case(conn, shipment_id)
        
        # Plan the same autonomous action twice
        action_id1 = capability.plan_autonomous_action(
            conn,
            case_id,
            "GENERATE_DOCUMENT",
            {"doc_type": "reattempt_notice"}
        )
        action_id2 = capability.plan_autonomous_action(
            conn,
            case_id,
            "GENERATE_DOCUMENT",
            {"doc_type": "reattempt_notice"}
        )
        
        # Verify only one action was created (idempotency)
        assert action_id1 == action_id2
        
        # Verify the action exists and is planned
        action = autonomous_action.get_autonomous_action(conn, action_id1)
        assert action is not None
        assert action["status"] == "PLANNED"
        
        # Verify only one autonomous action exists for this case
        actions = autonomous_action.get_actions_for_case(conn, case_id)
        planned_actions = [a for a in actions if a["status"] == "PLANNED"]
        assert len(planned_actions) == 1
        
        conn.close()


def test_end_to_end_rfd_lifecycle():
    """Test the complete RFD lifecycle: RFD → verification → contradiction → decision → reattempt → monitor → new evidence."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        
        # STEP 1: Initial RFD observation
        tracking_no = "SN123"
        evidence_step1 = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence_step1)
        shipment_id = ship_id(conn, tracking_no)
        # Backdate tracking event so operator action at 2026-06-15T11:00:00 is the latest evidence
        conn.execute(
            "UPDATE tracking_event SET imported_at = ? WHERE shipment_id = ?",
            ("2026-06-15T08:00:00+00:00", shipment_id)
        )
        case_id = case.create_case(conn, shipment_id)
        
        # Evaluate case - should require verification
        result = case_engine.evaluate_case(conn, case_id)
        assert result.disposition == "HUMAN_TASK_REQUIRED"
        assert result.capability == "VERIFY_CUSTOMER"
        
        # Execute verification capability
        verify_task_id = capability.execute_capability(
            conn, case_id, "VERIFY_CUSTOMER", {}
        )
        
        # STEP 2: Customer denies refusal (contradiction evidence)
        actions.record_customer_confirmation(
            conn,
            shipment_id,
            "I did not refuse the parcel.",
            channel="email",
            confirmed_at="2026-06-15T10:00:00+00:00"
        )
        
        # Re-evaluate case - should now require decision due to contradiction
        result = case_engine.evaluate_case(conn, case_id)
        assert result.disposition == "HUMAN_TASK_REQUIRED"
        assert result.capability == "DECIDE_ACTION"
        assert "contradiction" in result.reason.lower()
        
        # Execute decision capability
        decide_task_id = capability.execute_capability(
            conn, case_id, "DECIDE_ACTION", {}
        )
        
        # STEP 3: Operator decides on reattempt
        actions.record_operator_action(
            conn,
            shipment_id,
            "reattempt_requested",
            acted_at="2026-06-15T11:00:00+00:00"
        )
        
        # Re-evaluate case - should now monitor (decision made, no new evidence)
        result = case_engine.evaluate_case(conn, case_id)
        assert result.disposition == "MONITOR"
        assert result.capability is None
        assert "decision" in result.reason.lower()
        
        # STEP 4: New courier evidence arrives (new tracking event)
        evidence_step4 = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},RFD,,
"""
        core.import_source(conn, evidence_step4)
        conn.execute(
            "UPDATE tracking_event SET imported_at = ? WHERE shipment_id = ? AND raw_code = ?",
            ("2026-06-15T12:00:00+00:00", shipment_id, "RFD")
        )

        # Re-evaluate case - should now require verification again (new evidence)
        result = case_engine.evaluate_case(conn, case_id)
        assert result.disposition == "HUMAN_TASK_REQUIRED"
        assert result.capability == "VERIFY_CUSTOMER"
        assert result.capability == "VERIFY_CUSTOMER"
        
        # Verify we can execute verification again (new evidence cycle)
        verify_task_id2 = capability.execute_capability(
            conn, case_id, "VERIFY_CUSTOMER", {"cycle": 2}
        )

        # Verify this is a different task (new evidence cycle)
        assert verify_task_id2 != verify_task_id
        
        conn.close()


if __name__ == "__main__":
    test_verify_customer_capability_creates_task()
    test_decide_action_capability_creates_task()
    test_follow_up_action_creates_follow_up()
    test_plan_autonomous_action_creates_planned_action()
    test_verify_customer_task_completion_records_evidence()
    test_decide_action_task_completion_records_evidence()
    test_follow_up_action_completion_records_evidence()
    test_capability_execution_is_idempotent()
    test_autonomous_action_planning_is_idempotent()
    test_end_to_end_rfd_lifecycle()
    print("All Phase 3 tests passed!")

def test_idempotency_same_request_returns_existing_id():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = core.connect(db_path)
        tracking_no = "SN_DUP"
        evidence = f"tracking_no,postex_remark,our_remark,status,customer_phone\n{tracking_no},RFD,,\n"
        core.import_source(conn, evidence)
        sid = ship_id(conn, tracking_no)
        case_id = case.create_case(conn, sid)
        t1 = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
        t2 = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
        assert t1 == t2
        tasks = human_task.get_tasks_for_case(conn, case_id)
        assert len([t for t in tasks if t["type"] == "VERIFY_CUSTOMER"]) == 1
        conn.close()
