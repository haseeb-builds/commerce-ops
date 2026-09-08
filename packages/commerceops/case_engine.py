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
from commerceops.transactions import atomic
from commerceops.timestamps import parse_timestamp


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


def evaluate_shipment(conn: sqlite3.Connection, shipment_id: str) -> EvaluationResult:
    """Explicit operational entry point: create once, otherwise reuse the Case.

    Resolved Cases reopen on new evidence within the same Case. Abandoned Cases
    require an explicit operator reopen. Every status change is audited.
    """
    from commerceops.actions import _check_shipment
    with atomic(conn):
        _check_shipment(conn, shipment_id)
        row = conn.execute(
            "SELECT id FROM case_entity WHERE shipment_id=? ORDER BY opened_at DESC, rowid DESC LIMIT 1",
            (shipment_id,),
        ).fetchone()
        case_id = row["id"] if row else case.create_case(conn, shipment_id)
        return evaluate_case(conn, case_id)


def assess_case(conn: sqlite3.Connection, case_id: str):
    """Load authoritative evidence and consult policy without materializing work.

    Write callers must hold their transaction across assessment and mutation.
    """
    case_obj = case.get_case(conn, case_id)
    if case_obj is None:
        raise ValueError(f"Case not found: {case_id}")
    shipment_id = case_obj["shipment_id"]
    evidence = _gather_evidence(conn, shipment_id)
    tasks = human_task.get_tasks_for_case(conn, case_id)
    follow_up_rows = followups.list_follow_ups(conn, shipment_id=shipment_id)
    coordination = policy.CoordinationState(
        case_id=case_id, shipment_id=shipment_id, status=case_obj["status"],
        opened_at=case_obj["opened_at"], updated_at=case_obj["updated_at"],
        latest_evidence_at=case_obj["latest_evidence_at"],
        resolution=json.loads(case_obj["resolution"]) if case_obj["resolution"] else None,
        policy_version=case_obj["policy_version"], human_tasks=tasks,
        autonomous_actions=autonomous_action.get_actions_for_case(conn, case_id),
        follow_ups=follow_up_rows["open"] + follow_up_rows["closed"],
        closed_evidence_boundary=case.evidence_boundary(conn, case_obj),
        requirement_scope=case.requirement_scope(conn, case_obj),
    )
    return case_obj, evidence, tasks, policy.evaluate_policy(evidence, coordination)


def task_matches_work(task: dict, assessment: dict) -> bool:
    """Completion requires an explicit, non-empty current evidence basis."""
    if (assessment["disposition"] != "HUMAN_TASK_REQUIRED"
            or task["type"] != assessment["capability"]):
        return False
    basis = (task["payload"] or {}).get("evidence_ids")
    return (isinstance(basis, list) and bool(basis)
            and all(isinstance(eid, str) for eid in basis)
            and sorted(basis) == assessment["work_evidence_ids"]
            and task["payload"].get("requirement_scope") == assessment.get("requirement_scope"))


def work_payload(assessment):
    """Only evidence references and, after reopening, the lifecycle audit ID."""
    payload = {"evidence_ids": assessment["work_evidence_ids"]}
    if assessment.get("requirement_scope"):
        payload["requirement_scope"] = assessment["requirement_scope"]
    return payload


def _reconcile_human_work(conn, case_id, tasks, assessment):
    """Retire obsolete RFD work without destroying its payload or history.

    FOLLOW_UP_ACTION is independently human-scheduled and is not superseded
    by the RFD policy. Completed tasks are immutable to this reconciliation.
    """
    active = [t for t in tasks if t["status"] in {"PENDING", "IN_PROGRESS"}
              and t["type"] in {"VERIFY_CUSTOMER", "DECIDE_ACTION"}]
    required_id = None
    if assessment["disposition"] == "HUMAN_TASK_REQUIRED":
        payload = work_payload(assessment)
        # Recover only when the immutable request hash PROVES this exact basis
        # was originally requested. An actual {} legacy request cannot pass.
        for task in active:
            if (task["type"] == assessment["capability"]
                    and "evidence_ids" not in (task["payload"] or {})
                    and task["idempotency_key"] == human_task.task_key(case_id, assessment["capability"], payload)):
                task["payload"] = {**(task["payload"] or {}), **payload}
                conn.execute("UPDATE human_task SET payload=? WHERE id=?",
                             (json.dumps(task["payload"]), task["id"]))
        matching = next((t for t in active if task_matches_work(t, assessment)), None)
        if matching:
            required_id = matching["id"]
        else:
            required_id = capability.execute_capability(
                conn, case_id, assessment["capability"], payload)
    # MONITOR alone is not authorization to discard a human obligation.
    if required_id is None and not assessment.get("retire_work", False):
        return
    for task in active:
        if task["id"] != required_id:
            human_task.supersede_human_task(
                conn, task["id"], reason=assessment["reason"],
                evidence_ids=assessment["work_evidence_ids"], replacement_task_id=required_id)


def evaluate_case(conn: sqlite3.Connection, case_id: str) -> EvaluationResult:
    """Atomically assess evidence, reconcile work and update coordination metadata."""
    with atomic(conn):
        case_obj, evidence, tasks, assessment = assess_case(conn, case_id)
        if assessment.get("reopen_case"):
            case.update_case_status(conn, case_id, "OPEN", reason=assessment.get("blocked_reason") or "New exception/customer evidence after resolution")
            case_obj, evidence, tasks, assessment = assess_case(conn, case_id)
        latest = _get_latest_evidence_timestamp(evidence)
        previous = parse_timestamp(case_obj["latest_evidence_at"])
        if latest and (previous is None or parse_timestamp(latest) > previous):
            case.update_case_latest_evidence(conn, case_id, latest)
        _reconcile_human_work(conn, case_id, tasks, assessment)
        if assessment["disposition"] == "AUTONOMOUS_ACTION_AVAILABLE":
            capability.plan_autonomous_action(conn, case_id, assessment["action_type"], {})
        elif assessment["disposition"] == "RESOLVE":
            case.update_case_status(conn, case_id, "RESOLVED")
        return EvaluationResult(
            case_id=case_id, disposition=assessment["disposition"],
            capability=assessment.get("capability"), action_type=assessment.get("action_type"),
            reason=assessment["reason"], evidence_ids=assessment["evidence_ids"],
            policy_version=assessment["policy_version"],
        )


def _gather_evidence(conn: sqlite3.Connection, shipment_id: str) -> List[policy.Evidence]:
    """Gather all evidence for a given shipment from the authoritative tables.

    Returns a list of policy.Evidence objects.
    """
    evidence = []

    # tracking_event
    rows = conn.execute(
        """
        SELECT rowid AS record_order, id, raw_code, raw_text, occurred_at, imported_at, event_key
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
                "record_order": row["record_order"],
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
        SELECT rowid AS record_order, id, kind, note, cancel_reason, actor, acted_at
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
                "record_order": row["record_order"],
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
        SELECT rowid AS record_order, id, channel, content, confirmed_at
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
                "record_order": row["record_order"],
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

    evidence.sort(key=lambda e: (policy.evidence_timestamp(e), e.id))
    return evidence


def _get_latest_evidence_timestamp(evidence: List[policy.Evidence]) -> Optional[str]:
    """Latest received evidence timestamp, not a fabricated courier occurrence time."""
    return max((value for e in evidence if (value := policy.evidence_timestamp(e))), default=None)
