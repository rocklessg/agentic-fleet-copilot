import json
import os
import re
import sqlite3
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

from src.agent.insights import detect_fleet_insights, format_insights_markdown
from src.agent.tools import (
    SecurityError,
    create_upgrade_order,
    execute_fleet_query,
    flag_device_for_replacement,
    notify_employee,
    open_remediation_ticket,
)
from src.database.ingest import DB_PATH
from src.utils.logger import log_audit_event

FLEET_SCHEMA = """
SQLite schema (tenant column is always devices.company_id):

devices(
  device_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  serial_number TEXT,
  model_name TEXT,
  model_identifier TEXT,
  processor TEXT,
  hardware_uuid TEXT,
  total_memory TEXT
)

telemetry_snapshots(
  snapshot_id INTEGER PRIMARY KEY,
  device_id TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  agent_version TEXT,
  os_platform TEXT,
  os_product_name TEXT,
  os_product_version TEXT,
  os_build_version TEXT,
  os_architecture TEXT,
  os_kernel_name TEXT,
  os_kernel_release TEXT,
  os_hostname TEXT,
  ram_bytes INTEGER,
  total_memory_bytes INTEGER,
  used_memory_bytes INTEGER,
  free_memory_bytes INTEGER,
  page_size_bytes INTEGER,
  disk_volume_name TEXT,
  disk_file_system TEXT,
  disk_mount_point TEXT,
  disk_size_bytes INTEGER,
  disk_available_bytes INTEGER,
  disk_encrypted INTEGER,
  battery_present INTEGER,
  battery_charging_status TEXT,
  battery_percentage INTEGER,
  battery_condition TEXT,
  battery_cycle_count INTEGER,
  battery_full_charge_capacity INTEGER
)

compliance_checks(
  compliance_id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL,
  device_id TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  check_id TEXT NOT NULL,
  status TEXT NOT NULL,
  severity TEXT NOT NULL
)
"""


class AgentState(TypedDict):
    input_query: str
    company_id: str
    current_plan: list
    generated_sql: str
    query_results: list
    detected_insights: list
    proposed_actions: list
    approval_decision: str
    final_response: str


def _thread_id(config: RunnableConfig) -> str:
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id", "default-thread"))


def _get_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0,
    )


def _llm_available() -> bool:
    api_key = os.getenv("OPENAI_API_KEY", "")
    return bool(api_key) and not api_key.startswith("your-")


def _heuristic_plan(state: AgentState) -> dict[str, Any]:
    query = state["input_query"].lower()
    action_tokens = (
        "remediation",
        "upgrade",
        "replace",
        "notify",
        "propose",
        "ticket",
        "action",
    )
    route = "action" if any(token in query for token in action_tokens) else "sql"
    return {
        "current_plan": [
            {"route": route},
            {"step": "Analyze request with deterministic planner fallback."},
        ]
    }


def _heuristic_sql(state: AgentState) -> dict[str, Any]:
    company_id = state["company_id"]
    return {
        "generated_sql": (
            "SELECT d.device_id, ts.snapshot_id, ts.collected_at, "
            "ts.os_product_name, ts.os_product_version, ts.battery_percentage, "
            "ts.disk_size_bytes, ts.disk_available_bytes "
            "FROM telemetry_snapshots ts "
            "INNER JOIN devices d ON d.device_id = ts.device_id "
            f"WHERE d.company_id = '{company_id}' "
            "ORDER BY ts.collected_at DESC LIMIT 10"
        )
    }


def _grounded_fallback_response(state: AgentState) -> str:
    lines: list[str] = []
    insights = state.get("detected_insights") or []
    if insights:
        lines.append(format_insights_markdown(insights))

    rows = state.get("query_results") or []
    if rows:
        lines.append("### Fleet findings")
        for row in rows[:10]:
            if "snapshot_id" in row:
                citation = f"telemetry_snapshots/{row['snapshot_id']}"
            elif "device_id" in row:
                citation = f"devices/{row['device_id']}"
            elif "compliance_id" in row:
                citation = f"compliance_checks/{row['compliance_id']}"
            else:
                citation = "telemetry_snapshots/unknown"
            device_id = row.get("device_id", "unknown")
            lines.append(
                f"- Device `{device_id}` telemetry reviewed "
                f"[Source: {citation}]."
            )

    actions = state.get("proposed_actions") or []
    if actions:
        lines.append("### Proposed actions")
        for item in actions[:10]:
            action = item.get("action", {})
            target = action.get("device_id") or action.get("employee_id") or "target"
            lines.append(
                f"- {action.get('action_type', 'action')} for `{target}` "
                f"(status: {item.get('status', 'pending_approval')})."
            )

    decision = state.get("approval_decision")
    if decision:
        lines.append(f"### Approval decision\n- Administrator decision: **{decision}**")

    return "\n".join(lines) or "No grounded fleet results were available for this request."


