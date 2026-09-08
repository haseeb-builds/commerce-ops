"""Commerce Ops Phase 2 — Policy layer for Case Engine evaluation.

This module contains the policy/business logic that determines what
capability is required given the evidence and current coordination state.

The policy layer is pure and deterministic: given the same inputs, it
produces the same output. It does not perform any I/O or side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from commerceops import core


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


def evaluate_policy(
    evidence: List[Evidence],
    coordination: CoordinationState,
) -> dict:
    """Determine what capability is required based on evidence and coordination state.

    Returns a dictionary with keys:
        - disposition: one of 'HUMAN_TASK_REQUIRED', 'AUTONOMOUS_ACTION_AVAILABLE', 'MONITOR', 'RESOLVE'
        - capability: required capability (if disposition is HUMAN_TASK_REQUIRED)
        - action_type: proposed action type (if disposition is AUTONOMOUS_ACTION_AVAILABLE)
        - reason: a string explaining the decision
        - evidence_ids: list of evidence ids that were considered
        - policy_version: the policy version used

    This policy handles the RFD examples from the task description.
    """
    # If the case is not open, we monitor (or do nothing). We'll return MONITOR.
    if coordination.status != "OPEN":
        return {
            "disposition": "MONITOR",
            "capability": None,
            "action_type": None,
            "reason": f"Case is not open (status: {coordination.status}).",
            "evidence_ids": [e.id for e in evidence],
            "policy_version": coordination.policy_version,
        }

    # We'll find the latest evidence of each type by timestamp.
    # We'll define a helper to extract a timestamp from an evidence piece.
    def get_evidence_ts(e: Evidence) -> str:
        data = e.data
        # Common timestamp fields: occurred_at, imported_at, acted_at, confirmed_at, created_at, due_at
        for ts_field in ("occurred_at", "imported_at", "acted_at", "confirmed_at", "created_at", "due_at"):
            if ts_field in data and data[ts_field] is not None:
                return data[ts_field]
        # If none found, we'll use a very old timestamp to put them at the beginning.
        return "1970-01-01T00:00:00+00:00"

    # Sort evidence by timestamp (newest first) and then by id to break ties.
    evidence_sorted = sorted(evidence, key=lambda e: (get_evidence_ts(e), e.id), reverse=True)

    # We'll initialize the latest of each type to None.
    latest_tracking_event = None
    latest_customer_confirmation = None
    latest_operator_action = None

    # We'll also keep track of the latest evidence overall to see what the newest event is.
    latest_evidence_overall = evidence_sorted[0] if evidence_sorted else None

    # We'll iterate through the sorted evidence to find the latest of each type.
    for e in evidence_sorted:
        if e.type == "tracking_event" and latest_tracking_event is None:
            latest_tracking_event = e
        elif e.type == "customer_confirmation" and latest_customer_confirmation is None:
            latest_customer_confirmation = e
        elif e.type == "operator_action" and latest_operator_action is None:
            latest_operator_action = e

    # Now we have the latest of each type (if any).

    # We'll also consider the coordination state: the case's latest_evidence_at should be <= the latest evidence timestamp.
    # But we don't need that for the policy.

    # We'll now apply the rules.

    # Rule 1: If there is a latest operator_action that is a terminal kind, then we have made a decision.
    #   Terminal kinds are: cancel_decided, delivered_confirmed, returned_confirmed
    terminal_kinds = {"cancel_decided", "delivered_confirmed", "returned_confirmed"}
    if latest_operator_action and latest_operator_action.data.get("kind") in terminal_kinds:
        # We have made a decision. Now we need to see if there is any new evidence after this decision.
        # We'll check if there is any evidence (tracking_event or customer_confirmation) that is newer than this operator_action.
        # If there is, then we need to re-evaluate based on that new evidence.
        # We'll compare timestamps.
        latest_op_ts = get_evidence_ts(latest_operator_action)
        # Check for newer tracking_event or customer_confirmation
        newer_tracking = False
        newer_confirmation = False
        for e in evidence_sorted:
            if get_evidence_ts(e) > latest_op_ts:
                if e.type == "tracking_event":
                    newer_tracking = True
                elif e.type == "customer_confirmation":
                    newer_confirmation = True
        # If there is newer evidence, we need to process it.
        # But for simplicity, we'll assume that if there is newer evidence, we will handle it in the next evaluation.
        # For now, we'll say that if there is no newer evidence, we can monitor.
        if not newer_tracking and not newer_confirmation:
            # We have made a decision and there is no new evidence, so we monitor.
            return {
                "disposition": "MONITOR",
                "capability": None,
                "action_type": None,
                "reason": "Decision has been made and no new evidence.",
                "evidence_ids": [e.id for e in evidence],
                "policy_version": coordination.policy_version,
            }
        # If there is newer evidence, we fall through to the next rules.

    # Rule 2: If we have not returned yet, and there is a latest operator_action that is a decision kind (like reattempt_requested),
    #         and there is no new tracking_event after it, then we monitor.
    #         We'll consider reattempt_requested as a decision kind.
    decision_kinds = {"reattempt_requested", "cancel_decided"}  # note: cancel_decided is also terminal, but we already handled terminal above.
    if latest_operator_action and latest_operator_action.data.get("kind") in decision_kinds:
        # Check if there is any newer tracking_event or customer_confirmation.
        latest_op_ts = get_evidence_ts(latest_operator_action)
        newer_tracking = False
        newer_confirmation = False
        for e in evidence_sorted:
            if get_evidence_ts(e) > latest_op_ts:
                if e.type == "tracking_event":
                    newer_tracking = True
                elif e.type == "customer_confirmation":
                    newer_confirmation = True
        if not newer_tracking and not newer_confirmation:
            # We have made a decision and there is no new evidence, so we monitor.
            return {
                "disposition": "MONITOR",
                "capability": None,
                "action_type": None,
                "reason": "Decision has been made and no new evidence.",
                "evidence_ids": [e.id for e in evidence],
                "policy_version": coordination.policy_version,
            }

    # Rule 3: If there is a latest tracking_event that is an exception code (we'll assume RFD for now) and
    #         there is no customer confirmation that verifies it (or there is a contradiction), then we need to verify.
    #         But note: we have to consider the timeline.
    #         We'll check if the latest tracking_event is RFD and if there is a customer confirmation that is after it and confirms the refusal.
    #         If there is a customer confirmation after the tracking_event that confirms the refusal, then verification is satisfied.
    #         If there is a customer confirmation after the tracking_event that contradicts the refusal, then we have a contradiction and we need to decide.
    #         If there is no customer confirmation after the tracking_event, then we need to verify.
    if latest_tracking_event and latest_tracking_event.data.get("raw_code") == "RFD":
        # Check if there is a customer confirmation after this tracking_event.
        tracking_ts = get_evidence_ts(latest_tracking_event)
        # We'll look for customer confirmations that are after this tracking_event.
        confirmations_after = []
        for e in evidence_sorted:
            if e.type == "customer_confirmation" and get_evidence_ts(e) > tracking_ts:
                confirmations_after.append(e)
        if confirmations_after:
            # We have at least one customer confirmation after the tracking_event.
            # We'll take the latest one (since evidence_sorted is newest first, the first one we encounter is the latest).
            latest_confirmation_after = confirmations_after[0]
            # Check the content of the confirmation.
            content = latest_confirmation_after.data.get("content", "").lower()
            # Check for refusal confirmation: "refuse" or "refused" present AND "not" NOT present (to avoid catching denials)
            refuse_or_refused_in_content = ("refuse" in content or "refused" in content)
            not_in_content = "not" in content
            if refuse_or_refused_in_content and not not_in_content:
                # The customer confirmed the refusal.
                # Verification is satisfied, so we need to decide what to do.
                return {
                    "disposition": "HUMAN_TASK_REQUIRED",
                    "capability": "DECIDE_ACTION",
                    "action_type": None,
                    "reason": "Customer confirmed refusal; need to decide on action.",
                    "evidence_ids": [e.id for e in evidence],
                    "policy_version": coordination.policy_version,
                }
            # Check for refusal denial (contradiction): "refuse" or "refused" present AND "not" present
            elif refuse_or_refused_in_content and not_in_content:
                # The customer denied the refusal (contradiction).
                return {
                    "disposition": "HUMAN_TASK_REQUIRED",
                    "capability": "DECIDE_ACTION",
                    "action_type": None,
                    "reason": "Customer denied refusal (contradiction); need to decide.",
                    "evidence_ids": [e.id for e in evidence],
                    "policy_version": coordination.policy_version,
                }
            else:
                # The confirmation is about something else or doesn't clearly confirm/refuse refusal.
                # We'll need to verify.
                return {
                    "disposition": "HUMAN_TASK_REQUIRED",
                    "capability": "VERIFY_CUSTOMER",
                    "action_type": None,
                    "reason": "Customer confirmation does not verify refusal; need to verify.",
                    "evidence_ids": [e.id for e in evidence],
                    "policy_version": coordination.policy_version,
                }
        else:
            # No customer confirmation after the tracking_event -> we need to verify.
            return {
                "disposition": "HUMAN_TASK_REQUIRED",
                "capability": "VERIFY_CUSTOMER",
                "action_type": None,
                "reason": "No customer confirmation after RFD; need to verify.",
                "evidence_ids": [e.id for e in evidence],
                "policy_version": coordination.policy_version,
            }

    # Rule 3: If we have not returned yet, and there is a latest operator_action that is a decision kind (like reattempt_requested),
    #         and there is no new tracking_event after it, then we monitor.
    #         We'll consider reattempt_requested as a decision kind.
    decision_kinds = {"reattempt_requested", "cancel_decided"}  # note: cancel_decided is also terminal, but we already handled terminal above.
    if latest_operator_action and latest_operator_action.data.get("kind") in decision_kinds:
        # Check if there is any newer tracking_event or customer_confirmation.
        latest_op_ts = get_evidence_ts(latest_operator_action)
        newer_tracking = False
        newer_confirmation = False
        for e in evidence_sorted:
            if get_evidence_ts(e) > latest_op_ts:
                if e.type == "tracking_event":
                    newer_tracking = True
                elif e.type == "customer_confirmation":
                    newer_confirmation = True
        if not newer_tracking and not newer_confirmation:
            # We have made a decision and there is no new evidence, so we monitor.
            return {
                "disposition": "MONITOR",
                "capability": None,
                "action_type": None,
                "reason": "Decision has been made and no new evidence.",
                "evidence_ids": [e.id for e in evidence],
                "policy_version": coordination.policy_version,
            }

    # Rule 4: If we have not returned yet, and there is no unresolved obligation, we monitor.
    #         We'll assume that if we have not found any reason to require a human task, we monitor.
    return {
        "disposition": "MONITOR",
        "capability": None,
        "action_type": None,
        "reason": "No unresolved obligation detected.",
        "evidence_ids": [e.id for e in evidence],
        "policy_version": coordination.policy_version,
    }


def _gather_evidence_for_case(conn: core.sqlite3.Connection, case_id: str) -> List[Evidence]:
    """Gather all evidence relevant to a case from the authoritative tables.

    This function is internal to the policy layer and should not be called directly
    by the Case Engine orchestrator. It is provided here for completeness and
    may be used by the policy layer if needed.

    Returns a list of Evidence objects.
    """
    # We will implement this function in the Case Engine orchestrator, not here.
    # This is just a stub to show the expected structure.
    raise NotImplementedError("This function is a stub and should be implemented in case_engine.py.")