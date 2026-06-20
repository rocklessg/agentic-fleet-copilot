import os
import uuid
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from src.utils.env import load_project_env

load_project_env()

from src.agent.graph import graph

app = FastAPI(title="Fleet Copilot API", version="1.0.0")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    thread_id: str | None = None


class ChatResponse(BaseModel):
    thread_id: str
    status: Literal["completed", "paused"]
    final_response: str
    query_results: list[dict[str, Any]]
    proposed_actions: list[dict[str, Any]]
    generated_sql: str
    interrupt_payload: dict[str, Any] | None = None


class ApproveRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    approved: bool


class ApproveResponse(BaseModel):
    thread_id: str
    status: Literal["completed", "paused"]
    final_response: str
    query_results: list[dict[str, Any]]
    proposed_actions: list[dict[str, Any]]
    approval_decision: str


def _graph_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _initial_state(message: str, company_id: str) -> dict[str, Any]:
    return {
        "input_query": message,
        "company_id": company_id,
        "current_plan": [],
        "generated_sql": "",
        "query_results": [],
        "detected_insights": [],
        "proposed_actions": [],
        "approval_decision": "",
        "final_response": "",
    }


def _sanitize_rows(rows: list[Any]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sanitized.append({key: value for key, value in row.items() if key != "_tracing"})
    return sanitized


def _snapshot_values(config: dict[str, Any]) -> dict[str, Any]:
    snapshot = graph.get_state(config)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    return values


def _is_paused(config: dict[str, Any]) -> bool:
    snapshot = graph.get_state(config)
    return bool(snapshot.interrupts)


def _interrupt_payload(config: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = graph.get_state(config)
    if not snapshot.interrupts:
        return None
    interrupt_obj = snapshot.interrupts[0]
    payload = interrupt_obj.value
    return payload if isinstance(payload, dict) else {"value": payload}


def _build_chat_response(config: dict[str, Any], thread_id: str) -> ChatResponse:
    values = _snapshot_values(config)
    paused = _is_paused(config)
    return ChatResponse(
        thread_id=thread_id,
        status="paused" if paused else "completed",
        final_response=values.get("final_response") or "",
        query_results=_sanitize_rows(values.get("query_results") or []),
        proposed_actions=values.get("proposed_actions") or [],
        generated_sql=values.get("generated_sql") or "",
        interrupt_payload=_interrupt_payload(config) if paused else None,
    )


def _build_approve_response(config: dict[str, Any], thread_id: str) -> ApproveResponse:
    values = _snapshot_values(config)
    paused = _is_paused(config)
    return ApproveResponse(
        thread_id=thread_id,
        status="paused" if paused else "completed",
        final_response=values.get("final_response") or "",
        query_results=_sanitize_rows(values.get("query_results") or []),
        proposed_actions=values.get("proposed_actions") or [],
        approval_decision=values.get("approval_decision") or "",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    thread_id = request.thread_id or str(uuid.uuid4())
    config = _graph_config(thread_id)

    if _is_paused(config):
        raise HTTPException(
            status_code=409,
            detail="Thread is paused awaiting approval. Use /approve to continue.",
        )

    try:
        graph.invoke(_initial_state(request.message, request.company_id), config)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _build_chat_response(config, thread_id)


@app.post("/approve", response_model=ApproveResponse)
def approve(request: ApproveRequest) -> ApproveResponse:
    config = _graph_config(request.thread_id)

    if not _is_paused(config):
        raise HTTPException(
            status_code=409,
            detail="Thread is not paused. No approval is required.",
        )

    resume_value = "approve" if request.approved else "reject"
    try:
        graph.invoke(Command(resume=resume_value), config)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return _build_approve_response(config, request.thread_id)


@app.get("/status/{thread_id}")
def status(thread_id: str) -> dict[str, Any]:
    config = _graph_config(thread_id)
    snapshot = graph.get_state(config)
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    return {
        "thread_id": thread_id,
        "paused": bool(snapshot.interrupts),
        "final_response": values.get("final_response") or "",
        "query_results": _sanitize_rows(values.get("query_results") or []),
        "proposed_actions": values.get("proposed_actions") or [],
        "interrupt_payload": _interrupt_payload(config),
    }
