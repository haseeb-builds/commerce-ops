"""Commerce Ops Phase 2 — Policy layer for Case Engine evaluation.

This module contains the policy/business logic that determines what
capability is required given the evidence and current coordination state.

The policy layer is pure and deterministic: given the same inputs, it
produces the same output. It does not perform any I/O or side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from commerceops.timestamps import parse_timestamp, invalid_timestamp_fields



# Only these existing operator kinds settle decision work. Notes, queries and
# open permission remain recordable but do not themselves decide the outcome.
DECISION_KINDS = frozenset({
    "reattempt_requested", "cancel_decided", "delivered_confirmed", "returned_confirmed",
})


@dataclass(frozen=True)
class Evidence:
    """A piece of evidence relevant to a case."""
    id: str
    type: str  # e.g., 'tracking_event', 'operator_action', 'customer_confirmation', 'follow_up'
    data: dict  # the raw data from the table, as a dictionary


@dataclass(frozen=True)
class CoordinationState:
    """Current coordination state for a case."""
    case_id: str
    shipment_id: str
    status: str  # OPEN, RESOLVED, ABANDONED
    opened_at: str
    updated_at: str
    latest_evidence_at: str
    resolution: Optional[dict]  # JSON deserialized, or None
    policy_version: int
    human_tasks: List[dict]  # each task as a dict, with case_entity_id renamed to case_id
    autonomous_actions: List[dict]  # each action as a dict, with case_entity_id renamed to case_id
    follow_ups: List[dict]  # from follow_up table
    closed_evidence_boundary: Optional[dict] = None
    requirement_scope: Optional[str] = None


def evidence_timestamp(e: Evidence) -> str:
    """Ordering for Case evaluation: receipt time for courier claims.

    Source occurrence time remains untouched in the event store. Normalize
    offsets for comparison only; legacy naive timestamps are treated as UTC.
    """
    for field in ("imported_at", "occurred_at", "acted_at", "confirmed_at", "created_at"):
        value = e.data.get(field)
        if value is not None:
            parsed = parse_timestamp(value)
            return parsed.isoformat() if parsed is not None else ""
    return ""


def evaluate_policy(evidence: List[Evidence], coordination: CoordinationState) -> dict:
    """Pure RFD policy. Work identity uses only the evidence requiring that work.

    All evidence IDs remain in the evaluation result for inspection. The small
    work_evidence_ids basis excludes notes, follow-up coordination and older
    cycles, so those cannot spuriously create another verification/decision.
    This preserves the existing text-based refusal routing; it does not infer
    whether the courier or customer is factually correct.
    """
    boundary = coordination.closed_evidence_boundary
    def since_closure(e):
        return (boundary is None or boundary.get(e.type) is None
                or e.data.get("record_order", 0) > boundary[e.type])
    reopen = coordination.status == "RESOLVED" and any(
        e.type in {"tracking_event", "customer_confirmation"} and since_closure(e) for e in evidence)

    def result(disposition, reason, capability=None, basis=(), *, retire_work=False):
        return {
            "disposition": disposition,
            "capability": capability,
            "action_type": None,
            "reason": reason,
            "evidence_ids": sorted(e.id for e in evidence),
            "work_evidence_ids": sorted(e.id for e in basis),
            # IDs are retained for API compatibility, but task identity also
            # carries the authoritative source.  A courier row and a customer
            # row must not become interchangeable merely because a damaged or
            # imported database reused an ID.
            "work_evidence_refs": sorted(f"{e.type}:{e.id}" for e in basis),
            "policy_version": coordination.policy_version,
            "retire_work": retire_work,
            "reopen_case": reopen,
            "requirement_scope": coordination.requirement_scope,
        }

    if coordination.status != "OPEN" and not reopen:
        return result("MONITOR", f"Case is not open (status: {coordination.status}).", retire_work=True)

    if boundary is not None and any(cursor is None for cursor in boundary.values()):
        blocked = "Legacy closure boundary is unknown; review history and record an explicit lifecycle decision before completing work"
        review = result("HUMAN_TASK_REQUIRED", blocked, "DECIDE_ACTION",
                        [e for e in evidence if e.type in {"tracking_event", "customer_confirmation", "operator_action"}])
        review["blocked_reason"] = blocked
        return review

    if any(invalid_timestamp_fields(e.type, e.data) for e in evidence):
        blocked = "Legacy timestamp chronology is uncertain; review source records before completing Case work"
        review = result("HUMAN_TASK_REQUIRED", blocked, "DECIDE_ACTION",
                        [e for e in evidence if e.type in {"tracking_event", "customer_confirmation"}
                         or invalid_timestamp_fields(e.type, e.data)])
        review["blocked_reason"] = blocked
        return review

    # Within each source, equal-time records follow append order, not random
    # UUID order. Cross-source "responded after" still requires a later instant;
    # independent tables' rowids do not establish cross-source causality.
    ordered = sorted(evidence, key=lambda e: (
        evidence_timestamp(e), e.data.get("record_order", 0), e.id), reverse=True)
    tracking = next((e for e in ordered if e.type == "tracking_event" and since_closure(e)), None)
    if tracking is None:
        tracking = next((e for e in ordered if e.type == "tracking_event"), None)
    decision = next((e for e in ordered if e.type == "operator_action"
                     and e.data.get("kind") in DECISION_KINDS and since_closure(e)), None)
    if decision and not any(
        e.type in {"tracking_event", "customer_confirmation"} and since_closure(e)
        and evidence_timestamp(e) >= evidence_timestamp(decision)
        for e in ordered
    ):
        return result("MONITOR", "Decision has been made and no new evidence.", retire_work=True)

    if not tracking:
        return result("MONITOR", "No courier obligation classified; existing work is retained.")
    if tracking.data.get("raw_code") != "RFD":
        # Unknown semantics are not evidence of resolution. Reuse human decision
        # capability for explicit review, without inventing a courier taxonomy.
        customer = next((e for e in ordered if e.type == "customer_confirmation" and since_closure(e)), None)
        basis = [tracking] + ([customer] if customer else [])
        return result("HUMAN_TASK_REQUIRED", "Unclassified courier observation; human review and decision required.",
                      "DECIDE_ACTION", basis)

    confirmation = next((e for e in ordered if e.type == "customer_confirmation" and since_closure(e)
                         and evidence_timestamp(e) > evidence_timestamp(tracking)), None)
    if confirmation is None:
        return result("HUMAN_TASK_REQUIRED", "No customer confirmation after RFD; need to verify.",
                      "VERIFY_CUSTOMER", [tracking])

    # The latest response to THIS observation matters, including in later cycles.
    # An old reattempt must not short-circuit a fresh customer response.
    content = confirmation.data.get("content", "").lower()
    if "refuse" in content:
        reason = ("Customer denied refusal (contradiction); need to decide."
                  if "not" in content else
                  "Customer confirmed refusal; need to decide on action.")
        return result("HUMAN_TASK_REQUIRED", reason, "DECIDE_ACTION", [tracking, confirmation])
    return result("HUMAN_TASK_REQUIRED", "Customer confirmation does not verify refusal; need to verify.",
                  "VERIFY_CUSTOMER", [tracking, confirmation])
