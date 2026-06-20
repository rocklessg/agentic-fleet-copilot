import os
import uuid

import httpx
import pandas as pd
import streamlit as st

from src.utils.env import load_project_env

load_project_env()

API_BASE_URL = os.getenv("FLEET_API_URL", "http://127.0.0.1:8000")

TENANTS = {
    "Acme": "acme-001",
    "Globex": "globex-002",
    "Initech": "initech-003",
}


def _init_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())
    if "paused" not in st.session_state:
        st.session_state.paused = False
    if "proposed_actions" not in st.session_state:
        st.session_state.proposed_actions = []
    if "last_query_results" not in st.session_state:
        st.session_state.last_query_results = []
    if "tenant_label" not in st.session_state:
        st.session_state.tenant_label = "Acme"


def _reset_thread() -> None:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.paused = False
    st.session_state.proposed_actions = []


def _post_chat(message: str, company_id: str) -> dict:
    payload = {
        "message": message,
        "company_id": company_id,
        "thread_id": st.session_state.thread_id,
    }
    with httpx.Client(timeout=120.0) as client:
        response = client.post(f"{API_BASE_URL}/chat", json=payload)
        response.raise_for_status()
        return response.json()


def _post_approve(approved: bool) -> dict:
    payload = {
        "thread_id": st.session_state.thread_id,
        "approved": approved,
    }
    with httpx.Client(timeout=120.0) as client:
        response = client.post(f"{API_BASE_URL}/approve", json=payload)
        response.raise_for_status()
        return response.json()


def _apply_response(data: dict, user_message: str) -> None:
    assistant_message = data.get("final_response") or (
        "Fleet Copilot paused for human approval of proposed hardware actions."
    )
    st.session_state.messages.append({"role": "user", "content": user_message})
    st.session_state.messages.append({"role": "assistant", "content": assistant_message})
    st.session_state.last_query_results = data.get("query_results") or []
    st.session_state.paused = data.get("status") == "paused"
    st.session_state.proposed_actions = data.get("proposed_actions") or []
    if data.get("status") == "completed":
        st.session_state.thread_id = str(uuid.uuid4())


def main() -> None:
    st.set_page_config(page_title="Fleet Copilot", layout="wide")
    _init_session_state()

    st.title("Fleet Copilot")
    st.caption("Agentic IT fleet management with tenant isolation and human-in-the-loop guardrails.")

    with st.sidebar:
        st.header("Administrator")
        selected_tenant = st.selectbox(
            "Active tenant",
            options=list(TENANTS.keys()),
            index=list(TENANTS.keys()).index(st.session_state.tenant_label),
        )
        if selected_tenant != st.session_state.tenant_label:
            st.session_state.tenant_label = selected_tenant
            st.session_state.messages = []
            _reset_thread()

        company_id = TENANTS[selected_tenant]
        st.markdown(f"**Scoped company_id:** `{company_id}`")
        st.markdown(f"**Thread ID:** `{st.session_state.thread_id[:8]}...`")

        if st.button("New conversation"):
            st.session_state.messages = []
            st.session_state.last_query_results = []
            _reset_thread()
            st.rerun()

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if st.session_state.last_query_results:
        st.subheader("Fleet statistics")
        st.dataframe(pd.DataFrame(st.session_state.last_query_results), use_container_width=True)

    if st.session_state.paused:
        st.warning("Graph execution is paused pending administrator approval.")
        if st.session_state.proposed_actions:
            st.json(st.session_state.proposed_actions)

        col_approve, col_reject = st.columns(2)
        with col_approve:
            if st.button("Approve", type="primary", use_container_width=True):
                try:
                    data = _post_approve(True)
                    st.session_state.paused = data.get("status") == "paused"
                    st.session_state.proposed_actions = data.get("proposed_actions") or []
                    st.session_state.last_query_results = data.get("query_results") or []
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "content": data.get("final_response")
                            or "Actions approved and graph resumed.",
                        }
                    )
                    if data.get("status") == "completed":
                        _reset_thread()
                    st.rerun()
                except httpx.HTTPError as exc:
                    st.error(f"Approval failed: {exc}")

        with col_reject:
            if st.button("Reject", use_container_width=True):
                try:
                    data = _post_approve(False)
                    st.session_state.paused = False
                    st.session_state.proposed_actions = []
                    st.session_state.last_query_results = data.get("query_results") or []
                    st.session_state.messages.append(
                        {
                            "role": "assistant",
                            "content": data.get("final_response")
                            or "Proposed actions were rejected.",
                        }
                    )
                    _reset_thread()
                    st.rerun()
                except httpx.HTTPError as exc:
                    st.error(f"Rejection failed: {exc}")

    prompt = st.chat_input("Ask about fleet health, compliance, or remediation...")
    if prompt and not st.session_state.paused:
        with st.spinner("Fleet Copilot is reasoning over telemetry..."):
            try:
                data = _post_chat(prompt, TENANTS[st.session_state.tenant_label])
                _apply_response(data, prompt)
                st.rerun()
            except httpx.HTTPError as exc:
                st.error(f"Chat request failed: {exc}")
    elif prompt and st.session_state.paused:
        st.info("Resolve the pending approval before sending a new query.")


if __name__ == "__main__":
    main()
