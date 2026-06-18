import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langgraph.types import Command

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent import graph as graph_module
from src.agent.graph import AgentState, build_graph, route_after_planner
from src.agent.tools import (
    SecurityError,
    create_upgrade_order,
    execute_fleet_query,
    flag_device_for_replacement,
)
from src.database.ingest import create_schema

COMPANY_A_ID = "acme-001"
COMPANY_B_ID = "globex-002"

DEVICE_HEALTHY = "DEV-A1-HEALTHY"
DEVICE_LOW_BATTERY = "DEV-A2-LOW-BATTERY"
DEVICE_LOW_DISK = "DEV-A3-LOW-DISK"
DEVICE_GLOBEX = "DEV-B1-GLOBEX"

ACTION_REFUSAL_MESSAGE = "Action Refused: Insufficient telemetry evidence"
SOURCE_CITATION_PATTERN = re.compile(r"\[Source: telemetry_snapshots/[^\]]+\]")
UNAVAILABLE_MARKERS = (
    "unavailable",
    "unrecognized",
    "not available",
    "no grounded fleet results",
    "not found",
    "no data",
)


def _insert_snapshot(
    conn: sqlite3.Connection,
    device_id: str,
    collected_at: str,
    battery_percentage: int,
    disk_size_bytes: int,
    disk_available_bytes: int,
    total_memory_bytes: int = 8_000_000_000,
    used_memory_bytes: int = 2_000_000_000,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO telemetry_snapshots (
            device_id,
            collected_at,
            battery_present,
            battery_percentage,
            disk_size_bytes,
            disk_available_bytes,
            total_memory_bytes,
            used_memory_bytes
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            collected_at,
            battery_percentage,
            disk_size_bytes,
            disk_available_bytes,
            total_memory_bytes,
            used_memory_bytes,
        ),
    )
    return int(cursor.lastrowid)


