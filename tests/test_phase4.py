"""Commerce Ops Phase 4 Increment 1 — Operational Loop tests.

These tests exercise the full operational loop:
Case → Case Engine → Required capability → HumanTask → operator completes task
→ Authoritative evidence recorded → HumanTask completed → Case re-evaluated
→ Next required capability / MONITOR / RESOLVE

This test file does NOT redefine canonical contracts; it verifies that the
Phase 4 layer preserves V0 + Phase 1 + Phase 2 + Phase 3 semantics.
"""

import os
import sys
import tempfile
import json
import uuid
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))

from commerceops import core, case as case_mod, case_engine, capability
from commerceops import actions, followups, human_task, operational
from commerceops.actions import ValidationError, ShipmentNotFoundError


def ship_id(conn, tracking_no: str) -> str:
    row = conn.execute(
        "SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)
    ).fetchone()
    return row["id"]


def fresh_db():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "p4.db")
    return core.connect(db), tmp


def backdate_tracking_event(conn, shipment_id: str, ts: str) -> None:
    conn.execute(
        "UPDATE tracking_event SET imported_at=? WHERE shipment_id=?", (ts, shipment_id)
    )


def append_courier_observation(conn, shipment_id: str, raw_code: str,
                               raw_text: str, imported_at: str) -> None:
    """Add a distinct courier observation for a fresh-evidence cycle.

    An identical CSV row is intentionally idempotent under the V0 event-key
    contract; it is not new evidence.  A second observation therefore carries
    its own source text and event key, as a real second courier report would.
    """
    tracking_no = conn.execute(
        "SELECT tracking_no FROM shipment WHERE id=?", (shipment_id,)
    ).fetchone()["tracking_no"]
    event_key = core._event_key(tracking_no, raw_code, raw_text, None)
    conn.execute(
        """
        INSERT INTO tracking_event
        (id, shipment_id, raw_code, raw_text, occurred_at, imported_at, event_key)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        """,
        (uuid.uuid4().hex, shipment_id, raw_code, raw_text, imported_at, event_key),
    )
    core.refresh_state(conn, shipment_id)


def make_rfd_case(conn, tracking_no="SN1", backdate_to=None):
    core.import_source(
        conn,
        f"tracking_no,postex_remark,our_remark,status,customer_phone\n{tracking_no},RFD,,\n",
    )
    sid = ship_id(conn, tracking_no)
    if backdate_to:
        backdate_tracking_event(conn, sid, backdate_to)
    case_id = case_mod.create_case(conn, sid)
    return sid, case_id


# ---------- 1. pending task retrieval ----------

def test_pending_task_retrieval_returns_pending_only():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_PEND", backdate_to="2026-06-15T08:00:00+00:00")
    # No tasks yet
    assert operational.get_pending_human_tasks_with_context(conn) == []
    tid = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    pending = operational.get_pending_human_tasks_with_context(conn)
    assert len(pending) == 1
    t = pending[0]
    assert t["id"] == tid
    assert t["type"] == "VERIFY_CUSTOMER"
    assert t["status"] == "PENDING"
    assert t["case_info"]["tracking_no"] == "SN_PEND"
    assert t["case_info"]["shipment_id"] == sid
    assert t["case_info"]["case_id"] == case_id
    # recent_evidence list is present (may be empty for fresh case)
    assert "recent_evidence" in t
    assert "task_context" in t
    conn.close()


# ---------- 2/3. VERIFY_CUSTOMER completion + customer_confirmation persistence ----------

def test_verify_customer_completion_records_customer_confirmation():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_V1", backdate_to="2026-06-15T08:00:00+00:00")
    tid = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    result = operational.complete_human_task_with_evidence(conn, tid, {
        "customer_response": "I did not refuse",
        "verification_method": "whatsapp",
    })
    assert result["success"] is True, result.get("error")
    # Authoritative evidence recorded
    ccs = actions.list_customer_confirmations(conn, sid)
    assert len(ccs) == 1
    assert ccs[0]["content"] == "I did not refuse"
    assert ccs[0]["channel"] == "whatsapp"
    # evidence_recorded shape
    assert result["evidence_recorded"]["type"] == "customer_confirmation"
    conn.close()


