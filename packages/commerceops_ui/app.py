"""Commerce Ops v0 — minimal local UI harness.

Thin HTTP layer over the frozen domain APIs. NO business logic lives here:
every mutation calls an existing domain function; every view reads existing
read models. Server-rendered Jinja2 (autoescape on) so all values are
HTML-escaped by default.

Launch:  python -m commerceops_ui.run   (from repository root)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from commerceops import core, actions, followups, outcomes

DB_PATH = os.environ.get(
    "COMMERCEOPS_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "commerceops.db"),
)

app = FastAPI(title="Commerce Ops v0", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))


def _conn():
    return core.connect(DB_PATH)


def _queue(conn):
    """Authoritative queue via the single domain implementation."""
    return followups.work_queue(conn)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    conn = _conn()
    try:
        queue = _queue(conn)
        total = conn.execute("SELECT COUNT(*) c FROM shipment").fetchone()["c"]
        return templates.TemplateResponse(request, "index.html", {
            "queue": queue, "total_shipments": total,
        })
    finally:
        conn.close()


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    return templates.TemplateResponse(request, "import.html", {"result": None, "pasted": ""})


@app.post("/import")
def import_post(request: Request, pasted: str = Form("")):
    conn = _conn()
    try:
        result = core.import_source(conn, pasted, source_label="ui-paste")
        quarantined = conn.execute(
            "SELECT line_no, line_text, reason FROM quarantine_row WHERE batch_id=? ORDER BY line_no",
            (result["batch_id"],),
        ).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "import.html", {
        "result": result,
        "pasted": pasted,
        "quarantined_rows": [dict(q) for q in quarantined],
    })


@app.get("/shipment/{tracking_no}", response_class=HTMLResponse)
def shipment_detail_view(request: Request, tracking_no: str, error: str = ""):
    conn = _conn()
    try:
        d = detail_safe(conn, tracking_no)
        if d is None:
            return templates.TemplateResponse(request, "not_found.html",
                                              {"tracking_no": tracking_no}, status_code=404)
        ship_row = conn.execute("SELECT id FROM shipment WHERE tracking_no=?",
                                (tracking_no,)).fetchone()
        fus = followups.list_follow_ups(conn, shipment_id=ship_row["id"])
        drafts = {intent: outcomes.generate_draft(intent, tracking_no)
                  for intent in outcomes.DRAFT_INTENTS}
    finally:
        conn.close()
    return templates.TemplateResponse(request, "shipment.html", {
        "d": d, "fus": fus, "drafts": drafts,
        "action_kinds": ["note", "reattempt_requested", "open_allowed"],
        "error": error,
    })


def detail_safe(conn, tracking_no):
    from commerceops import detail as detail_mod
    return detail_mod.shipment_detail(conn, tracking_no)


# ------------------------- operator actions -------------------------

def _record_and_redirect(tracking_no: str, fn, *args, **kwargs):
    conn = _conn()
    error = None
    try:
        fn(conn, *args, **kwargs)
    except (actions.ValidationError, actions.ShipmentNotFoundError) as e:
        error = str(e)
    finally:
        conn.close()
    if error:
        # re-render detail with the validation message; keep it simple
        return RedirectResponse(f"/shipment/{tracking_no}?error={error}", status_code=303)
    return RedirectResponse(f"/shipment/{tracking_no}", status_code=303)


@app.post("/shipment/{tracking_no}/note")
def add_note(tracking_no: str, note: str = Form("")):
    conn = _conn()
    try:
        sid = conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)).fetchone()
        if not sid:
            conn.close()
            return RedirectResponse("/shipment/NOT_FOUND_DOES_NOT_EXIST?error=unknown+shipment", status_code=303)
        kwargs = {}
        err = None
        if not note.strip():
            err = "operator note requires non-empty note text"
        else:
            try:
                actions.record_operator_action(conn, sid["id"], kind="note", note=note)
            except actions.ValidationError as e:
                err = str(e)
    finally:
        pass
    conn.close()
    dest = f"/shipment/{tracking_no}" + (f"?error={err}" if err else "")
    return RedirectResponse(dest, status_code=303)


@app.post("/shipment/{tracking_no}/action/{kind}")
def do_action(tracking_no: str, kind: str):
    conn = _conn()
    err = None
    try:
        row = conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)).fetchone()
        if row:
            actions.record_operator_action(conn, row["id"], kind=kind)
        else:
            err = "unknown shipment"
    except actions.ValidationError as e:
        err = str(e)
    finally:
        conn.close()
    dest = f"/shipment/{tracking_no}" + (f"?error={err}" if err else "")
    return RedirectResponse(dest, status_code=303)


@app.post("/shipment/{tracking_no}/customer-confirmation")
def add_confirmation(tracking_no: str, content: str = Form(""), channel: str = Form("")):
    conn = _conn()
    err = None
    try:
        row = conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)).fetchone()
        if row:
            actions.record_customer_confirmation(
                conn, row["id"], content=content, channel=channel or None)
        else:
            err = "unknown shipment"
    except actions.ValidationError as e:
        err = str(e)
    finally:
        conn.close()
    dest = f"/shipment/{tracking_no}" + (f"?error={err}" if err else "")
    return RedirectResponse(dest, status_code=303)


@app.post("/shipment/{tracking_no}/follow-up")
def create_followup(tracking_no: str, due_at: str = Form(""), reason: str = Form("")):
    conn = _conn()
    err = None
    try:
        row = conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)).fetchone()
        if row:
            followups.create_follow_up(conn, row["id"], due_at=due_at, reason=reason)
        else:
            err = "unknown shipment"
    except (actions.ValidationError, actions.ShipmentNotFoundError) as e:
        err = str(e)
    finally:
        conn.close()
    dest = f"/shipment/{tracking_no}" + (f"?error={err}" if err else "")
    return RedirectResponse(dest, status_code=303)


@app.post("/follow-up/{fid}/complete")
def complete_followup(fid: str, tracking_no: str = Form("")):
    conn = _conn()
    err = None
    try:
        followups.complete_follow_up(conn, fid)
    except (actions.ValidationError, followups.FollowUpNotFoundError) as e:
        err = str(e)
    finally:
        conn.close()
    dest = f"/shipment/{tracking_no}" + (f"?error={err}" if err else "")
    return RedirectResponse(dest, status_code=303)


# ------------------------- terminal outcomes -------------------------

@app.post("/shipment/{tracking_no}/terminal/{kind}")
def terminal(tracking_no: str, kind: str, reason: str = Form("")):
    conn = _conn()
    err = None
    try:
        row = conn.execute("SELECT id FROM shipment WHERE tracking_no=?", (tracking_no,)).fetchone()
        if not row:
            raise actions.ShipmentNotFoundError("unknown shipment")
        if kind == "delivered":
            outcomes.mark_delivered(conn, row["id"])
        elif kind == "cancelled":
            outcomes.mark_cancelled(conn, row["id"], cancel_reason=reason)
        elif kind == "returned":
            outcomes.mark_returned(conn, row["id"])
        else:
            err = "unknown terminal outcome"
    except (actions.ValidationError, actions.ShipmentNotFoundError) as e:
        err = str(e)
    finally:
        conn.close()
    dest = f"/shipment/{tracking_no}" + (f"?error={err}" if err else "")
    return RedirectResponse(dest, status_code=303)


# ------------------------- human task operations -------------------------

@app.get("/tasks/pending", response_class=HTMLResponse)
def pending_tasks(request: Request):
    """Get all pending human tasks with context for operational display."""
    from commerceops import operational
    conn = _conn()
    try:
        pending_tasks = operational.get_pending_human_tasks_with_context(conn)
        return templates.TemplateResponse(request, "pending_tasks.html", {
            "pending_tasks": pending_tasks
        })
    finally:
        conn.close()


@app.post("/tasks/{task_id}/complete")
def complete_task(task_id: str, 
                  completion_result: str = Form(""),
                  completion_method: str = Form("")):
    """Complete a human task with the provided result.
    
    Expected form data:
    - completion_result: JSON string containing the completion details
    - completion_method: For VERIFY_CUSTOMER: verification method (phone, email, etc.)
                        For DECIDE_ACTION: decision type 
                        For FOLLOW_UP_ACTION: completion notes
    """
    from commerceops import operational
    import json
    
    conn = _conn()
    err = None
    next_evaluation = None
    evidence_recorded = None
    
    try:
        # Parse completion result
        try:
            result_data = json.loads(completion_result) if completion_result.strip() else {}
        except json.JSONDecodeError:
            result_data = {"raw_input": completion_result}
        
        # Add method-specific data
        if completion_method:
            if completion_method not in result_data:
                result_data["method"] = completion_method
        
        # Complete the task
        completion_result_dict = operational.complete_human_task_with_evidence(
            conn, task_id, result_data
        )
        
        if not completion_result_dict["success"]:
            err = completion_result_dict["error"]
        else:
            next_evaluation = completion_result_dict["next_evaluation"]
            evidence_recorded = completion_result_dict["evidence_recorded"]
            
    except Exception as e:
        err = str(e)
    finally:
        conn.close()
    
    # Redirect back to task list with results
    dest = f"/tasks/pending"
    if err:
        dest += f"?error={err}"
    elif next_evaluation is not None:
        # Include evaluation results in redirect for display
        dest += f"?next_disposition={next_evaluation.disposition}"
        if next_evaluation.capability:
            dest += f"&next_capability={next_evaluation.capability}"
        if next_evaluation.reason:
            dest += f"&next_reason={next_evaluation.reason}"
    
    return RedirectResponse(dest, status_code=303)


@app.get("/task/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: str):
    """Get detailed information about a specific human task."""
    from commerceops import human_task, case
    conn = _conn()
    try:
        task = human_task.get_human_task(conn, task_id)
        if task is None:
            return templates.TemplateResponse(request, "not_found.html",
                                            {"task_id": task_id}, status_code=404)
        
        # Get case information
        case_obj = case.get_case(conn, task["case_id"])
        shipment_info = None
        if case_obj:
            shipment_row = conn.execute(
                "SELECT tracking_no FROM shipment WHERE id=?", 
                (case_obj["shipment_id"],)
            ).fetchone()
            shipment_info = {
                "tracking_no": shipment_row["tracking_no"] if shipment_row else None
            } if shipment_row else None
        
        return templates.TemplateResponse(request, "task_detail.html", {
            "task": task,
            "case": case_obj,
            "shipment": shipment_info
        })
    finally:
        conn.close()


# ------------------------- terminal outcomes -------------------------
