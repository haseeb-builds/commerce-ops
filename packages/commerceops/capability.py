"""Commerce Ops Phase 3 — Operational Capability Layer.

This module implements the capability layer that turns Case Engine decisions
into executable, auditable operational work and feeds resulting evidence back
into the authoritative evidence store.

The capability layer performs or records capabilities requested by the Case Engine:
- VERIFY_CUSTOMER → customer_confirmation (via operator input/recording)
- DECIDE_ACTION → operator_action (via operator decision recording)  
- FOLLOW_UP_ACTION → follow_up (human-set follow-up creation)
- Autonomous actions → autonomous_action (PLANNED state only)

All evidence is stored in the existing authoritative tables to maintain a single
source of truth. No workflow engine or state machine is introduced.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from commerceops import core
from commerceops import actions
from commerceops import followups
from commerceops import human_task
from commerceops import autonomous_action


def execute_capability(
    conn: core.sqlite3.Connection,
    case_id: str,
    capability: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Execute a human capability request and return the evidence/reference ID.
    
    This function executes the operational work associated with a human capability
    request from the Case Engine. It records the work in the appropriate
    authoritative evidence table and returns a reference ID for auditing.
    
    Args:
        conn: SQLite connection (with foreign keys enabled)
        case_id: ID of the case requesting the capability
        capability: Capability to execute (VERIFY_CUSTOMER, DECIDE_ACTION, FOLLOW_UP_ACTION)
        payload: Optional payload data for the capability execution
        
    Returns:
        Reference ID of the created evidence/work record
        
    Raises:
        ValueError: For invalid capability types
        ValidationError: For invalid payload data
    """
    if payload is None:
        payload = {}
        
    if capability == "VERIFY_CUSTOMER":
        return _execute_verify_customer(conn, case_id, payload)
    elif capability == "DECIDE_ACTION":
        return _execute_decide_action(conn, case_id, payload)
    elif capability == "FOLLOW_UP_ACTION":
        return _execute_follow_up_action(conn, case_id, payload)
    else:
        raise ValueError(f"Unsupported capability: {capability}")