def _parse_json_payload(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _strip_sql(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def planner_node(state: AgentState) -> dict[str, Any]:
    if not _llm_available():
        return _heuristic_plan(state)

    llm = _get_llm()
    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are the Fleet Copilot planner. Classify the user request and "
                    "produce a concise execution plan.\n"
                    "Return ONLY JSON with keys:\n"
                    '- route: "sql" for analytics/lookup/trends/compliance questions, '
                    '"action" for operational remediation (upgrades, tickets, replacement, notify).\n'
                    "- steps: array of short plan step strings."
                )
            ),
            HumanMessage(
                content=(
                    f"Company ID: {state['company_id']}\n"
                    f"User query: {state['input_query']}"
                )
            ),
        ]
    )
    payload = _parse_json_payload(str(response.content))
    route = payload.get("route", "sql")
    steps = payload.get("steps") or []
    current_plan = [{"route": route}] + [{"step": step} for step in steps]
    return {"current_plan": current_plan}


def route_after_planner(state: AgentState) -> Literal["sql_generation", "action_proposal"]:
    plan = state.get("current_plan") or []
    if plan and plan[0].get("route") == "action":
        return "action_proposal"
    return "sql_generation"


def sql_generation_node(state: AgentState) -> dict[str, Any]:
    if not _llm_available():
        return _heuristic_sql(state)

    llm = _get_llm()
    company_id = state["company_id"]
    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You generate read-only SQLite SELECT queries for fleet telemetry.\n"
                    f"{FLEET_SCHEMA}\n"
                    "Rules:\n"
                    "- SELECT or WITH queries only.\n"
                    "- Always JOIN devices AS d when querying snapshots or compliance.\n"
                    f"- MUST include WHERE d.company_id = '{company_id}' "
                    "(or AND d.company_id = ... when other filters exist).\n"
                    "- Prefer latest snapshot per device using MAX(collected_at) subqueries when needed.\n"
                    "- Return ONLY the SQL statement."
                )
            ),
            HumanMessage(content=state["input_query"]),
        ]
    )
    return {"generated_sql": _strip_sql(str(response.content))}


def sql_execution_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    try:
        result = execute_fleet_query(
            sql_query=state["generated_sql"],
            company_id=state["company_id"],
            thread_id=_thread_id(config),
            natural_language_context=state["input_query"],
        )
        rows = []
        for row in result["raw_rows"]:
            enriched = dict(row)
            enriched["_tracing"] = result["tracing_metadata"]
            rows.append(enriched)
        return {"query_results": rows}
    except SecurityError as exc:
        return {
            "query_results": [],
            "detected_insights": [],
            "final_response": f"Security violation: {exc}",
        }


def insight_detection_node(state: AgentState) -> dict[str, Any]:
    if state.get("final_response"):
        return {}

    if not (state.get("query_results") or []):
        return {"detected_insights": []}

    return {"detected_insights": detect_fleet_insights(state["company_id"])}


def _rule_based_action_proposal(
    state: AgentState, config: RunnableConfig
) -> dict[str, Any]:
    company_id = state["company_id"]
    thread_id = _thread_id(config)
    staged_actions: list[dict[str, Any]] = []
    query_lower = state["input_query"].lower()

    for row in _latest_anomalies(company_id):
        device_id = row["device_id"]
        employee_id = row["employee_id"]
        disk_util = _disk_utilization(row)
        memory_util = _memory_utilization(row)
        battery_pct = row.get("battery_percentage")

        if row.get("compliance_status") == "fail" and row.get("check_id"):
            try:
                open_remediation_ticket(
                    device_id=device_id,
                    check_id=row["check_id"],
                    note=f"Automated remediation for failing {row['check_id']}.",
                    company_id=company_id,
                    thread_id=thread_id,
                    staged_actions=staged_actions,
                )
            except ValueError:
                pass

        if battery_pct is not None and battery_pct < 50:
            try:
                flag_device_for_replacement(
                    device_id=device_id,
                    reason="Battery health below 50% on latest telemetry snapshot.",
                    company_id=company_id,
                    thread_id=thread_id,
                    staged_actions=staged_actions,
                )
            except ValueError:
                pass

        if disk_util > 90.0 and any(
            token in query_lower for token in ("disk", "storage", "upgrade")
        ):
            try:
                create_upgrade_order(
                    device_id=device_id,
                    component="disk",
                    spec="1TB SSD",
                    company_id=company_id,
                    thread_id=thread_id,
                    staged_actions=staged_actions,
                )
            except ValueError:
                pass

        if memory_util > 90.0 and "memory" in query_lower:
            try:
                create_upgrade_order(
                    device_id=device_id,
                    component="memory",
                    spec="32GB RAM",
                    company_id=company_id,
                    thread_id=thread_id,
                    staged_actions=staged_actions,
                )
            except ValueError:
                pass

        if any(token in query_lower for token in ("notify", "employee", "message")):
            try:
                notify_employee(
                    employee_id=employee_id,
                    message="Fleet Copilot detected a device issue requiring your attention.",
                    company_id=company_id,
                    thread_id=thread_id,
                    staged_actions=staged_actions,
                )
            except ValueError:
                pass

    return {"proposed_actions": staged_actions}


