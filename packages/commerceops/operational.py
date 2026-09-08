"""Commerce Ops Phase 4 Increment 1 — Operational Task Completion Layer.

This module implements the operational task completion functionality that
pectateurs can use to complete human tasks and record the resulting evidence
in the authoritative store, triggering automatic case re-evaluation.

The operational layer provides:
- Pending task retrieval for work queues
- Human task completion with evidence recording
- Automatic case re-evaluation after meaningful completion
- Idempotent and transaction-safe operations
- Proper error handling and validation

This completes the human operational loop:
Evidence → Case Engine → Policy → Capability Request → Human Task →
Operator Completion → Evidence Recording → Case Re-evaluation → Next Work
"""

import json
from typing import Optional, Dict, Any, List, Tuple

from commerceops import core
from commerceops import case
from commerceops import case_engine
from commerceops import human_task
from commerceops import capability
from commerceops import actions
from commerceops import followups
from commerceops.actions import ValidationError
from commerceops.transactions import atomic
from commerceops.timestamps import parse_timestamp, display_order


def get_pending_human_tasks_with_context(
    conn: core.sqlite3.Connection
) -> List[Dict[str, Any]]:
    """Get all pending human tasks with additional context for operational display.
    
    This function extends the basic pending tasks list with contextual information
    that operators need to understand and perform the work:
    - Case information (shipment details, case age, etc.)
    - Evidence summary for the case
    - Suggested next steps based on task type
    
    Args:
        conn: SQLite connection (with foreign keys enabled)
        
    Returns:
        List of pending human task dictionaries with contextual information
    """
    # Get basic pending tasks (each entry already has case_id renamed by the helper)
    pending_tasks = [human_task.get_human_task(conn, t["id"]) for t in human_task.get_pending_tasks(conn, include_in_progress=True)]
    pending_tasks = [t for t in pending_tasks if t is not None]
    
    # Enrich each task with contextual information
    enriched_tasks = []
    for task in pending_tasks:
        task_id = task["id"]
        case_id = task["case_id"]
        
        # Get case information
        case_obj = case.get_case(conn, case_id)
        if case_obj is None:
            # Skip tasks for missing cases (shouldn't happen in practice)
            continue
            
        shipment_id = case_obj["shipment_id"]
        
        # Get shipment details
        shipment_row = conn.execute(
            "SELECT tracking_no, courier, customer_phone, current_state, created_at "
            "FROM shipment WHERE id=?", (shipment_id,)
        ).fetchone()
        
        # Get recent evidence for context (last 5 events)
        evidence_rows = conn.execute(
            """
            SELECT 'tracking_event' as source, te.id, te.raw_code, te.imported_at as ts
            FROM tracking_event te WHERE te.shipment_id=?
            UNION ALL
            SELECT 'operator_action' as source, oa.id, oa.kind as raw_code, oa.acted_at as ts
            FROM operator_action oa WHERE oa.shipment_id=?
            UNION ALL
            SELECT 'customer_confirmation' as source, cc.id, 
                   SUBSTR(cc.content, 1, 20) as raw_code, cc.confirmed_at as ts
            FROM customer_confirmation cc WHERE cc.shipment_id=?

            """,
            (shipment_id, shipment_id, shipment_id)
        ).fetchall()
        
        evidence_rows = sorted(evidence_rows, key=lambda row: display_order(row["ts"]), reverse=True)[:5]
        recent_evidence = [
            {
                "source": row["source"],
                "id": row["id"],
                "description": row["raw_code"],
                "timestamp": row["ts"]
            }
            for row in evidence_rows
        ]
        
        _, _, _, assessment = case_engine.assess_case(conn, case_id)
        # Build enriched task
        enriched_task = {
            **task,  # Include all original task fields
            "case_info": {
                "case_id": case_id,
                "shipment_id": shipment_id,
                "tracking_no": shipment_row["tracking_no"] if shipment_row else None,
                "courier": shipment_row["courier"] if shipment_row else None,
                "customer_phone": shipment_row["customer_phone"] if shipment_row else None,
                "case_age_hours": _calculate_case_age_hours(case_obj["opened_at"]),
                "case_status": case_obj["status"]
            },
            "shipment_info": {
                "tracking_no": shipment_row["tracking_no"] if shipment_row else None,
                "courier": shipment_row["courier"] if shipment_row else None,
                "customer_phone": shipment_row["customer_phone"] if shipment_row else None,
                "current_state": shipment_row["current_state"] if shipment_row else None
            },
            "recent_evidence": recent_evidence,
            "blocked_reason": assessment.get("blocked_reason"),
            "task_context": _get_task_context(task["type"], case_id, shipment_id, conn)
        }
        
        enriched_tasks.append(enriched_task)
    
    return enriched_tasks