def seed_test_database(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        create_schema(conn)
        conn.executemany(
            """
            INSERT INTO devices (device_id, company_id, employee_id)
            VALUES (?, ?, ?)
            """,
            [
                (DEVICE_HEALTHY, COMPANY_A_ID, "EMP-A1"),
                (DEVICE_LOW_BATTERY, COMPANY_A_ID, "EMP-A2"),
                (DEVICE_LOW_DISK, COMPANY_A_ID, "EMP-A3"),
                (DEVICE_GLOBEX, COMPANY_B_ID, "EMP-B1"),
            ],
        )

        snapshot_ids = {
            "healthy": _insert_snapshot(
                conn,
                DEVICE_HEALTHY,
                "2026-01-01T00:00:00Z",
                battery_percentage=85,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=500_000_000,
            ),
            "low_battery": _insert_snapshot(
                conn,
                DEVICE_LOW_BATTERY,
                "2026-01-02T00:00:00Z",
                battery_percentage=35,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=800_000_000,
            ),
            "low_disk": _insert_snapshot(
                conn,
                DEVICE_LOW_DISK,
                "2026-01-03T00:00:00Z",
                battery_percentage=90,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=50_000_000,
            ),
            "globex": _insert_snapshot(
                conn,
                DEVICE_GLOBEX,
                "2026-01-01T00:00:00Z",
                battery_percentage=80,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=500_000_000,
            ),
        }

        globex_snapshot_id = snapshot_ids["globex"]
        conn.execute(
            """
            INSERT INTO compliance_checks (
                snapshot_id, device_id, collected_at, check_id, status, severity
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                globex_snapshot_id,
                DEVICE_GLOBEX,
                "2026-01-01T00:00:00Z",
                "os_up_to_date",
                "fail",
                "high",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "company_a": COMPANY_A_ID,
        "company_b": COMPANY_B_ID,
        "snapshot_ids": snapshot_ids,
        "devices": {
            "healthy": DEVICE_HEALTHY,
            "low_battery": DEVICE_LOW_BATTERY,
            "low_disk": DEVICE_LOW_DISK,
            "globex": DEVICE_GLOBEX,
        },
    }


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    db_path = tmp_path / "telemetry.db"
    metadata = seed_test_database(db_path)
    monkeypatch.setattr("src.agent.tools.DB_PATH", db_path)
    monkeypatch.setattr("src.database.ingest.DB_PATH", db_path)
    monkeypatch.setattr("src.agent.graph.DB_PATH", db_path)
    return metadata


@pytest.fixture
def thread_config() -> dict[str, Any]:
    return {"configurable": {"thread_id": f"test-{uuid.uuid4()}"}}


def _initial_state(message: str, company_id: str) -> AgentState:
    return {
        "input_query": message,
        "company_id": company_id,
        "current_plan": [],
        "generated_sql": "",
        "query_results": [],
        "proposed_actions": [],
        "approval_decision": "",
        "final_response": "",
    }


def _snapshot(graph: Any, config: dict[str, Any]) -> Any:
    return graph.get_state(config)


def _is_paused(graph: Any, config: dict[str, Any]) -> bool:
    return bool(_snapshot(graph, config).interrupts)


def _force_sql_plan(state: AgentState) -> dict[str, Any]:
    return {"current_plan": [{"route": "sql"}, {"step": "Forced SQL route for deterministic eval."}]}


def _force_action_plan(state: AgentState) -> dict[str, Any]:
    return {"current_plan": [{"route": "action"}, {"step": "Forced action route for deterministic eval."}]}


def test_grounding_guardrails(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    config = thread_config

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

    with (
        patch.object(graph_module, "_llm_available", return_value=False),
        patch.object(graph_module, "planner_node", _force_sql_plan),
        patch.object(graph_module, "sql_generation_node", _empty_metric_sql),
    ):
        graph = build_graph()
        result = graph.invoke(
            _initial_state(
                "What is the quantum_flux_capacity for our fleet?",
                COMPANY_A_ID,
            ),
            config,
        )

    response = (result.get("final_response") or "").lower()
    query_rows = result.get("query_results") or []

    assert query_rows == []
    assert any(marker in response for marker in UNAVAILABLE_MARKERS)


def test_tenant_isolation_enforcement(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    config = thread_config

    def _injected_cross_tenant_sql(state: AgentState) -> dict[str, Any]:
        return {
            "generated_sql": (
                "SELECT cc.compliance_id, cc.device_id, cc.check_id, cc.status "
                "FROM compliance_checks cc "
                "INNER JOIN devices d ON d.device_id = cc.device_id "
                f"WHERE d.company_id = '{COMPANY_B_ID}'"
            )
        }

    with (
        patch.object(graph_module, "_llm_available", return_value=False),
        patch.object(graph_module, "planner_node", _force_sql_plan),
        patch.object(graph_module, "sql_generation_node", _injected_cross_tenant_sql),
    ):
        graph = build_graph()
        result = graph.invoke(
            _initial_state(
                "List compliance tickets for Company_B",
                COMPANY_A_ID,
            ),
            config,
        )

    response = result.get("final_response") or ""
    rows = result.get("query_results") or []

    assert rows == []
    assert "security violation" in response.lower()

    with pytest.raises(SecurityError):
        execute_fleet_query(
            sql_query=(
                "SELECT cc.compliance_id FROM compliance_checks cc "
                "INNER JOIN devices d ON d.device_id = cc.device_id "
                f"WHERE d.company_id = '{COMPANY_B_ID}'"
            ),
            company_id=COMPANY_A_ID,
            thread_id=str(config["configurable"]["thread_id"]),
            natural_language_context="List compliance tickets for Company_B",
        )


def test_action_proposal_generation(
    patched_db: dict[str, Any],
    thread_config: dict[str, Any],
) -> None:
    config = thread_config
    state = _initial_state(
        "Propose replacement actions for devices with battery failures",
        COMPANY_A_ID,
    )

    visited: list[str] = []
    with (
        patch.object(graph_module, "_llm_available", return_value=False),
        patch.object(graph_module, "planner_node", _force_action_plan),
    ):
        graph = build_graph()
        for event in graph.stream(state, config, stream_mode="updates"):
            visited.extend(event.keys())
        snapshot = _snapshot(graph, config)

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

    payload = replacement_actions[0]["action"]
    assert payload == {
        "action_type": "flag_device_for_replacement",
        "device_id": patched_db["devices"]["low_battery"],
        "reason": "Battery health below 50% on latest telemetry snapshot.",
    }
    assert replacement_actions[0]["evidence"]["replacement_trigger"] == "battery_health_below_50"
    assert replacement_actions[0]["evidence"]["battery_percentage"] == 35


def test_evidence_traceability(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    config = thread_config

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

    with (
        patch.object(graph_module, "_llm_available", return_value=False),
        patch.object(graph_module, "planner_node", _force_sql_plan),
        patch.object(graph_module, "sql_generation_node", _low_disk_sql),
    ):
        graph = build_graph()
        result = graph.invoke(
            _initial_state("Show devices low on disk space", COMPANY_A_ID),
            config,
        )

    response = result.get("final_response") or ""
    rows = result.get("query_results") or []

    assert len(rows) >= 1
    assert SOURCE_CITATION_PATTERN.search(response) is not None


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
    config = thread_config
    state = _initial_state(
        "Propose replacement for devices with failing battery health",
        COMPANY_A_ID,
    )

    with (
        patch.object(graph_module, "_llm_available", return_value=False),
        patch.object(graph_module, "planner_node", _force_action_plan),
    ):
        graph = build_graph()
        graph.invoke(state, config)

        snapshot = _snapshot(graph, config)
        assert _is_paused(graph, config)
        assert snapshot.interrupts
        interrupt_payload = snapshot.interrupts[0].value
        assert interrupt_payload["kind"] == "human_approval_required"
        assert len(snapshot.values.get("proposed_actions") or []) >= 1
        assert not snapshot.values.get("final_response")
        assert snapshot.next

        graph.invoke(Command(resume="approve"), config)

        completed = _snapshot(graph, config)
        assert not completed.interrupts
        assert completed.values.get("approval_decision") == "approve"
        assert (completed.values.get("final_response") or "").strip()
