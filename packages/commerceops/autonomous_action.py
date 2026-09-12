"""Commerce Ops Phase 1 — Case Engine persistence: AutonomousAction entity."""

import uuid
from datetime import datetime, timezone
from typing import Optional, List
import json

from commerceops.core import connect, utcnow


def _decode_payload(raw):
    """Keep malformed coordination rows readable and explicitly untrusted."""
    if raw is None:
        return None, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "malformed autonomous-action payload"
    if not isinstance(value, dict):
        return None, "autonomous-action payload is not an object"
    return value, None


def create_autonomous_action(
    conn,
    case_id: str,
    action_type: str,
    payload: dict,
    idempotency_key: Optional[str] = None,
) -> str:
    """Create a new AutonomousAction (initially in PLANNED state).
    
    Args:
        conn: SQLite connection
        case_id: ID of the associated case
        action_type: Type of action (e.g., 'SEND_REATTEMPT_REQUEST')
        payload: Dictionary containing action-specific data
        idempotency_key: Optional key to prevent duplicate actions
        
    Returns:
        The ID of the created action
    """
    action_id = uuid.uuid4().hex
    now = utcnow()
    
    # Generate idempotency key if not provided
    if idempotency_key is None:
        # Create a stable idempotency key based on case_id, action_type, and payload content
        # This avoids fragile timestamp-based identity
        import hashlib
        payload_str = json.dumps(payload, sort_keys=True)
        key_material = f"{case_id}:{action_type}:{payload_str}"
        idempotency_key = hashlib.sha256(key_material.encode()).hexdigest()
    
    existing = conn.execute(
        "SELECT id FROM autonomous_action WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone()
    if existing:
        return existing["id"]
    conn.execute(
        """
        INSERT INTO autonomous_action (
            id, case_entity_id, action_type, status, payload, 
            idempotency_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            case_id,
            action_type,
            "PLANNED",
            json.dumps(payload),
            idempotency_key,
            now,
            now,
        ),
    )
    return action_id


def get_autonomous_action(conn, action_id: str) -> Optional[dict]:
    """Get an AutonomousAction by its ID.
    
    Args:
        conn: SQLite connection
        action_id: ID of the action to retrieve
        
    Returns:
        Dictionary representing the action, or None if not found
    """
    import json
    row = conn.execute(
        "SELECT * FROM autonomous_action WHERE id=?", (action_id,)
    ).fetchone()
    if row:
        action = dict(row)
        # Rename case_entity_id to case_id for compatibility
        action["case_id"] = action.pop("case_entity_id")
        action["payload"], payload_error = _decode_payload(action["payload"])
        if payload_error:
            action["payload_error"] = payload_error
        return action
    return None


def get_actions_for_case(conn, case_id: str) -> List[dict]:
    """Get all actions for a Case.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case
        
    Returns:
        List of dictionaries representing actions
    """
    import json
    rows = conn.execute(
        "SELECT * FROM autonomous_action WHERE case_entity_id=? ORDER BY created_at",
        (case_id,)
    ).fetchall()
    actions = []
    for row in rows:
        action = dict(row)
        # Rename case_entity_id to case_id for compatibility
        action["case_id"] = action.pop("case_entity_id")
        action["payload"], payload_error = _decode_payload(action["payload"])
        if payload_error:
            action["payload_error"] = payload_error
        actions.append(action)
    return actions


def update_autonomous_action_status(
    conn, action_id: str, status: str, executed_at: Optional[str] = None
) -> None:
    """Update the status of an AutonomousAction.
    
    Args:
        conn: SQLite connection
        action_id: ID of the action
        status: New status (PLANNED, EXECUTED, FAILED)
        executed_at: Optional execution timestamp (defaults to now for EXECUTED)
    """
    now = utcnow()
    if status == "EXECUTED" and executed_at is None:
        executed_at = now
    elif status != "EXECUTED":
        executed_at = None
        
    conn.execute(
        """
        UPDATE autonomous_action 
        SET status=?, updated_at=?, executed_at=?
        WHERE id=?
        """,
        (status, now, executed_at, action_id),
    )


def mark_autonomous_action_executed(
    conn, action_id: str, result: dict
) -> None:
    """Mark an AutonomousAction as EXECUTED with a result.
    
    Args:
        conn: SQLite connection
        action_id: ID of the action
        result: Dictionary containing the execution result
    """
    import json
    now = utcnow()
    conn.execute(
        """
        UPDATE autonomous_action 
        SET status='EXECUTED', payload=json(?), updated_at=?, executed_at=?
        WHERE id=?
        """,
        (json.dumps(result), now, now, action_id),
    )


def mark_autonomous_action_failed(
    conn, action_id: str, failure_info: dict
) -> None:
    """Mark an AutonomousAction as FAILED with failure information.
    
    Args:
        conn: SQLite connection
        action_id: ID of the action
        failure_info: Dictionary containing failure information
    """
    import json
    now = utcnow()
    conn.execute(
        """
        UPDATE autonomous_action 
        SET status='FAILED', updated_at=?, failure_info=json(?)
        WHERE id=?
        """,
        (now, json.dumps(failure_info), action_id),
    )


def get_planned_actions(conn) -> List[dict]:
    """Get all pending (PLANNED) autonomous actions.
    
    Args:
        conn: SQLite connection
        
    Returns:
        List of dictionaries representing planned actions
    """
    import json
    rows = conn.execute(
        "SELECT * FROM autonomous_action WHERE status='PLANNED' ORDER BY created_at"
    ).fetchall()
    actions = []
    for row in rows:
        action = dict(row)
        action["payload"], payload_error = _decode_payload(action["payload"])
        if payload_error:
            action["payload_error"] = payload_error
        actions.append(action)
    return actions