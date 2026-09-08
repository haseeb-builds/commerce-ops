"""Commerce Ops Phase 1 — Case Engine persistence: HumanTask entity."""

import uuid
import hashlib
from commerceops.transactions import atomic
from datetime import datetime, timezone
from typing import Optional, List
import json

from commerceops.core import connect, utcnow


def create_human_task(
    conn,
    case_id: str,
    task_type: str,
    payload: dict,
    idempotency_key: Optional[str] = None,
) -> str:
    """Create a new HumanTask.
    
    Args:
        conn: SQLite connection
        case_id: ID of the associated case
        task_type: Type of task (VERIFY_CUSTOMER, DECIDE_ACTION, FOLLOW_UP_ACTION)
        payload: Dictionary containing task-specific data
        idempotency_key: Optional key to prevent duplicate tasks
        
    Returns:
        The ID of the created task
    """
    with atomic(conn):
        key = idempotency_key or task_key(case_id, task_type, payload)
        previous = None
        # A cancellation closes a requirement incarnation, not its future.
        # Deriving the next key from the cancelled row makes concurrent retries
        # converge without changing the old row/key or creating a global cycle.
        while True:
            existing = conn.execute(
                "SELECT id, status FROM human_task WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is None:
                break
            if existing["status"] != "CANCELLED":
                return existing["id"]
            previous = existing["id"]
            key = hashlib.sha256(f"{key}:after-cancellation:{previous}".encode()).hexdigest()
        task_id = uuid.uuid4().hex
        now = utcnow()
        stored_payload = dict(payload)
        if previous:
            stored_payload["replaces_cancelled_task_id"] = previous
        conn.execute(
            "INSERT INTO human_task (id, case_entity_id, type, capability, status, payload, "
            "idempotency_key, created_at, updated_at) VALUES (?,?,?,?, 'PENDING',?,?,?,?)",
            (task_id, case_id, task_type, task_type, json.dumps(stored_payload), key, now, now),
        )
        return task_id


def task_key(case_id, task_type, payload):
    """Immutable request identity, also usable to verify a damaged legacy payload."""
    material = f"{case_id}:{task_type}:{json.dumps(payload, sort_keys=True)}"
    return hashlib.sha256(material.encode()).hexdigest()


def get_human_task(conn, task_id: str) -> Optional[dict]:
    """Get a HumanTask by its ID.
    
    Args:
        conn: SQLite connection
        task_id: ID of the task to retrieve
        
    Returns:
        Dictionary representing the task, or None if not found
    """
    import json
    row = conn.execute(
        "SELECT * FROM human_task WHERE id=?", (task_id,)
    ).fetchone()
    if row:
        task = dict(row)
        # Rename case_entity_id to case_id for compatibility
        task["case_id"] = task.pop("case_entity_id")
        task["payload"] = json.loads(task["payload"]) if task["payload"] else None
        return task
    return None


def get_tasks_for_case(conn, case_id: str) -> List[dict]:
    """Get all tasks for a Case.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case
        
    Returns:
        List of dictionaries representing tasks
    """
    import json
    rows = conn.execute(
        "SELECT * FROM human_task WHERE case_entity_id=? ORDER BY created_at",
        (case_id,)
    ).fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        # Rename case_entity_id to case_id for compatibility
        task["case_id"] = task.pop("case_entity_id")
        task["payload"] = json.loads(task["payload"]) if task["payload"] else None
        tasks.append(task)
    return tasks


def update_human_task_status(
    conn, task_id: str, status: str, completed_at: Optional[str] = None
) -> None:
    """Update the status of a HumanTask.
    
    Args:
        conn: SQLite connection
        task_id: ID of the task
        status: New status (PENDING, IN_PROGRESS, COMPLETED, CANCELLED)
        completed_at: Optional completion timestamp (defaults to now)
    """
    now = utcnow() if completed_at is None else completed_at
    conn.execute(
        """
        UPDATE human_task 
        SET status=?, updated_at=?, completed_at=?
        WHERE id=?
        """,
        (status, now, now if status in {"COMPLETED", "CANCELLED"} else None, task_id),
    )


def complete_human_task(conn, task_id: str, result: dict) -> None:
    """Mark a HumanTask as completed with a result.
    
    Args:
        conn: SQLite connection
        task_id: ID of the task
        result: Dictionary containing the task result
    """
    import json
    now = utcnow()
    conn.execute(
        """
        UPDATE human_task 
        SET status='COMPLETED', payload=json(?), updated_at=?, completed_at=?
        WHERE id=?
        """,
        (json.dumps(result), now, now, task_id),
    )


def get_pending_tasks(conn, *, include_in_progress: bool = False) -> List[dict]:
    """Get all pending tasks.
    
    Args:
        conn: SQLite connection
        
    Returns:
        List of dictionaries representing pending tasks
    """
    import json
    statuses = ("PENDING", "IN_PROGRESS") if include_in_progress else ("PENDING",)
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM human_task WHERE status IN ({placeholders}) ORDER BY created_at, rowid", statuses
    ).fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        task["payload"] = json.loads(task["payload"]) if task["payload"] else None
        tasks.append(task)
    return tasks

def supersede_human_task(conn, task_id: str, *, reason: str,
                         evidence_ids: List[str], replacement_task_id: Optional[str]) -> None:
    """Cancel obsolete work with a retained explanation and replacement reference."""
    task = get_human_task(conn, task_id)
    if task is None or task["status"] not in {"PENDING", "IN_PROGRESS"}:
        return
    now = utcnow()
    payload = {**(task["payload"] or {}), "superseded": {
        "reason": reason, "evidence_ids": evidence_ids,
        "replacement_task_id": replacement_task_id, "at": now,
    }}
    conn.execute(
        "UPDATE human_task SET status='CANCELLED', payload=?, updated_at=?, completed_at=? "
        "WHERE id=? AND status IN ('PENDING', 'IN_PROGRESS')",
        (json.dumps(payload), now, now, task_id),
    )
