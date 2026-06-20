import re
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.agent import graph as graph_module
from src.agent.graph import AgentState, build_graph
from src.database.ingest import create_schema

COMPANY_A_ID = "acme-001"
COMPANY_B_ID = "globex-002"
COMPANY_C_ID = "initech-003"

DEVICE_HEALTHY = "DEV-A1-HEALTHY"
DEVICE_LOW_BATTERY = "DEV-A2-LOW-BATTERY"
DEVICE_LOW_DISK = "DEV-A3-LOW-DISK"
DEVICE_COMPLIANCE_FAIL = "DEV-A4-COMPLIANCE-FAIL"
DEVICE_TREND_BATTERY = "DEV-A5-TREND-BATTERY"
DEVICE_HIGH_MEMORY = "DEV-A6-HIGH-MEMORY"
DEVICE_GLOBEX = "DEV-B1-GLOBEX"

ACTION_REFUSAL_MESSAGE = "Action Refused: Insufficient telemetry evidence"
SOURCE_CITATION_PATTERN = re.compile(r"\[Source: telemetry_snapshots/[^\]]+\]")
COMPLIANCE_CITATION_PATTERN = re.compile(r"\[Source: compliance_checks/[^\]]+\]")
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


def _insert_compliance(
    conn: sqlite3.Connection,
    snapshot_id: int,
    device_id: str,
    collected_at: str,
    check_id: str,
    status: str,
    severity: str,
) -> None:
    conn.execute(
        """
        INSERT INTO compliance_checks (
            snapshot_id, device_id, collected_at, check_id, status, severity
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (snapshot_id, device_id, collected_at, check_id, status, severity),
    )


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
                (DEVICE_COMPLIANCE_FAIL, COMPANY_A_ID, "EMP-A4"),
                (DEVICE_TREND_BATTERY, COMPANY_A_ID, "EMP-A5"),
                (DEVICE_HIGH_MEMORY, COMPANY_A_ID, "EMP-A6"),
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
            "compliance_fail": _insert_snapshot(
                conn,
                DEVICE_COMPLIANCE_FAIL,
                "2026-01-04T00:00:00Z",
                battery_percentage=88,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=700_000_000,
            ),
            "trend_battery_old": _insert_snapshot(
                conn,
                DEVICE_TREND_BATTERY,
                "2026-01-01T00:00:00Z",
                battery_percentage=80,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=700_000_000,
            ),
            "trend_battery_mid": _insert_snapshot(
                conn,
                DEVICE_TREND_BATTERY,
                "2026-01-15T00:00:00Z",
                battery_percentage=55,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=700_000_000,
            ),
            "trend_battery_latest": _insert_snapshot(
                conn,
                DEVICE_TREND_BATTERY,
                "2026-01-30T00:00:00Z",
                battery_percentage=38,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=700_000_000,
            ),
            "high_memory": _insert_snapshot(
                conn,
                DEVICE_HIGH_MEMORY,
                "2026-01-05T00:00:00Z",
                battery_percentage=90,
                disk_size_bytes=1_000_000_000,
                disk_available_bytes=700_000_000,
                total_memory_bytes=10_000_000_000,
                used_memory_bytes=9_600_000_000,
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

        _insert_compliance(
            conn,
            snapshot_ids["compliance_fail"],
            DEVICE_COMPLIANCE_FAIL,
            "2026-01-04T00:00:00Z",
            "screen_lock",
            "fail",
            "high",
        )
        _insert_compliance(
            conn,
            snapshot_ids["globex"],
            DEVICE_GLOBEX,
            "2026-01-01T00:00:00Z",
            "os_up_to_date",
            "fail",
            "high",
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "company_a": COMPANY_A_ID,
        "company_b": COMPANY_B_ID,
        "company_c": COMPANY_C_ID,
        "snapshot_ids": snapshot_ids,
        "devices": {
            "healthy": DEVICE_HEALTHY,
            "low_battery": DEVICE_LOW_BATTERY,
            "low_disk": DEVICE_LOW_DISK,
            "compliance_fail": DEVICE_COMPLIANCE_FAIL,
            "trend_battery": DEVICE_TREND_BATTERY,
            "high_memory": DEVICE_HIGH_MEMORY,
            "globex": DEVICE_GLOBEX,
        },
    }


def initial_state(message: str, company_id: str) -> AgentState:
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


def force_sql_plan(state: AgentState) -> dict[str, Any]:
    return {"current_plan": [{"route": "sql"}, {"step": "Forced SQL route for deterministic eval."}]}


def force_action_plan(state: AgentState) -> dict[str, Any]:
    return {"current_plan": [{"route": "action"}, {"step": "Forced action route for deterministic eval."}]}


def graph_snapshot(graph: Any, config: dict[str, Any]) -> Any:
    return graph.get_state(config)


def is_paused(graph: Any, config: dict[str, Any]) -> bool:
    return bool(graph_snapshot(graph, config).interrupts)


def build_patched_graph(*, sql_handler=None, action_plan: bool = False) -> Any:
    patches = [patch.object(graph_module, "_llm_available", return_value=False)]
    if action_plan:
        patches.append(patch.object(graph_module, "planner_node", force_action_plan))
    else:
        patches.append(patch.object(graph_module, "planner_node", force_sql_plan))
    if sql_handler is not None:
        patches.append(patch.object(graph_module, "sql_generation_node", sql_handler))

    for patcher in patches:
        patcher.start()
    try:
        return build_graph()
    except Exception:
        for patcher in patches:
            patcher.stop()
        raise