def _build_action_tools(
    company_id: str,
    thread_id: str,
    staged_actions: list[dict[str, Any]],
) -> list[StructuredTool]:
    def flag_replacement(device_id: str, reason: str) -> str:
        flag_device_for_replacement(
            device_id=device_id,
            reason=reason,
            company_id=company_id,
            thread_id=thread_id,
            staged_actions=staged_actions,
        )
        return f"Staged flag_device_for_replacement for {device_id}"

    def remediation_ticket(device_id: str, check_id: str, note: str) -> str:
        open_remediation_ticket(
            device_id=device_id,
            check_id=check_id,
            note=note,
            company_id=company_id,
            thread_id=thread_id,
            staged_actions=staged_actions,
        )
        return f"Staged open_remediation_ticket for {device_id}"

    def upgrade_order(device_id: str, component: str, spec: str) -> str:
        create_upgrade_order(
            device_id=device_id,
            component=component,
            spec=spec,
            company_id=company_id,
            thread_id=thread_id,
            staged_actions=staged_actions,
        )
        return f"Staged create_upgrade_order for {device_id}"

    def employee_notification(employee_id: str, message: str) -> str:
        notify_employee(
            employee_id=employee_id,
            message=message,
            company_id=company_id,
            thread_id=thread_id,
            staged_actions=staged_actions,
        )
        return f"Staged notify_employee for {employee_id}"

    return [
        StructuredTool.from_function(
            func=flag_replacement,
            name="flag_device_for_replacement",
            description="Flag a device for replacement when battery or disk telemetry justifies it.",
        ),
        StructuredTool.from_function(
            func=remediation_ticket,
            name="open_remediation_ticket",
            description="Open a remediation ticket for a failing compliance check on a device.",
        ),
        StructuredTool.from_function(
            func=upgrade_order,
            name="create_upgrade_order",
            description="Create a hardware upgrade order for disk, memory, or battery components.",
        ),
        StructuredTool.from_function(
            func=employee_notification,
            name="notify_employee",
            description="Notify an employee about a justified device issue on their fleet hardware.",
        ),
    ]


def _llm_tool_action_proposal(
    state: AgentState, config: RunnableConfig
) -> dict[str, Any]:
    company_id = state["company_id"]
    thread_id = _thread_id(config)
    staged_actions: list[dict[str, Any]] = []
    anomalies = _latest_anomalies(company_id)
    tools = _build_action_tools(company_id, thread_id, staged_actions)
    tool_map = {tool.name: tool for tool in tools}
    llm = _get_llm().bind_tools(tools)

    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are the Fleet Copilot action planner. Review telemetry and "
                    "call tools to propose justified operational actions.\n"
                    "Rules:\n"
                    "- Only call tools when telemetry evidence supports the action.\n"
                    "- Use flag_device_for_replacement for failing batteries or critical disk pressure.\n"
                    "- Use open_remediation_ticket for failing compliance checks.\n"
                    "- Use create_upgrade_order for disk/memory constraints.\n"
                    "- Use notify_employee when employee outreach is requested and issues exist.\n"
                    "- You may call zero or more tools."
                )
            ),
            HumanMessage(
                content=(
                    f"Company ID: {company_id}\n"
                    f"User query: {state['input_query']}\n"
                    f"Latest telemetry anomalies:\n"
                    f"{json.dumps(anomalies, default=str)}"
                )
            ),
        ]
    )

    if isinstance(response, AIMessage):
        for tool_call in response.tool_calls or []:
            tool = tool_map.get(tool_call["name"])
            if tool is None:
                continue
            try:
                tool.invoke(tool_call["args"])
            except (ValueError, TypeError):
                continue

    return {"proposed_actions": staged_actions}


def action_proposal_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    if _llm_available():
        try:
            llm_result = _llm_tool_action_proposal(state, config)
            if llm_result.get("proposed_actions"):
                return llm_result
        except Exception:
            pass
    return _rule_based_action_proposal(state, config)


