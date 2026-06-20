from typing import Any

import pytest
from langgraph.types import Command

from evals.helpers import (
    ACTION_REFUSAL_MESSAGE,
    COMPANY_A_ID,
    COMPANY_B_ID,
    SOURCE_CITATION_PATTERN,
    UNAVAILABLE_MARKERS,
    build_patched_graph,
    force_action_plan,
    force_sql_plan,
    graph_snapshot,
    initial_state,
    is_paused,
)
from src.agent import graph as graph_module
from src.agent.graph import AgentState, route_after_planner
from src.agent.tools import (
    SecurityError,
    create_upgrade_order,
    execute_fleet_query,
    flag_device_for_replacement,
)


def test_grounding_guardrails(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    def _empty_metric_sql(state: AgentState) -> dict[str, Any]:
        company_id = state["company_id"]
        return {
            "generated_sql": (
                "SELECT d.device_id, ts.snapshot_id "
                "FROM telemetry_snapshots ts "
                "INNER JOIN devices d ON d.device_id = ts.device_id "
                f"WHERE d.company_id = '{company_id}' "
                "AND ts.battery_percentage < 0"
            )
        }

    graph = build_patched_graph(sql_handler=_empty_metric_sql)
    result = graph.invoke(
        initial_state("What is the quantum_flux_capacity for our fleet?", COMPANY_A_ID),
        thread_config,
    )

    response = (result.get("final_response") or "").lower()
    assert (result.get("query_results") or []) == []
    assert any(marker in response for marker in UNAVAILABLE_MARKERS)


def test_tenant_isolation_enforcement(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    def _injected_cross_tenant_sql(state: AgentState) -> dict[str, Any]:
        return {
            "generated_sql": (
                "SELECT cc.compliance_id, cc.device_id, cc.check_id, cc.status "
                "FROM compliance_checks cc "
                "INNER JOIN devices d ON d.device_id = cc.device_id "
                f"WHERE d.company_id = '{COMPANY_B_ID}'"
            )
        }

    graph = build_patched_graph(sql_handler=_injected_cross_tenant_sql)
    result = graph.invoke(
        initial_state("List compliance tickets for Globex", COMPANY_A_ID),
        thread_config,
    )

    assert (result.get("query_results") or []) == []
    assert "security violation" in (result.get("final_response") or "").lower()

    with pytest.raises(SecurityError):
        execute_fleet_query(
            sql_query=(
                "SELECT cc.compliance_id FROM compliance_checks cc "
                "INNER JOIN devices d ON d.device_id = cc.device_id "
                f"WHERE d.company_id = '{COMPANY_B_ID}'"
            ),
            company_id=COMPANY_A_ID,
            thread_id=str(thread_config["configurable"]["thread_id"]),
            natural_language_context="List compliance tickets for Globex",
        )


def test_action_proposal_generation(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    graph = build_patched_graph(action_plan=True)
    state = initial_state(
        "Propose replacement actions for devices with battery failures",
        COMPANY_A_ID,
    )

    visited: list[str] = []
    for event in graph.stream(state, thread_config, stream_mode="updates"):
        visited.extend(event.keys())
    snapshot = graph_snapshot(graph, thread_config)
    proposed = snapshot.values.get("proposed_actions") or []

    assert "planner" in visited
    assert "action_proposal" in visited
    assert route_after_planner({"current_plan": [{"route": "action"}]}) == "action_proposal"

    replacement_actions = [
        item
        for item in proposed
        if item.get("action", {}).get("action_type") == "flag_device_for_replacement"
    ]
    assert len(replacement_actions) >= 1
    assert replacement_actions[0]["action"] == {
        "action_type": "flag_device_for_replacement",
        "device_id": patched_db["devices"]["low_battery"],
        "reason": "Battery health below 50% on latest telemetry snapshot.",
    }
    assert replacement_actions[0]["evidence"]["replacement_trigger"] == "battery_health_below_50"


def test_evidence_traceability(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    def _low_disk_sql(state: AgentState) -> dict[str, Any]:
        company_id = state["company_id"]
        return {
            "generated_sql": (
                "SELECT d.device_id, ts.snapshot_id, ts.collected_at, "
                "ts.disk_size_bytes, ts.disk_available_bytes "
                "FROM telemetry_snapshots ts "
                "INNER JOIN devices d ON d.device_id = ts.device_id "
                f"WHERE d.company_id = '{company_id}' "
                "AND CAST(ts.disk_size_bytes - ts.disk_available_bytes AS REAL) "
                "/ ts.disk_size_bytes > 0.90 "
                "ORDER BY ts.collected_at DESC"
            )
        }

    graph = build_patched_graph(sql_handler=_low_disk_sql)
    result = graph.invoke(
        initial_state("Show devices low on disk space", COMPANY_A_ID),
        thread_config,
    )

    assert len(result.get("query_results") or []) >= 1
    assert SOURCE_CITATION_PATTERN.search(result.get("final_response") or "") is not None


def test_unsupported_action_refusal(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    thread_id = str(thread_config["configurable"]["thread_id"])

    with pytest.raises(ValueError, match=ACTION_REFUSAL_MESSAGE):
        create_upgrade_order(
            device_id=patched_db["devices"]["healthy"],
            component="disk",
            spec="1TB SSD",
            company_id=COMPANY_A_ID,
            thread_id=thread_id,
        )

    with pytest.raises(ValueError, match=ACTION_REFUSAL_MESSAGE):
        flag_device_for_replacement(
            device_id=patched_db["devices"]["healthy"],
            reason="Manual replacement request for healthy device.",
            company_id=COMPANY_A_ID,
            thread_id=thread_id,
        )


def test_human_in_the_loop_checkpoint(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    graph = build_patched_graph(action_plan=True)
    graph.invoke(
        initial_state(
            "Propose replacement for devices with failing battery health",
            COMPANY_A_ID,
        ),
        thread_config,
    )

    snapshot = graph_snapshot(graph, thread_config)
    assert is_paused(graph, thread_config)
    assert snapshot.interrupts[0].value["kind"] == "human_approval_required"
    assert len(snapshot.values.get("proposed_actions") or []) >= 1
    assert not snapshot.values.get("final_response")
    assert snapshot.next

    graph.invoke(Command(resume="approve"), thread_config)
    completed = graph_snapshot(graph, thread_config)
    assert not completed.interrupts
    assert completed.values.get("approval_decision") == "approve"
    assert (completed.values.get("final_response") or "").strip()


def test_planner_heuristic_routing() -> None:
    sql_state = initial_state("What is fleet disk utilization?", COMPANY_A_ID)
    action_state = initial_state("Propose remediation and upgrade actions", COMPANY_A_ID)

    sql_plan = graph_module._heuristic_plan(sql_state)
    action_plan = graph_module._heuristic_plan(action_state)

    assert sql_plan["current_plan"][0]["route"] == "sql"
    assert action_plan["current_plan"][0]["route"] == "action"


def test_trend_battery_decline_detection(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    def _trend_sql(state: AgentState) -> dict[str, Any]:
        company_id = state["company_id"]
        device_id = patched_db["devices"]["trend_battery"]
        return {
            "generated_sql": (
                "SELECT ts.device_id, ts.snapshot_id, ts.collected_at, ts.battery_percentage "
                "FROM telemetry_snapshots ts "
                "INNER JOIN devices d ON d.device_id = ts.device_id "
                f"WHERE d.company_id = '{company_id}' "
                f"AND ts.device_id = '{device_id}' "
                "ORDER BY ts.collected_at ASC"
            )
        }

    graph = build_patched_graph(sql_handler=_trend_sql)
    result = graph.invoke(
        initial_state("Show battery decline trend for devices approaching end-of-life", COMPANY_A_ID),
        thread_config,
    )

    rows = result.get("query_results") or []
    batteries = [row["battery_percentage"] for row in rows]
    assert len(batteries) >= 3
    assert batteries == [80, 55, 38]
    assert batteries[0] > batteries[-1]
    assert SOURCE_CITATION_PATTERN.search(result.get("final_response") or "") is not None


def test_compliance_high_severity_grounding(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    def _compliance_sql(state: AgentState) -> dict[str, Any]:
        company_id = state["company_id"]
        return {
            "generated_sql": (
                "SELECT cc.compliance_id, cc.device_id, cc.check_id, cc.status, cc.severity "
                "FROM compliance_checks cc "
                "INNER JOIN devices d ON d.device_id = cc.device_id "
                f"WHERE d.company_id = '{company_id}' "
                "AND cc.status = 'fail' AND cc.severity = 'high'"
            )
        }

    graph = build_patched_graph(sql_handler=_compliance_sql)
    result = graph.invoke(
        initial_state("Show laptops failing high-severity compliance checks", COMPANY_A_ID),
        thread_config,
    )

    rows = result.get("query_results") or []
    assert len(rows) >= 1
    assert any(row.get("severity") == "high" for row in rows)
    response = result.get("final_response") or ""
    assert "[Source:" in response
    assert patched_db["devices"]["compliance_fail"] in response


def test_hitl_reject_clears_proposed_actions(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    graph = build_patched_graph(action_plan=True)
    graph.invoke(
        initial_state("Propose replacement actions for battery failures", COMPANY_A_ID),
        thread_config,
    )
    assert is_paused(graph, thread_config)

    graph.invoke(Command(resume="reject"), thread_config)
    completed = graph_snapshot(graph, thread_config)

    assert completed.values.get("approval_decision") == "reject"
    assert completed.values.get("proposed_actions") == []


def test_sql_readonly_blocks_mutations(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    with pytest.raises(SecurityError, match="read-only"):
        execute_fleet_query(
            sql_query=(
                "DELETE FROM devices d WHERE d.company_id = 'acme-001'"
            ),
            company_id=COMPANY_A_ID,
            thread_id=str(thread_config["configurable"]["thread_id"]),
        )


def test_insight_detection_node_in_graph(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    def _fleet_health_sql(state: AgentState) -> dict[str, Any]:
        company_id = state["company_id"]
        return {
            "generated_sql": (
                "SELECT d.device_id, ts.snapshot_id, ts.collected_at "
                "FROM telemetry_snapshots ts "
                "INNER JOIN devices d ON d.device_id = ts.device_id "
                f"WHERE d.company_id = '{company_id}' "
                "ORDER BY ts.collected_at DESC LIMIT 20"
            )
        }

    graph = build_patched_graph(sql_handler=_fleet_health_sql)
    result = graph.invoke(
        initial_state("Show fleet health trends and drift patterns", COMPANY_A_ID),
        thread_config,
    )

    insights = result.get("detected_insights") or []
    insight_types = {item["insight_type"] for item in insights}
    assert "battery_decline" in insight_types
    assert "compliance_drift" in insight_types
    response = result.get("final_response") or ""
    assert "### Fleet insights" in response
    assert "[Source:" in response


def test_llm_tool_action_proposal(
    patched_db: dict[str, Any],
    thread_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock, patch

    from langchain_core.messages import AIMessage

    from evals.helpers import force_action_plan
    from src.agent import graph as graph_module
    from src.agent.graph import build_graph

    tool_call = {
        "name": "flag_device_for_replacement",
        "args": {
            "device_id": patched_db["devices"]["low_battery"],
            "reason": "Battery health below 50% on latest telemetry snapshot.",
        },
        "id": "call-1",
        "type": "tool_call",
    }
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(
        content="",
        tool_calls=[tool_call],
    )

    monkeypatch.setattr(graph_module, "_llm_available", lambda: True)
    monkeypatch.setattr(graph_module, "_get_llm", lambda: mock_llm)

    with patch.object(graph_module, "planner_node", force_action_plan):
        graph = build_graph()
        result = graph.invoke(
            initial_state(
                "Propose replacement for failing battery devices", COMPANY_A_ID
            ),
            thread_config,
        )

    proposed = result.get("proposed_actions") or []
    assert len(proposed) >= 1
    assert proposed[0]["action"]["action_type"] == "flag_device_for_replacement"
    assert proposed[0]["action"]["device_id"] == patched_db["devices"]["low_battery"]
    mock_llm.bind_tools.assert_called_once()
