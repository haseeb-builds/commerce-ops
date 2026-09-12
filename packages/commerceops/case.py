"""Commerce Ops Phase 1 — Case Engine persistence: Case entity."""

import uuid
import json
from commerceops.transactions import atomic
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


def update_case_status(conn, case_id: str, status: str, *, reason: str = "Explicit lifecycle decision") -> None:
    """Audit a deliberate lifecycle change and its evidence boundary atomically.

    OPEN after a closed status retains the closure boundary so old decisions
    cannot settle newly arrived evidence. Repeated identical changes are no-ops.
    """
    if status not in {"OPEN", "RESOLVED", "ABANDONED"}:
        raise ValueError(f"Invalid Case status: {status}")
    with atomic(conn):
        obj = get_case(conn, case_id)
        if obj is None:
            raise ValueError(f"Case not found: {case_id}")
        if obj["status"] == status:
            return
        boundary = evidence_boundary(conn, obj) if status == "OPEN" else None
        if boundary is None:
            boundary = {table: conn.execute(
                f"SELECT COALESCE(MAX(rowid),0) FROM {table} WHERE shipment_id=?",
                (obj["shipment_id"],)).fetchone()[0] for table in EVIDENCE_TABLES}
        now = utcnow()
        conn.execute(
            "INSERT INTO case_status_event (id,case_entity_id,from_status,to_status,changed_at,reason,"
            "tracking_cursor,customer_cursor,operator_cursor,resolution) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, case_id, obj["status"], status, now, reason,
             boundary["tracking_event"], boundary["customer_confirmation"], boundary["operator_action"], obj["resolution"]),
        )
        conn.execute("UPDATE case_entity SET status=?, updated_at=?, resolution=? WHERE id=?",
                     (status, now, None if status == "OPEN" else obj["resolution"], case_id))


EVIDENCE_TABLES = {"tracking_event": "imported_at", "customer_confirmation": "confirmed_at", "operator_action": "acted_at"}


def evidence_boundary(conn, obj):
    """Return the last closure scope; None for a Case never closed.

    Legacy closed Cases lack an audit boundary. NULL cursors mean unknown,
    not zero/new or a guessed cutoff from updated_at. Backdated records cannot
    safely be classified as already handled from timestamps alone.
    """
    row = conn.execute("SELECT * FROM case_status_event WHERE case_entity_id=? ORDER BY rowid DESC LIMIT 1",
                       (obj["id"],)).fetchone()
    if row:
        if obj["status"] != "OPEN" or row["from_status"] in {"RESOLVED", "ABANDONED"}:
            return dict(zip(EVIDENCE_TABLES, (row["tracking_cursor"], row["customer_cursor"], row["operator_cursor"])))
        return None
    if obj["status"] == "OPEN":
        return None
    return {table: None for table in EVIDENCE_TABLES}


def get_case_history(conn, case_id):
    """Read-only lifecycle audit, including prior resolution payloads.

    Resolution is coordination metadata. Preserve a damaged historical value
    as visible uncertainty instead of making detail/history reads fail.
    """
    rows = conn.execute("SELECT * FROM case_status_event WHERE case_entity_id=? ORDER BY rowid", (case_id,))
    history = []
    for row in rows:
        item = dict(row)
        raw = item.get("resolution")
        try:
            item["resolution"] = json.loads(raw) if raw else None
        except (TypeError, ValueError, json.JSONDecodeError):
            item["resolution"] = None
            item["resolution_error"] = "malformed lifecycle resolution"
        history.append(item)
    return history


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

def requirement_scope(conn, obj):
    """A reopen audit ID distinguishes renewed work from earlier completed work."""
    if obj["status"] != "OPEN":
        return None
    row = conn.execute("SELECT id, from_status FROM case_status_event WHERE case_entity_id=? ORDER BY rowid DESC LIMIT 1",
                       (obj["id"],)).fetchone()
    return row["id"] if row and row["from_status"] in {"RESOLVED", "ABANDONED"} else None
