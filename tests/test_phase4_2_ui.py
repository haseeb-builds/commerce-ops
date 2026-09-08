"""HTTP coverage for real operator input and explicit evidence re-evaluation."""
import os
import sys
import json

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages"))
from commerceops import actions, core, human_task
from commerceops_ui import app as ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = str(tmp_path / "ui.db")
    monkeypatch.setattr(ui, "DB_PATH", path)
    with TestClient(ui.app) as client:
        client.post("/import", data={"pasted": "tracking_no,postex_remark\nUI,RFD"})
        yield client


def tasks():
    conn = ui._conn()
    try:
        return [human_task.get_human_task(conn, t["id"]) for t in human_task.get_pending_tasks(conn)]
    finally:
        conn.close()


def test_explicit_evaluation_creates_one_case_and_queue_reads_do_not_mutate(client):
    assert "Evaluate current evidence" in client.get("/shipment/UI").text
    assert tasks() == []
    response = client.post("/shipment/UI/evaluate")
    assert response.status_code == 200
    assert "VERIFY_CUSTOMER" in response.text
    tid = tasks()[0]["id"]
    client.post("/shipment/UI/evaluate")
    assert tasks()[0]["id"] == tid
    conn = ui._conn()
    try:
        before = list(conn.iterdump())
        for url in ("/", "/shipment/UI", "/tasks/pending", f"/task/{tid}"):
            assert client.get(url).status_code == 200
        assert list(conn.iterdump()) == before
        assert conn.execute("SELECT COUNT(*) FROM case_entity").fetchone()[0] == 1
    finally:
        conn.close()


def test_typed_forms_complete_verification_and_decision_without_fake_defaults(client):
    client.post("/shipment/UI/evaluate")
    tid = tasks()[0]["id"]
    page = client.get(f"/task/{tid}").text
    assert 'name="customer_response"' in page
    assert "I did not refuse" not in page
    assert "Complete (verify)" not in client.get("/tasks/pending").text
    text = "  I did not refuse the parcel. <courier claim disputed>\n"
    response = client.post(f"/tasks/{tid}/complete", data={
        "customer_response": text, "verification_method": "call"})
    assert "DECIDE_ACTION" in response.text
    decision = tasks()[0]
    assert decision["type"] == "DECIDE_ACTION"
    response = client.post(f"/tasks/{decision['id']}/complete", data={
        "decision_type": "reattempt_requested", "notes": "Customer contradicted courier"})
    assert "MONITOR" in response.text and tasks() == []
    conn = ui._conn()
    try:
        row = conn.execute("SELECT content, channel FROM customer_confirmation").fetchone()
        assert tuple(row) == (text, "call")
        assert conn.execute("SELECT kind FROM operator_action").fetchone()[0] == "reattempt_requested"
        assert conn.execute("SELECT COUNT(*) FROM autonomous_action").fetchone()[0] == 0
    finally:
        conn.close()


def test_stale_submission_error_is_visible_and_retired_task_links_replacement(client):
    client.post("/shipment/UI/evaluate")
    tid = tasks()[0]["id"]
    client.post("/shipment/UI/customer-confirmation", data={"content": "I did not refuse", "channel": "call"})
    response = client.post(f"/tasks/{tid}/complete", data={"customer_response": "I refused"})
    assert "Task is stale" in response.text
    client.post("/shipment/UI/evaluate")
    replacement = tasks()[0]
    page = client.get(f"/task/{tid}").text
    assert "CANCELLED" in page
    assert f'/task/{replacement["id"]}' in page
    assert 'name="customer_response"' not in page
    conn = ui._conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM customer_confirmation").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("payload", [{}, {"confirm_cancel": "yes"}, {"cancel_reason": "Customer does not want it"}])
def test_cancellation_form_requires_reason_and_explicit_confirmation(client, payload):
    client.post("/shipment/UI/evaluate")
    client.post(f"/tasks/{tasks()[0]['id']}/complete", data={"customer_response": "I refused"})
    tid = tasks()[0]["id"]
    response = client.post(f"/tasks/{tid}/complete", data={"decision_type": "cancel_decided", **payload})
    assert "Error:" in response.text
    assert tasks()[0]["id"] == tid


def test_explicit_cancel_with_reason_records_human_decision(client):
    client.post("/shipment/UI/evaluate")
    client.post(f"/tasks/{tasks()[0]['id']}/complete", data={"customer_response": "I refused"})
    response = client.post(f"/tasks/{tasks()[0]['id']}/complete", data={
        "decision_type": "cancel_decided", "cancel_reason": "NH CHAIYE", "confirm_cancel": "yes"})
    assert "MONITOR" in response.text
    assert tasks() == []
    conn = ui._conn()
    try:
        row = conn.execute("SELECT kind, cancel_reason, actor FROM operator_action").fetchone()
        assert tuple(row) == ("cancel_decided", "NH CHAIYE", "laiba")
    finally:
        conn.close()


def test_legacy_json_completion_keeps_channel_and_handles_bad_input(client):
    client.post("/shipment/UI/evaluate")
    tid = tasks()[0]["id"]
    for value in ('[]', 'null', '{bad json', '"not an object"'):
        response = client.post(f"/tasks/{tid}/complete", data={"completion_result": value})
        assert response.status_code == 200 and "Error:" in response.text
        assert tasks()[0]["id"] == tid
    response = client.post(f"/tasks/{tid}/complete", data={
        "completion_result": json.dumps({"customer_response": "I did not refuse"}),
        "completion_method": "whatsapp"})
    assert "DECIDE_ACTION" in response.text
    conn = ui._conn()
    try:
        assert conn.execute("SELECT channel FROM customer_confirmation").fetchone()[0] == "whatsapp"
    finally:
        conn.close()


def test_unknown_shipment_evaluation_is_safe_and_errors_are_escaped(client):
    response = client.post("/shipment/UNKNOWN/evaluate")
    assert "unknown shipment" in response.text
    response = client.get("/tasks/pending", params={"error": "<script>alert('bad')</script>&details"})
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert tasks() == []