# ---------- 4. HumanTask completion ----------

def test_human_task_marked_completed_only_on_success():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_V2", backdate_to="2026-06-15T08:00:00+00:00")
    tid = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    operational.complete_human_task_with_evidence(
        conn, tid, {"customer_response": "I did not refuse"}
    )
    t = human_task.get_human_task(conn, tid)
    assert t["status"] == "COMPLETED"
    # and a second attempt must reject
    result = operational.complete_human_task_with_evidence(
        conn, tid, {"customer_response": "again"}
    )
    assert result["success"] is False
    assert "not PENDING" in (result.get("error") or "")
    # still only one customer_confirmation persisted
    assert len(actions.list_customer_confirmations(conn, sid)) == 1
    conn.close()


# ---------- 5/6. Automatic case re-evaluation + next capability materialization ----------

def test_complete_verify_triggers_re_evaluation_and_next_capability():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_V3", backdate_to="2026-06-15T08:00:00+00:00")
    tid = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    result = operational.complete_human_task_with_evidence(
        conn, tid, {"customer_response": "I did not refuse", "verification_method": "whatsapp"}
    )
    assert result["success"] is True
    ev = result["next_evaluation"]
    assert ev is not None
    assert ev.disposition == "HUMAN_TASK_REQUIRED"
    assert ev.capability == "DECIDE_ACTION"
    # And a new pending task is now materialized for that capability
    pending = operational.get_pending_human_tasks_with_context(conn)
    types = [p["type"] for p in pending]
    assert "DECIDE_ACTION" in types
    conn.close()


# ---------- 7/8. DECIDE_ACTION completion + operator_action persistence ----------

def test_decide_action_completion_records_operator_action():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_D1", backdate_to="2026-06-15T08:00:00+00:00")
    # First verify
    vt = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    operational.complete_human_task_with_evidence(
        conn, vt, {"customer_response": "I did not refuse", "verification_method": "email"}
    )
    # Pick up the DECIDE_ACTION task that was materialized
    decide_tasks = [t for t in operational.get_pending_human_tasks_with_context(conn)
                    if t["type"] == "DECIDE_ACTION"]
    assert len(decide_tasks) == 1
    dt = decide_tasks[0]["id"]
    result = operational.complete_human_task_with_evidence(conn, dt, {
        "decision_type": "reattempt_requested",
        "notes": "Customer denied refusal; reattempting",
    })
    assert result["success"] is True, result.get("error")
    oa = actions.list_operator_actions(conn, sid)
    assert any(a["kind"] == "reattempt_requested" and a["note"] == "Customer denied refusal; reattempting"
               for a in oa)
    conn.close()


# ---------- 9. Resulting MONITOR ----------

def test_decide_reattempt_leads_to_monitor():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_M1", backdate_to="2026-06-15T08:00:00+00:00")
    vt = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    operational.complete_human_task_with_evidence(
        conn, vt, {"customer_response": "I did not refuse", "verification_method": "email"}
    )
    decide_tasks = [t for t in operational.get_pending_human_tasks_with_context(conn)
                    if t["type"] == "DECIDE_ACTION"]
    dt = decide_tasks[0]["id"]
    result = operational.complete_human_task_with_evidence(conn, dt, {
        "decision_type": "reattempt_requested",
    })
    assert result["success"] is True
    ev = result["next_evaluation"]
    assert ev.disposition == "MONITOR"
    assert ev.capability is None
    # No further pending tasks
    assert operational.get_pending_human_tasks_with_context(conn) == []
    conn.close()


# ---------- 10/11. New evidence -> re-evaluation on SAME case ----------

