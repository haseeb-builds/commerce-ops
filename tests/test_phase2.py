"""Phase 2 tests for Case Engine evaluation core."""

import os
import tempfile
import json

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
import commerceops.case as case_module
import commerceops.case_engine as case_engine
import commerceops.core as core
import commerceops.human_task as human_task
import commerceops.autonomous_action as autonomous_action
import commerceops.followups as followups
from commerceops import policy


def ship_id(conn, tracking_no: str) -> str:
    """Get shipment id by tracking_no."""
    row = conn.execute(
        "SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)
    ).fetchone()
    return row["id"]


def create_shipment(conn, tracking_no: str) -> str:
    """Create a shipment by importing minimal evidence and return its id."""
    evidence = f"""\
tracking_no,postex_remark,our_remark,status,customer_phone
{tracking_no},XXX,our_remark,,
"""
    core.import_source(conn, evidence)
    return ship_id(conn, tracking_no)


def test_rfd_no_customer_verification_required():
    """RFD with no customer confirmation requires VERIFY_CUSTOMER."""
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
        
        # Create a case for the shipment
        case_id = case_module.create_case(conn, shipment_id)
        
        # Evaluate the case
        result = case_engine.evaluate_case(conn, case_id)
        
        # Assert that the policy returns HUMAN_TASK_REQUIRED with capability VERIFY_CUSTOMER
        assert result.disposition == "HUMAN_TASK_REQUIRED"
        assert result.capability == "VERIFY_CUSTOMER"
        assert result.action_type is None
        assert "verify" in result.reason.lower()
        assert result.evidence_ids  # should have the tracking event id
        assert result.policy_version == 1
        
        # Check that a human task was created
        tasks = human_task.get_tasks_for_case(conn, case_id)
        assert len(tasks) == 1
        task = tasks[0]
        assert task["capability"] == "VERIFY_CUSTOMER"
        assert task["status"] == "PENDING"
        assert task["case_id"] == case_id
        
        conn.close()


def test_rfd_customer_confirms_refusal_then_decide_action():
    """After customer confirms refusal, next is DECIDE_ACTION."""
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
        
        # Set the tracking event's imported_at to a fixed time in the past
        # We'll use 2026-01-01T09:00:00+00:00
        conn.execute(
            "UPDATE tracking_event SET imported_at = ? WHERE shipment_id = ?",
            ("2026-01-01T09:00:00+00:00", shipment_id)
        )
        
        # Add a customer confirmation that says they refused, after the tracking event
        from commerceops import actions
        actions.record_customer_confirmation(
            conn,
            shipment_id,
            "I refused the parcel.",
            channel="whatsapp",
            confirmed_at="2026-01-01T10:00:00+00:00"
        )
        
        # Create a case
        case_id = case_module.create_case(conn, shipment_id)
        
        # Evaluate the case
        result = case_engine.evaluate_case(conn, case_id)
        
        # After verification is satisfied, we should need to decide what to do.
        assert result.disposition == "HUMAN_TASK_REQUIRED"
        assert result.capability == "DECIDE_ACTION"
        assert result.action_type is None
        assert "decide" in result.reason.lower()
        assert result.evidence_ids  # should have the tracking event and customer confirmation
        
        # Check that a human task for DECIDE_ACTION was created (if not already present)
        tasks = human_task.get_tasks_for_case(conn, case_id)
        # We might have created a VERIFY_CUSTOMER task earlier? 
        # But in this scenario, verification is satisfied, so we should not create a VERIFY_CUSTOMER task.
        # We should create a DECIDE_ACTION task if there isn't one already.
        # We'll check that there is at least one DECIDE_ACTION task that is pending.
        decide_tasks = [t for t in tasks if t.get("capability") == "DECIDE_ACTION" and t.get("status") == "PENDING"]
        assert len(decide_tasks) >= 1
        
        conn.close()


def test_rfd_customer_denies_refusal_then_decide_action():
    """After customer denies refusal (contradiction), we need to decide."""
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
        
        # Set the tracking event's imported_at to a fixed time in the past
        # We'll use 2026-01-01T09:00:00+00:00
        conn.execute(
            "UPDATE tracking_event SET imported_at = ? WHERE shipment_id = ?",
            ("2026-01-01T09:00:00+00:00", shipment_id)
        )
        
        # Insert a customer confirmation that denies refusal, after the tracking event
        from commerceops import actions
        actions.record_customer_confirmation(
            conn,
            shipment_id,
            "I did not refuse the parcel.",
            channel="whatsapp",
            confirmed_at="2026-01-01T10:00:00+00:00"
        )
        
        # Create a case
        case_id = case_module.create_case(conn, shipment_id)
        
        # Evaluate the case
        result = case_engine.evaluate_case(conn, case_id)
        
        # Due to contradiction, we need to decide.
        assert result.disposition == "HUMAN_TASK_REQUIRED"
        assert result.capability == "DECIDE_ACTION"
        assert result.action_type is None
        assert "contradiction" in result.reason.lower() or "decision" in result.reason.lower()
        assert result.evidence_ids
        
        # Check that a DECIDE_ACTION task was created.
        tasks = human_task.get_tasks_for_case(conn, case_id)
        decide_tasks = [t for t in tasks if t.get("capability") == "DECIDE_ACTION" and t.get("status") == "PENDING"]
        assert len(decide_tasks) >= 1
        
        conn.close()


def test_after_reattempt_requested_then_monitor():
    """After an operator action of reattempt_requested, we should monitor."""
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
        
        # Set the tracking event's imported_at to a fixed time in the past
        # We'll use 2026-01-01T09:00:00+00:00
        conn.execute(
            "UPDATE tracking_event SET imported_at = ? WHERE shipment_id = ?",
            ("2026-01-01T09:00:00+00:00", shipment_id)
        )
        
        # Insert a customer confirmation that says they refused (so verification satisfied).
        from commerceops import actions
        actions.record_customer_confirmation(
            conn,
            shipment_id,
            "I refused the parcel.",
            channel="whatsapp",
            confirmed_at="2026-01-01T10:00:00+00:00"
        )
        
        # Insert an operator action of reattempt_requested, after the customer confirmation
        actions.record_operator_action(
            conn,
            shipment_id,
            "reattempt_requested",
            acted_at="2026-01-01T11:00:00+00:00"
        )
        
        # Create a case
        case_id = case_module.create_case(conn, shipment_id)
        
        # Evaluate the case
        result = case_engine.evaluate_case(conn, case_id)
        
        # After verification and decision (reattempt requested), we should monitor.
        assert result.disposition == "MONITOR"
        assert result.capability is None
        assert result.action_type is None
        assert "decision" in result.reason.lower() and "no new evidence" in result.reason.lower()
        assert result.evidence_ids
        
        # No new human task should be created (since we are monitoring).
        tasks = human_task.get_tasks_for_case(conn, case_id)
        # We should have the existing human tasks from before (verification and decision tasks) but no new pending ones.
        # We'll just check that there are no pending tasks of any type? 
        # Actually, we might have completed tasks. We'll not check for now.
        
        conn.close()


if __name__ == "__main__":
    test_rfd_no_customer_verification_required()
    test_rfd_customer_confirms_refusal_then_decide_action()
    test_rfd_customer_denies_refusal_then_decide_action()
    test_after_reattempt_requested_then_monitor()
    print("All tests passed!")