def _latest_anomalies(company_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                ts.snapshot_id,
                ts.device_id,
                d.employee_id,
                ts.collected_at,
                ts.battery_percentage,
                ts.disk_size_bytes,
                ts.disk_available_bytes,
                ts.used_memory_bytes,
                ts.total_memory_bytes,
                cc.check_id,
                cc.status AS compliance_status,
                cc.severity
            FROM telemetry_snapshots ts
            INNER JOIN devices d ON d.device_id = ts.device_id
            LEFT JOIN compliance_checks cc
              ON cc.snapshot_id = ts.snapshot_id
            INNER JOIN (
                SELECT device_id, MAX(collected_at) AS collected_at
                FROM telemetry_snapshots
                GROUP BY device_id
            ) latest
              ON ts.device_id = latest.device_id
             AND ts.collected_at = latest.collected_at
            WHERE d.company_id = ?
            """,
            (company_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _disk_utilization(row: dict[str, Any]) -> float:
    size_bytes = row.get("disk_size_bytes") or 0
    available_bytes = row.get("disk_available_bytes") or 0
    if size_bytes <= 0:
        return 0.0
    return ((size_bytes - available_bytes) / size_bytes) * 100.0


def _memory_utilization(row: dict[str, Any]) -> float:
    total_bytes = row.get("total_memory_bytes") or 0
    used_bytes = row.get("used_memory_bytes") or 0
    if total_bytes <= 0:
        return 0.0
    return (used_bytes / total_bytes) * 100.0


def action_execution_guardrail_node(
    state: AgentState, config: RunnableConfig
) -> dict[str, Any]:
    proposed = state.get("proposed_actions") or []
    if not proposed:
        return {}

    decision = interrupt(
        {
            "kind": "human_approval_required",
            "message": "Review proposed fleet actions. Resume with 'approve' or 'reject'.",
            "proposed_actions": proposed,
        }
    )
    normalized = str(decision).strip().lower()
    if normalized not in {"approve", "reject"}:
        normalized = "reject"

    conn = sqlite3.connect(DB_PATH)
    try:
        log_audit_event(
            conn,
            thread_id=_thread_id(config),
            company_id=state["company_id"],
            actor="admin",
            action_type="human_approval",
            details_dict={
                "decision": normalized,
                "proposed_actions": proposed,
            },
        )
    finally:
        conn.close()

    updates: dict[str, Any] = {"approval_decision": normalized}
    if normalized == "reject":
        updates["proposed_actions"] = []
    return updates


def response_synthesizer_node(state: AgentState) -> dict[str, Any]:
    if state.get("final_response"):
        return {}

    if not _llm_available():
        return {"final_response": _grounded_fallback_response(state)}

    llm = _get_llm()
    context = {
        "input_query": state["input_query"],
        "company_id": state["company_id"],
        "query_results": state.get("query_results") or [],
        "detected_insights": state.get("detected_insights") or [],
        "proposed_actions": state.get("proposed_actions") or [],
        "approval_decision": state.get("approval_decision") or "",
        "generated_sql": state.get("generated_sql") or "",
    }
    try:
        response = llm.invoke(
            [
                SystemMessage(
                    content=(
                        "You are the Fleet Copilot response synthesizer.\n"
                        "Ground every claim ONLY in the provided query_results, "
                        "detected_insights, and proposed_actions.\n"
                        "Format findings as markdown bullet points or compact tables.\n"
                        "Every factual statement MUST include a citation marker:\n"
                        "[Source: table_name/primary_key_value]\n"
                        "Use snapshot_id for telemetry_snapshots, device_id for devices, "
                        "compliance_id for compliance_checks.\n"
                        "Summarize detected_insights with finding, evidence, and brief explanation.\n"
                        "If proposed_actions exist, summarize them and note approval_decision.\n"
                        "Do not invent data."
                    )
                ),
                HumanMessage(content=json.dumps(context, default=str)),
            ]
        )
        return {"final_response": str(response.content).strip()}
    except Exception:
        return {"final_response": _grounded_fallback_response(state)}


def build_graph():
    builder = StateGraph(AgentState)

    builder.add_node("planner", planner_node)
    builder.add_node("sql_generation", sql_generation_node)
    builder.add_node("sql_execution", sql_execution_node)
    builder.add_node("insight_detection", insight_detection_node)
    builder.add_node("action_proposal", action_proposal_node)
    builder.add_node("action_execution_guardrail", action_execution_guardrail_node)
    builder.add_node("response_synthesizer", response_synthesizer_node)

    builder.add_edge(START, "planner")
    builder.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "sql_generation": "sql_generation",
            "action_proposal": "action_proposal",
        },
    )
    builder.add_edge("sql_generation", "sql_execution")
    builder.add_edge("sql_execution", "insight_detection")
    builder.add_edge("insight_detection", "response_synthesizer")
    builder.add_edge("action_proposal", "action_execution_guardrail")
    builder.add_edge("action_execution_guardrail", "response_synthesizer")
    builder.add_edge("response_synthesizer", END)

    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


graph = build_graph()