def plan_autonomous_action(
    conn: core.sqlite3.Connection,
    case_id: str,
    action_type: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Plan an autonomous action (store as PLANNED, do not execute).
    
    This function records a proposed autonomous action in the PLANNED state.
    Actual execution is outside the scope of this system and must be performed
    manually by operators who then record the result using the appropriate
    evidence recording mechanisms.
    
    Args:
        conn: SQLite connection (with foreign keys enabled)
        case_id: ID of the case associated with the action
        action_type: Type of action to plan (e.g., 'SEND_REATTEMPT_REQUEST')
        payload: Optional payload data for the action
        
    Returns:
        ID of the planned autonomous action record
        
    Raises:
        ValidationError: For invalid payload data
    """
    if payload is None:
        payload = {}
        
    return autonomous_action.create_autonomous_action(
        conn, case_id, action_type, payload
    )


def complete_human_task(
    conn: core.sqlite3.Connection,
    task_id: str,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark a human task as completed with a result.
    
    This function updates a human task's status to COMPLETED and stores the
    completion result. The result should contain the evidence that was
    generated (e.g., for VERIFY_CUSTOMER, the customer confirmation details).
    
    Args:
        conn: SQLite connection
        task_id: ID of the human task to complete
        result: Optional dictionary containing the task completion result
    """
    if result is None:
        result = {}
    human_task.complete_human_task(conn, task_id, result)


def complete_autonomous_action(
    conn: core.sqlite3.Connection,
    action_id: str,
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark an autonomous action as executed with a result.
    
    This function updates an autonomous action's status to EXECUTED and stores
    the execution result. Note: Actual execution of autonomous actions is
    outside the scope of this system and must be triggered externally.
    
    Args:
        conn: SQLite connection
        action_id: ID of the autonomous action to mark as executed
        result: Optional dictionary containing the execution result
    """
    if result is None:
        result = {}
    autonomous_action.mark_autonomous_action_executed(conn, action_id, result)


def fail_autonomous_action(
    conn: core.sqlite3.Connection,
    action_id: str,
    failure_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Mark an autonomous action as failed with failure information.
    
    Args:
        conn: SQLite connection
        action_id: ID of the autonomous action to mark as failed
        failure_info: Optional dictionary containing failure information
    """
    if failure_info is None:
        failure_info = {}
    autonomous_action.mark_autonomous_action_failed(conn, action_id, failure_info)


def get_pending_human_tasks(conn: core.sqlite3.Connection) -> List[Dict[str, Any]]:
    """Get all pending human tasks across all cases.
    
    Args:
        conn: SQLite connection
        
    Returns:
        List of pending human task dictionaries
    """
    return human_task.get_pending_tasks(conn)


def get_planned_autonomous_actions(
    conn: core.sqlite3.Connection,
) -> List[Dict[str, Any]]:
    """Get all planned autonomous actions across all cases.
    
    Args:
        conn: SQLite connection
        
    Returns:
        List of planned autonomous action dictionaries
    """
    return autonomous_action.get_planned_actions(conn)


def _execute_verify_customer(
    conn: core.sqlite3.Connection,
    case_id: str,
    payload: Dict[str, Any],
) -> str:
    """Execute VERIFY_CUSTOMER capability.
    
    For VERIFY_CUSTOMER, the capability layer records that verification
    is needed. The actual verification (customer contact) and evidence
    recording (customer_confirmation) is performed by human operators
    using the existing V0 action recording mechanisms.
    
    The capability layer creates a human task to track that verification
    is needed. Operators then:
    1. Contact the customer to verify the refusal
    2. Record the result using actions.record_customer_confirmation()
    3. Complete the human task with the verification result
    
    Args:
        conn: SQLite connection
        case_id: ID of the case requiring verification
        payload: Payload data (may include preferred contact method, etc.)
        
    Returns:
        ID of the created human task tracking the verification request
    """
    # Create a human task to track that verification is needed
    task_id = human_task.create_human_task(
        conn,
        case_id,
        "VERIFY_CUSTOMER",
        payload,
    )
    return task_id


def _execute_decide_action(
    conn: core.sqlite3.Connection,
    case_id: str,
    payload: Dict[str, Any],
) -> str:
    """Execute DECIDE_ACTION capability.
    
    For DECIDE_ACTION, the capability layer records that a decision
    is needed. The actual decision-making and evidence recording
    (operator_action) is performed by human operators.
    
    The capability layer creates a human task to track that a decision
    is needed. Operators then:
    1. Make the required decision (e.g., reattempt, cancel, etc.)
    2. Record the decision using actions.record_operator_action()
    3. Complete the human task with the decision details
    
    Args:
        conn: SQLite connection
        case_id: ID of the case requiring a decision
        payload: Payload data (may include decision context, options, etc.)
        
    Returns:
        ID of the created human task tracking the decision request
    """
    # Create a human task to track that a decision is needed
    task_id = human_task.create_human_task(
        conn,
        case_id,
        "DECIDE_ACTION",
        payload,
    )
    return task_id


def _execute_follow_up_action(
    conn: core.sqlite3.Connection,
    case_id: str,
    payload: Dict[str, Any],
) -> str:
    """Execute FOLLOW_UP_ACTION capability.
    
    For FOLLOW_UP_ACTION, the capability layer creates a human-set
    follow-up using the existing V0 follow-up mechanism.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case requiring follow-up
        payload: Payload data containing follow-up details:
                - reason: Reason for the follow-up (required)
                - due_at: Due date for the follow-up (required, ISO format)
                
    Returns:
        ID of the created follow-up record
        
    Raises:
        ValidationError: For missing required payload fields
    """
    # Extract required follow-up parameters from payload
    reason = payload.get("reason", "").strip()
    due_at = payload.get("due_at", "").strip()
    
    if not reason:
        raise ValidationError("follow_up_action requires a non-empty reason in payload")
    if not due_at:
        raise ValidationError("follow_up_action requires a non-empty due_at in payload")
        
    # Get the shipment ID associated with this case
    from commerceops import case
    case_obj = case.get_case(conn, case_id)
    if case_obj is None:
        raise ValueError(f"Case not found: {case_id}")
    shipment_id = case_obj["shipment_id"]
    
    # Create the follow-up using the existing V0 mechanism
    follow_up_id = followups.create_follow_up(
        conn,
        shipment_id,
        due_at,
        reason,
    )
    return follow_up_id