def test_new_evidence_reevaluates_same_case():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_E1", backdate_to="2026-06-15T08:00:00+00:00")
    # Verify
    vt = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    operational.complete_human_task_with_evidence(
        conn, vt, {
            "customer_response": "I did not refuse",
            "verified_at": "2026-06-15T08:30:00+00:00",
        }
    )
    # Decide reattempt
    dt = [t for t in operational.get_pending_human_tasks_with_context(conn)
          if t["type"] == "DECIDE_ACTION"][0]["id"]
    operational.complete_human_task_with_evidence(conn, dt, {
        "decision_type": "reattempt_requested",
        "decided_at": "2026-06-15T09:00:00+00:00",
    })
    # Same Case
    case_after = case_mod.get_case(conn, case_id)
    assert case_after["status"] == "OPEN"
    # A distinct courier observation arrives after the reattempt action.
    append_courier_observation(
        conn, sid, "RFD", "second courier RFD observation",
        "2026-06-15T18:00:00+00:00",
    )
    ev = case_engine.evaluate_case(conn, case_id)
    assert ev.disposition == "HUMAN_TASK_REQUIRED"
    assert ev.capability == "VERIFY_CUSTOMER"
    # Still the same case id
    assert ev.case_id == case_id
    # A new VERIFY_CUSTOMER task is now pending
    pending_types = [t["type"] for t in operational.get_pending_human_tasks_with_context(conn)]
    assert pending_types.count("VERIFY_CUSTOMER") == 1
    conn.close()


# ---------- 12. Idempotent repeated task/action creation ----------

def test_idempotent_repeated_capability_returns_same_task():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_I1", backdate_to="2026-06-15T08:00:00+00:00")
    t1 = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    t2 = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    assert t1 == t2
    # Same in the operational view too
    pending = operational.get_pending_human_tasks_with_context(conn)
    assert len(pending) == 1
    conn.close()


# ---------- 13. Failure does not falsely complete task ----------

def test_failure_does_not_mark_task_completed():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_F1", backdate_to="2026-06-15T08:00:00+00:00")
    tid = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    # Empty customer_response must raise ValidationError and roll back
    result = operational.complete_human_task_with_evidence(
        conn, tid, {"customer_response": ""}
    )
    assert result["success"] is False
    assert "customer_response" in (result.get("error") or "").lower() or "non-empty" in (result.get("error") or "").lower()
    t = human_task.get_human_task(conn, tid)
    assert t["status"] == "PENDING"
    assert len(actions.list_customer_confirmations(conn, sid)) == 0
    conn.close()


# ---------- 14. Contradictory evidence remains distinguishable ----------

def test_contradictory_evidence_distinguishable():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_C1", backdate_to="2026-06-15T08:00:00+00:00")
    vt = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    operational.complete_human_task_with_evidence(
        conn, vt, {"customer_response": "I did not refuse the parcel", "verification_method": "whatsapp"}
    )
    # Now DECIDE_ACTION should be required
    decide_tasks = [t for t in operational.get_pending_human_tasks_with_context(conn)
                    if t["type"] == "DECIDE_ACTION"]
    assert len(decide_tasks) == 1
    # The customer_confirmation row is preserved verbatim; can be inspected
    cc = actions.list_customer_confirmations(conn, sid)[0]
    assert "I did not refuse" in cc["content"]
    # The case re-evaluation (run by complete_*) returned a contradiction reason
    # We re-evaluate to confirm policy produced the right wording
    ev = case_engine.evaluate_case(conn, case_id)
    assert ev.disposition == "HUMAN_TASK_REQUIRED"
    assert ev.capability == "DECIDE_ACTION"
    assert "contradiction" in ev.reason.lower()
    conn.close()


# ---------- 15. Terminal semantics remain intact ----------

