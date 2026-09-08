"""Commerce Ops Phase 2 — Policy layer for Case Engine evaluation.

This module contains the policy/business logic that determines what
capability is required given the evidence and current coordination state.

The policy layer is pure and deterministic: given the same inputs, it
produces the same output. It does not perform any I/O or side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from commerceops.timestamps import timestamp_key



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


def evidence_timestamp(e: Evidence) -> str:
    """Ordering for Case evaluation: receipt time for courier claims.

    Source occurrence time remains untouched in the event store. Normalize
    offsets for comparison only; legacy naive timestamps are treated as UTC.
    """
    for field in ("imported_at", "occurred_at", "acted_at", "confirmed_at", "created_at"):
        value = e.data.get(field)
        if value is not None:
            return timestamp_key(value).isoformat()
    return "1970-01-01T00:00:00+00:00"


def evaluate_policy(evidence: List[Evidence], coordination: CoordinationState) -> dict:
    """Pure RFD policy. Work identity uses only the evidence requiring that work.

    All evidence IDs remain in the evaluation result for inspection. The small
    work_evidence_ids basis excludes notes, follow-up coordination and older
    cycles, so those cannot spuriously create another verification/decision.
    This preserves the existing text-based refusal routing; it does not infer
    whether the courier or customer is factually correct.
    """
    def result(disposition, reason, capability=None, basis=()):
        return {
            "disposition": disposition,
            "capability": capability,
            "action_type": None,
            "reason": reason,
            "evidence_ids": sorted(e.id for e in evidence),
            "work_evidence_ids": sorted(e.id for e in basis),
            "policy_version": coordination.policy_version,
        }

    if coordination.status != "OPEN":
        return result("MONITOR", f"Case is not open (status: {coordination.status}).")

    # Within each source, equal-time records follow append order, not random
    # UUID order. Cross-source "responded after" still requires a later instant;
    # independent tables' rowids do not establish cross-source causality.
    ordered = sorted(evidence, key=lambda e: (
        evidence_timestamp(e), e.data.get("record_order", 0), e.id), reverse=True)
    tracking = next((e for e in ordered if e.type == "tracking_event"), None)
    decision = next((e for e in ordered if e.type == "operator_action"
                     and e.data.get("kind") in DECISION_KINDS), None)
    if decision and not any(
        e.type in {"tracking_event", "customer_confirmation"}
        and evidence_timestamp(e) > evidence_timestamp(decision)
        for e in ordered
    ):
        return result("MONITOR", "Decision has been made and no new evidence.")

    if not tracking or tracking.data.get("raw_code") != "RFD":
        return result("MONITOR", "No unresolved obligation detected.")

    confirmation = next((e for e in ordered if e.type == "customer_confirmation"
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