def complete_human_task_with_evidence(
    conn: core.sqlite3.Connection,
    task_id: str,
    completion_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Complete a human task and record the resulting evidence.
    
    This function implements the core operational loop:
    1. Validates the task exists and is completable
    2. Performs the capability operation based on task type
    3. Records the resulting evidence in the authoritative store
    4. Marks the human task as completed
    5. Triggers case re-evaluation
    6. Returns the next work information
    
    Args:
        conn: SQLite connection (with foreign keys enabled)
        task_id: ID of the human task to complete
        completion_result: Dictionary containing the completion details
                          Format depends on task type:
                          - VERIFY_CUSTOMER: {"verification_method": "...", 
                                            "customer_response": "...", ...}
                          - DECIDE_ACTION: {"decision_type": "...", 
                                           "notes": "...", ...}
                          - FOLLOW_UP_ACTION: {"completion_notes": "...", ...}
                          
    Returns:
        Dictionary containing:
        - success: bool
        - task_id: str (completed task ID)
        - case_id: str (associated case ID)
        - next_evaluation: EvaluationResult (from case re-evaluation)
        - evidence_recorded: dict (details of what evidence was recorded)
        - error: str (if success=False)
        
    Raises:
        ValueError: For invalid task IDs or unsupported task types
        ValidationError: For invalid completion data
    """
    case_id = None
    try:
        with atomic(conn):
            if not isinstance(completion_result, dict):
                raise ValidationError("completion_result must be an object")
            task = human_task.get_human_task(conn, task_id)
            if task is None:
                raise ValueError(f"Human task not found: {task_id}")
            if task["status"] not in {"PENDING", "IN_PROGRESS"}:
                raise ValidationError(
                    f"Human task {task_id} is not PENDING or IN_PROGRESS (current status: {task['status']})")
            case_id = task["case_id"]
            case_obj, evidence, _, assessment = case_engine.assess_case(conn, case_id)
            if case_obj["status"] != "OPEN":
                raise ValidationError("Case is not OPEN; re-evaluate before completing work")
            if assessment.get("blocked_reason"):
                raise ValidationError(assessment["blocked_reason"])
            task_type = task["type"]
            original_payload = task["payload"] or {}
            if task_type in {"VERIFY_CUSTOMER", "DECIDE_ACTION"}:
                if not case_engine.task_matches_work(task, assessment):
                    raise ValidationError("Task is stale for current evidence; re-evaluate the shipment")
                _validate_completion_timestamp(task_type, completion_result, evidence,
                                               assessment["work_evidence_ids"])
            shipment_id = case_obj["shipment_id"]
            if task_type == "VERIFY_CUSTOMER":
                evidence_recorded = _handle_verify_customer_completion(
                    conn, case_id, shipment_id, completion_result)
            elif task_type == "DECIDE_ACTION":
                evidence_recorded = _handle_decide_action_completion(
                    conn, case_id, shipment_id, completion_result)
            elif task_type == "FOLLOW_UP_ACTION":
                evidence_recorded = _handle_follow_up_action_completion(
                    conn, case_id, shipment_id,
                    {**completion_result, "original_task_payload": original_payload})
            else:
                raise ValueError(f"Unsupported task type: {task_type}")
            human_task.complete_human_task(conn, task_id, {
                **original_payload, "completion_result": completion_result,
                "evidence_recorded": evidence_recorded,
            })
            next_evaluation = case_engine.evaluate_case(conn, case_id)
        return {
            "success": True, "task_id": task_id, "case_id": case_id,
            "next_evaluation": next_evaluation, "evidence_recorded": evidence_recorded,
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False, "task_id": task_id, "case_id": case_id,
            "next_evaluation": None, "evidence_recorded": None, "error": str(exc),
        }


def _validate_completion_timestamp(task_type, result, evidence, basis):
    """A response/decision cannot complete work based on evidence it predates."""
    from commerceops.policy import Evidence, evidence_timestamp
    field = "verified_at" if task_type == "VERIFY_CUSTOMER" else "decided_at"
    value = result.get(field)
    if value is None:
        return
    if parse_timestamp(value) is None:
        raise ValidationError(f"Invalid {field} timestamp")
    timestamp = evidence_timestamp(Evidence("completion", "operator_action", {"acted_at": value}))
    latest = max(evidence_timestamp(e) for e in evidence if e.id in basis)
    if timestamp <= latest:
        raise ValidationError(f"{field} must be later than the task's evidence")


def _handle_verify_customer_completion(
    conn: core.sqlite3.Connection,
    case_id: str,
    shipment_id: str,
    completion_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle VERIFY_CUSTOMER task completion.
    
    Records the verification result as a customer_confirmation in the
    authoritative evidence store.
    """
    # Extract verification details
    verification_method = completion_result.get("verification_method", "unknown")
    customer_response = completion_result.get("customer_response", "")
    verified_at = completion_result.get("verified_at")
    
    if not isinstance(customer_response, str) or not customer_response.strip():
        raise ValidationError("VERIFY_CUSTOMER completion requires non-empty customer_response")
    
    # Use provided timestamp or generate next valid timestamp
    if verified_at is None:
        from commerceops.actions import _next_timestamp
        confirmed_at = _next_timestamp(conn, shipment_id)
    else:
        confirmed_at = verified_at
    
    # Record the customer confirmation (authoritative evidence)
    confirmation_id = actions.record_customer_confirmation(
        conn,
        shipment_id,
        customer_response,
        channel=verification_method,
        confirmed_at=confirmed_at
    )
    
    return {
        "type": "customer_confirmation",
        "record_id": confirmation_id,
        "details": {
            "content": customer_response,
            "channel": verification_method,
            "confirmed_at": confirmed_at
        }
    }


def _handle_decide_action_completion(
    conn: core.sqlite3.Connection,
    case_id: str,
    shipment_id: str,
    completion_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle DECIDE_ACTION task completion.
    
    Records the decision as an operator_action in the authoritative evidence store.
    """
    # Extract decision details
    decision_type = completion_result.get("decision_type", "").strip()
    notes = completion_result.get("notes", "")
    decided_at = completion_result.get("decided_at")
    
    if not decision_type:
        raise ValidationError("DECIDE_ACTION completion requires non-empty decision_type")
    
    # A note/query is valid evidence, but cannot discharge a decision task.
    from commerceops.policy import DECISION_KINDS
    if decision_type not in DECISION_KINDS:
        raise ValidationError(
            f"Invalid decision_type '{decision_type}'. "
            f"Valid decision kinds: {', '.join(sorted(DECISION_KINDS))}"
        )
    
    if decision_type == "cancel_decided":
        confirmation = completion_result.get("confirm_cancel")
        if confirmation is not True and confirmation != "yes":
            raise ValidationError("Explicit cancellation confirmation is required")

    # Use provided timestamp or generate next valid timestamp
    if decided_at is None:
        from commerceops.actions import _next_timestamp
        acted_at = _next_timestamp(conn, shipment_id)
    else:
        acted_at = decided_at
    
    from commerceops import outcomes
    fields = {"note": notes if notes else None,
              "actor": completion_result.get("actor", "laiba"), "acted_at": acted_at}
    if decision_type == "delivered_confirmed":
        action_id = outcomes.mark_delivered(conn, shipment_id, **fields)
    elif decision_type == "returned_confirmed":
        action_id = outcomes.mark_returned(conn, shipment_id, **fields)
    elif decision_type == "cancel_decided":
        action_id = outcomes.mark_cancelled(
            conn, shipment_id, cancel_reason=completion_result.get("cancel_reason"), **fields)
    else:
        action_id = actions.record_operator_action(conn, shipment_id, decision_type, **fields)

    return {
        "type": "operator_action",
        "record_id": action_id,
        "details": {
            "kind": decision_type,
            "note": notes,
            "acted_at": acted_at
        }
    }


def _handle_follow_up_action_completion(
    conn: core.sqlite3.Connection,
    case_id: str,
    shipment_id: str,
    completion_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Handle FOLLOW_UP_ACTION task completion.
    
    Marks the follow-up as completed in the authoritative follow-up store.
    """
    # Extract completion details
    completion_notes = completion_result.get("completion_notes", "").strip()
    completed_at = completion_result.get("completed_at")
    
    # Get the follow-up ID from the original task payload
    original_payload = completion_result.get("original_task_payload", {})
    follow_up_id = original_payload.get("follow_up_id")
    
    if not follow_up_id:
        raise ValidationError("FOLLOW_UP_ACTION completion requires follow_up_id in original task payload")
    
    row = conn.execute("SELECT shipment_id FROM follow_up WHERE id=?", (follow_up_id,)).fetchone()
    if row is None or row["shipment_id"] != shipment_id:
        raise ValidationError("Follow-up does not belong to this task's shipment")

    # Use provided timestamp or current time
    if completed_at is None:
        from commerceops.core import utcnow
        completed_at = utcnow()
    
    # Complete the follow-up (authoritative evidence)
    followup_row = followups.complete_follow_up(conn, follow_up_id, completed_at=completed_at)
    
    return {
        "type": "follow_up_completion",
        "record_id": follow_up_id,
        "details": {
            "reason": followup_row["reason"],
            "due_at": followup_row["due_at"],
            "completed_at": completed_at,
            "completion_notes": completion_notes
        }
    }


def _calculate_case_age_hours(opened_at: str) -> float:
    """Calculate case age in hours from opened_at timestamp."""
    try:
        from datetime import datetime
        opened = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
        now = datetime.now().astimezone()
        delta = now - opened
        return delta.total_seconds() / 3600
    except Exception:
        return 0.0


def _get_task_context(
    task_type: str,
    case_id: str,
    shipment_id: str,
    conn: core.sqlite3.Connection
) -> Dict[str, Any]:
    """Get contextual information and suggested next steps for a task type."""
    
    context = {
        "task_type": task_type,
        "description": _get_task_description(task_type),
        "suggested_actions": _get_suggested_actions(task_type),
        "evidence_needed": _get_evidence_needed(task_type)
    }
    
    return context


def _get_task_description(task_type: str) -> str:
    """Get human-readable description of what the task entails."""
    descriptions = {
        "VERIFY_CUSTOMER": "Contact the customer to verify whether they refused the parcel",
        "DECIDE_ACTION": "Make a decision about how to proceed with this case",
        "FOLLOW_UP_ACTION": "Complete the follow-up action as specified"
    }
    return descriptions.get(task_type, "Complete the specified task")


def _get_suggested_actions(task_type: str) -> List[str]:
    """Get suggested actions the operator should take to complete this task."""
    suggestions = {
        "VERIFY_CUSTOMER": [
            "Contact customer via phone, email, or messaging",
            "Ask if they refused the parcel delivery",
            "Record their exact response",
            "Note the time and method of contact"
        ],
        "DECIDE_ACTION": [
            "Review the case evidence and customer responses",
            "Consider options: reattempt delivery, cancel, return to sender, etc.",
            "Make a clear decision based on the facts",
            "Provide reasoning for your decision"
        ],
        "FOLLOW_UP_ACTION": [
            "Complete the specified follow-up action",
            "Record the outcome and any relevant details",
            "Note the completion time"
        ]
    }
    return suggestions.get(task_type, ["Complete the task as specified"])


def _get_evidence_needed(task_type: str) -> List[str]:
    """Get what evidence the operator should look for to understand the context."""
    evidence = {
        "VERIFY_CUSTOMER": [
            "Recent tracking events showing delivery issues",
            "Previous customer communications",
            "Case notes and history"
        ],
        "DECIDE_ACTION": [
            "Customer verification responses",
            "Delivery attempt records",
            "Case history and previous decisions"
        ],
        "FOLLOW_UP_ACTION": [
            "Original reason for the follow-up",
            "Scheduled due date",
            "Any related case notes"
        ]
    }
    return evidence.get(task_type, ["Review case context and history"])


# Convenience functions for direct API use
def get_pending_work(db_path: str = None) -> List[Dict[str, Any]]:
    """Convenience function to get pending work with context.

    Caller is responsible for providing a database path; we never assume a
    hard-coded on-disk location (no DB_PATH constant exists in core).
    """
    if db_path is None:
        raise ValueError("db_path is required (no global DB_PATH constant exists)")
    conn = core.connect(db_path)
    try:
        return get_pending_human_tasks_with_context(conn)
    finally:
        conn.close()


def complete_task(task_id: str, completion_result: Dict[str, Any],
                  db_path: str = None) -> Dict[str, Any]:
    """Convenience wrapper around complete_human_task_with_evidence.

    Caller must supply a database path; we never assume a hard-coded location.
    """
    if db_path is None:
        raise ValueError("db_path is required (no global DB_PATH constant exists)")
    conn = core.connect(db_path)
    try:
        return complete_human_task_with_evidence(conn, task_id, completion_result)
    finally:
        conn.close()