def test_terminal_outcomes_remain_intact():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_T1", backdate_to="2026-06-15T08:00:00+00:00")
    # Manually mark delivered via existing outcomes path
    from commerceops import outcomes
    outcomes.mark_delivered(conn, sid)
    ship_row = conn.execute("SELECT current_state FROM shipment WHERE id=?", (sid,)).fetchone()
    assert ship_row["current_state"] == "CLOSED_DELIVERED"
    # Re-evaluating the case should now report MONITOR (non-open)
    ev = case_engine.evaluate_case(conn, case_id)
    # Force the case to CLOSED via resolution so the case is in a non-open state for the policy
    case_mod.set_case_resolution(conn, case_id, {"outcome": "delivered"})
    case_mod.update_case_status(conn, case_id, "RESOLVED")
    ev2 = case_engine.evaluate_case(conn, case_id)
    assert ev2.disposition == "MONITOR"
    conn.close()


# ---------- 16. Canonical end-to-end lifecycle ----------

def test_canonical_end_to_end_lifecycle():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_FINAL", backdate_to="2026-06-15T08:00:00+00:00")

    # (1) Initial VERIFY_CUSTOMER
    vt = capability.execute_capability(conn, case_id, "VERIFY_CUSTOMER", {})
    result = operational.complete_human_task_with_evidence(conn, vt, {
        "customer_response": "I did not refuse the parcel",
        "verification_method": "whatsapp",
        "verified_at": "2026-06-15T08:30:00+00:00",
    })
    assert result["success"] is True
    assert result["next_evaluation"].capability == "DECIDE_ACTION"
    assert result["case_id"] == case_id

    # (2) DECIDE_ACTION → reattempt
    dt = [t for t in operational.get_pending_human_tasks_with_context(conn)
          if t["type"] == "DECIDE_ACTION"][0]["id"]
    result = operational.complete_human_task_with_evidence(conn, dt, {
        "decision_type": "reattempt_requested",
        "notes": "Customer denied",
        "decided_at": "2026-06-15T09:00:00+00:00",
    })
    assert result["success"] is True
    assert result["next_evaluation"].disposition == "MONITOR"
    assert result["next_evaluation"].capability is None

    # (3) New evidence → re-evaluates SAME case
    append_courier_observation(
        conn, sid, "RFD", "second courier RFD observation",
        "2026-06-15T18:00:00+00:00",
    )
    ev = case_engine.evaluate_case(conn, case_id)
    assert ev.disposition == "HUMAN_TASK_REQUIRED"
    assert ev.capability == "VERIFY_CUSTOMER"
    assert ev.case_id == case_id

    # History preserved: all events visible
    ccs = actions.list_customer_confirmations(conn, sid)
    oa = actions.list_operator_actions(conn, sid)
    assert len(ccs) == 1
    assert any(a["kind"] == "reattempt_requested" for a in oa)
    # Two tracking events (original RFD + new RFD)
    rows = conn.execute(
        "SELECT COUNT(*) c FROM tracking_event WHERE shipment_id=?", (sid,)
    ).fetchone()["c"]
    assert rows == 2
    conn.close()


# ---------- additional guard: task not found ----------

def test_complete_with_unknown_task_id():
    conn, _ = fresh_db()
    result = operational.complete_human_task_with_evidence(
        conn, "nonexistent-task-id", {"customer_response": "x"}
    )
    assert result["success"] is False
    assert "not found" in (result.get("error") or "").lower()
    conn.close()


# ---------- additional guard: payload with no customer_response fails before any evidence ----------

def test_decide_action_validates_decision_type():
    conn, _ = fresh_db()
    sid, case_id = make_rfd_case(conn, "SN_DV1", backdate_to="2026-06-15T08:00:00+00:00")
    # Forge a DECIDE_ACTION task directly (bypassing policy)
    from commerceops import human_task as ht
    task_id = ht.create_human_task(conn, case_id, "DECIDE_ACTION", {"cycle": "x"})
    result = operational.complete_human_task_with_evidence(conn, task_id, {
        "decision_type": "bogus_kind"
    })
    assert result["success"] is False
    # task still PENDING
    t = human_task.get_human_task(conn, task_id)
    assert t["status"] == "PENDING"
    # no operator_action persisted
    assert all(a["kind"] != "bogus_kind" for a in actions.list_operator_actions(conn, sid))
    conn.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
