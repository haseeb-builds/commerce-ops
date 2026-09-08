"""Commerce Ops Phase 1 — Case Engine persistence: Case entity."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from commerceops.core import connect, utcnow


def create_case(
    conn,
    shipment_id: str,
    policy_version: int = 1,
) -> str:
    """Create a new Case for a shipment.
    
    Args:
        conn: SQLite connection
        shipment_id: ID of the associated shipment
        policy_version: Version of policy to use (for future compatibility)
        
    Returns:
        The ID of the created case
    """
    case_id = uuid.uuid4().hex
    now = utcnow()
    
    conn.execute(
        """
        INSERT INTO case_entity (
            id, shipment_id, status, opened_at, updated_at, 
            latest_evidence_at, policy_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            shipment_id,
            "OPEN",
            now,
            now,
            now,  # latest_evidence_at starts as opened_at
            policy_version,
        ),
    )
    return case_id


def get_case(conn, case_id: str) -> Optional[dict]:
    """Get a Case by its ID.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case to retrieve
        
    Returns:
        Dictionary representing the case, or None if not found
    """
    row = conn.execute(
        "SELECT * FROM case_entity WHERE id=?", (case_id,)
    ).fetchone()
    return dict(row) if row else None


def get_case_by_shipment(conn, shipment_id: str) -> Optional[dict]:
    """Get the most recent open Case for a shipment.
    
    Args:
        conn: SQLite connection
        shipment_id: ID of the shipment
        
    Returns:
        Dictionary representing the case, or None if not found
    """
    row = conn.execute(
        """
        SELECT * FROM case_entity 
        WHERE shipment_id=? AND status='OPEN'
        ORDER BY updated_at DESC, rowid DESC LIMIT 1
        """,
        (shipment_id,)
    ).fetchone()
    return dict(row) if row else None


def update_case_status(conn, case_id: str, status: str) -> None:
    """Update the status of a Case.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case
        status: New status (must be one of OPEN, RESOLVED, ABANDONED)
    """
    now = utcnow()
    conn.execute(
        "UPDATE case_entity SET status=?, updated_at=? WHERE id=?",
        (status, now, case_id),
    )


def update_case_latest_evidence(conn, case_id: str, timestamp: str) -> None:
    """Update the latest_evidence_at timestamp for a Case.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case
        timestamp: ISO format timestamp of the latest evidence
    """
    conn.execute(
        "UPDATE case_entity SET latest_evidence_at=? WHERE id=?",
        (timestamp, case_id),
    )


def set_case_resolution(conn, case_id: str, resolution: dict) -> None:
    """Set the resolution for a Case.
    
    Args:
        conn: SQLite connection
        case_id: ID of the case
        resolution: Dictionary representing the resolution (will be JSON-encoded)
    """
    import json
    resolution_json = json.dumps(resolution)
    now = utcnow()
    conn.execute(
        "UPDATE case_entity SET resolution=?, updated_at=? WHERE id=?",
        (resolution_json, now, case_id),
    )


def get_open_cases(conn):
    """Get all open cases.
    
    Args:
        conn: SQLite connection
        
    Returns:
        List of dictionaries representing open cases
    """
    rows = conn.execute(
        "SELECT * FROM case_entity WHERE status='OPEN' ORDER BY opened_at"
    ).fetchall()
    return [dict(row) for row in rows]