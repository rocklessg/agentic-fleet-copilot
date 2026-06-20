from typing import Any

import pytest
from fastapi.testclient import TestClient

from evals.helpers import COMPANY_A_ID, build_patched_graph
from src.api import main as api_main


@pytest.fixture
def api_client(patched_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    test_graph = build_patched_graph()
    monkeypatch.setattr(api_main, "graph", test_graph)
    return TestClient(api_main.app)


@pytest.fixture
def action_api_client(patched_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    test_graph = build_patched_graph(action_plan=True)
    monkeypatch.setattr(api_main, "graph", test_graph)
    return TestClient(api_main.app)


def test_api_health(api_client: TestClient) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_chat_sql_flow(api_client: TestClient) -> None:
    response = api_client.post(
        "/chat",
        json={
            "message": "Show fleet telemetry",
            "company_id": COMPANY_A_ID,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert payload["thread_id"]
    assert payload["final_response"]
    assert isinstance(payload["query_results"], list)


def test_api_chat_action_pause_and_approve(action_api_client: TestClient) -> None:
    chat = action_api_client.post(
        "/chat",
        json={
            "message": "Propose replacement for battery failures",
            "company_id": COMPANY_A_ID,
        },
    )
    assert chat.status_code == 200
    paused = chat.json()
    assert paused["status"] == "paused"
    assert paused["interrupt_payload"]["kind"] == "human_approval_required"
    thread_id = paused["thread_id"]

    status = action_api_client.get(f"/status/{thread_id}")
    assert status.status_code == 200
    assert status.json()["paused"] is True

    blocked = action_api_client.post(
        "/chat",
        json={
            "message": "Another query while paused",
            "company_id": COMPANY_A_ID,
            "thread_id": thread_id,
        },
    )
    assert blocked.status_code == 409

    approved = action_api_client.post(
        "/approve",
        json={"thread_id": thread_id, "approved": True},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    assert approved.json()["approval_decision"] == "approve"


def test_api_approve_409_when_not_paused(api_client: TestClient) -> None:
    response = api_client.post(
        "/approve",
        json={"thread_id": "nonexistent-thread", "approved": True},
    )
    assert response.status_code == 409


def test_api_status_unknown_thread(api_client: TestClient) -> None:
    response = api_client.get("/status/brand-new-thread-id")
    assert response.status_code == 200
    body = response.json()
    assert body["paused"] is False
    assert body["proposed_actions"] == []
