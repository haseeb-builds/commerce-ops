"""Commerce Ops Phase 2 — Case Engine evaluation core.

This module implements the evaluate_case function that orchestrates the
exception lifecycle by loading evidence, consulting the policy layer,
and materializing required work (human tasks or autonomous actions).

The implementation is designed to be deterministic and idempotent.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from commerceops import core
from commerceops import policy
from commerceops import case
from commerceops import human_task
from commerceops import autonomous_action
from commerceops import followups
from commerceops import capability


@dataclass(frozen=True)
class EvaluationResult:
    """The result of evaluating a case.

    Attributes:
        case_id: The ID of the case that was evaluated.
        disposition: One of 'HUMAN_TASK_REQUIRED', 'AUTONOMOUS_ACTION_AVAILABLE',
            'MONITOR', 'RESOLVE'.
        capability: The required human capability (if disposition is HUMAN_TASK_REQUIRED).
        action_type: The proposed autonomous action type (if disposition is
            AUTONOMOUS_ACTION_AVAILABLE).
        reason: A string explaining the decision.
        evidence_ids: List of evidence IDs that were considered in the evaluation.
        policy_version: The policy version used for this evaluation.
    """
    case_id: str
    disposition: str
    capability: Optional[str] = None
    action_type: Optional[str] = None
    reason: str = ""
    evidence_ids: List[str] = None
    policy_version: int = 1

    def __post_init__(self):
        if self.evidence_ids is None:
            object.__setattr__(self, "evidence_ids", [])


def evaluate_case(conn: sqlite3.Connection, case_id: str) -> EvaluationResult:
    """Evaluate a case and return the required next capability or action.

    Args:
        conn: An open SQLite connection (with foreign keys enabled).
        case_id: The ID of the case to evaluate.

    Returns:
        An EvaluationResult indicating what the policy layer determines
        is required to move the case forward.

    Side effects:
        - May update the case's latest_evidence_at if new evidence is found.
        - May create new human tasks or autonomous actions (idempotently).
        - May update the case status to RESOLVED if the policy dictates.

    Note:
        The function is safe to run repeatedly for the same case_id and
        will not create duplicate work due to idempotency checks.
    """
    # 1. Load the case.
    case_obj = case.get_case(conn, case_id)
    if case_obj is None:
        raise ValueError(f"Case not found: {case_id}")

    # 2. Load the current coordination state from the case object.
    # We'll also load the associated shipment_id for evidence gathering.
    shipment_id = case_obj["shipment_id"]

    # 3. Gather all evidence relevant to this shipment.
    # Evidence includes tracking_event, operator_action, customer_confirmation, and follow_up.
    # We will also consider the shipment's current_state as evidence? 
    # According to the spec, the shipment's current_state is derived from the event tables,
    # so we don't need to include it separately to avoid duplication.
    evidence = _gather_evidence(conn, shipment_id)

    # 4. Load current coordination state (human tasks, autonomous actions, follow-ups).
    human_tasks = human_task.get_tasks_for_case(conn, case_id)
    autonomous_actions = autonomous_action.get_actions_for_case(conn, case_id)
    follow_ups_dict = followups.list_follow_ups(conn, shipment_id=shipment_id)
    follow_ups = (
        follow_ups_dict.get("open", [])
        + follow_ups_dict.get("overdue", [])
        + follow_ups_dict.get("upcoming", [])
        + follow_ups_dict.get("closed", [])
    )

    # 5. Build the coordination state for the policy layer.
    coordination = policy.CoordinationState(
        case_id=case_id,
        shipment_id=shipment_id,
        status=case_obj["status"],
        opened_at=case_obj["opened_at"],
        updated_at=case_obj["updated_at"],
        latest_evidence_at=case_obj["latest_evidence_at"],
        resolution=json.loads(case_obj["resolution"]) if case_obj["resolution"] else None,
        policy_version=case_obj["policy_version"],
        human_tasks=human_tasks,
        autonomous_actions=autonomous_actions,
        follow_ups=follow_ups,
    )

    # 6. Ask the policy layer what to do.
    policy_result = policy.evaluate_policy(evidence, coordination)

    # 7. Materialize the required work (if any) and update case metadata.
    # We will do this in a transaction to ensure atomicity.
    need_to_commit = not conn.in_transaction
    if need_to_commit:
        conn.execute("BEGIN")
    try:
        # Update the case's latest_evidence_at to the most recent evidence timestamp.
        # We'll find the maximum timestamp from the evidence we gathered.
        latest_evidence_ts = _get_latest_evidence_timestamp(evidence)
        if latest_evidence_ts and latest_evidence_ts > case_obj["latest_evidence_at"]:
            case.update_case_latest_evidence(conn, case_id, latest_evidence_ts)

        # Depending on the policy result, we may need to create work.
        disposition = policy_result["disposition"]
        if disposition == "HUMAN_TASK_REQUIRED":
            capability_name = policy_result["capability"]
            # Tie the task's idempotency key to the evidence cycle that
            # requires it.  Re-evaluating unchanged evidence returns the same
            # pending task; a genuinely new courier/customer record can open
            # a new task after the prior one was completed.
            payload = {
                "evidence_ids": sorted(policy_result["evidence_ids"]),
            }
            capability.execute_capability(
                conn,
                case_id,
                capability_name,
                payload,
            )
        elif disposition == "AUTONOMOUS_ACTION_AVAILABLE":
            action_type = policy_result["action_type"]
            # Plan the autonomous action through the capability layer
            # The capability layer handles idempotency and creates PLANNED action records
            payload = {}  # Policy layer should provide payload in future iterations
            capability.plan_autonomous_action(
                conn,
                case_id,
                action_type,
                payload,
            )
        elif disposition == "RESOLVE":
            # Update the case status to RESOLVED.
            case.update_case_status(conn, case_id, "RESOLVED")
            # We might also want to set a resolution payload based on the policy result.
            # For now, we'll leave the resolution as is (it might already be set).
            # We could update it with the policy result's reason.
            # But we'll leave it to the policy layer to decide what to put in resolution.
            # We'll skip for now.
            pass
        # For MONITOR, we do nothing.

        if need_to_commit:
            conn.commit()
    except Exception:
        if need_to_commit:
            conn.rollback()
        raise

    # 8. Return the evaluation result.
    # We'll use the policy result, but we need to convert it to an EvaluationResult.
    # We'll also include the evidence ids we gathered.
    evidence_ids = [e.id for e in evidence]
    return EvaluationResult(
        case_id=case_id,
        disposition=policy_result["disposition"],
        capability=policy_result.get("capability"),
        action_type=policy_result.get("action_type"),
        reason=policy_result.get("reason", ""),
        evidence_ids=evidence_ids,
        policy_version=policy_result.get("policy_version", 1),
    )


def _gather_evidence(conn: sqlite3.Connection, shipment_id: str) -> List[policy.Evidence]:
    """Gather all evidence for a given shipment from the authoritative tables.

    Returns a list of policy.Evidence objects.
    """
    evidence = []

    # tracking_event
    rows = conn.execute(
        """
        SELECT id, raw_code, raw_text, occurred_at, imported_at, event_key
        FROM tracking_event
        WHERE shipment_id=?
        """,
        (shipment_id,),
    ).fetchall()
    for row in rows:
        evidence.append(policy.Evidence(
            id=row["id"],
            type="tracking_event",
            data={
                "raw_code": row["raw_code"],
                "raw_text": row["raw_text"],
                "occurred_at": row["occurred_at"],
                "imported_at": row["imported_at"],
                "event_key": row["event_key"],
            }
        ))

    # operator_action
    rows = conn.execute(
        """
        SELECT id, kind, note, cancel_reason, actor, acted_at
        FROM operator_action
        WHERE shipment_id=?
        """,
        (shipment_id,),
    ).fetchall()
    for row in rows:
        evidence.append(policy.Evidence(
            id=row["id"],
            type="operator_action",
            data={
                "kind": row["kind"],
                "note": row["note"],
                "cancel_reason": row["cancel_reason"],
                "actor": row["actor"],
                "acted_at": row["acted_at"],
            }
        ))

    # customer_confirmation
    rows = conn.execute(
        """
        SELECT id, channel, content, confirmed_at
        FROM customer_confirmation
        WHERE shipment_id=?
        """,
        (shipment_id,),
    ).fetchall()
    for row in rows:
        evidence.append(policy.Evidence(
            id=row["id"],
            type="customer_confirmation",
            data={
                "channel": row["channel"],
                "content": row["content"],
                "confirmed_at": row["confirmed_at"],
            }
        ))

    # follow_up
    rows = conn.execute(
        """
        SELECT id, reason, due_at, status, created_at, closed_at, closed_by_action
        FROM follow_up
        WHERE shipment_id=?
        """,
        (shipment_id,),
    ).fetchall()
    for row in rows:
        evidence.append(policy.Evidence(
            id=row["id"],
            type="follow_up",
            data={
                "reason": row["reason"],
                "due_at": row["due_at"],
                "status": row["status"],
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
                "closed_by_action": row["closed_by_action"],
            }
        ))

    # We could also consider the shipment's current_state as evidence, but it is derived from the above.
    # To avoid duplication, we skip it.

    # Sort the evidence by timestamp and then by rowid (or id) for deterministic ordering.
    # We'll define a helper to extract a timestamp from each evidence piece.
    def get_evidence_ts(e: policy.Evidence) -> str:
        # Each evidence type has a timestamp field, but they have different names.
        # We'll try to get a timestamp from the data dictionary.
        data = e.data
        # Common timestamp fields: occurred_at, imported_at, acted_at, confirmed_at, created_at, due_at
        # We'll pick the first one that exists and is not None.
        for ts_field in ("occurred_at", "imported_at", "acted_at", "confirmed_at", "created_at", "due_at"):
            if ts_field in data and data[ts_field] is not None:
                return data[ts_field]
        # If none found, we'll use a very old timestamp to put them at the beginning.
        return "1970-01-01T00:00:00+00:00"

    # We'll sort by timestamp, and then by id to break ties.
    evidence.sort(key=lambda e: (get_evidence_ts(e), e.id))
    return evidence


def _get_latest_evidence_timestamp(evidence: List[policy.Evidence]) -> Optional[str]:
    """Return the latest timestamp from a list of evidence, or None if no evidence."""
    if not evidence:
        return None

    def get_evidence_ts(e: policy.Evidence) -> str:
        data = e.data
        for ts_field in ("occurred_at", "imported_at", "acted_at", "confirmed_at", "created_at", "due_at"):
            if ts_field in data and data[ts_field] is not None:
                return data[ts_field]
        return "1970-01-01T00:00:00+00:00"

    latest = max(evidence, key=get_evidence_ts)
    return get_evidence_ts(latest)
