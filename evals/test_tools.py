import sqlite3
from typing import Any

import pytest

from evals.helpers import (
    ACTION_REFUSAL_MESSAGE,
    COMPANY_A_ID,
    COMPANY_B_ID,
    build_patched_graph,
    initial_state,
    graph_snapshot,
)
from src.agent.tools import (
    create_upgrade_order,
    execute_fleet_query,
    notify_employee,
    open_remediation_ticket,
)


def test_execute_fleet_query_happy_path_and_audit(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    thread_id = str(thread_config["configurable"]["thread_id"])
    result = execute_fleet_query(
        sql_query=(
            "SELECT d.device_id, ts.snapshot_id, ts.battery_percentage "
            "FROM telemetry_snapshots ts "
            "INNER JOIN devices d ON d.device_id = ts.device_id "
            f"WHERE d.company_id = '{COMPANY_A_ID}' "
            "ORDER BY ts.collected_at DESC"
        ),
        company_id=COMPANY_A_ID,
        thread_id=thread_id,
        natural_language_context="List fleet battery status",
    )

    assert len(result["raw_rows"]) >= 1
    assert result["tracing_metadata"]["company_id"] == COMPANY_A_ID
    assert "telemetry_snapshots" in {
        source["table"] for source in result["tracing_metadata"]["sources"]
    }

    from src.agent.tools import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    try:
        audit_rows = conn.execute(
            "SELECT action_type FROM audit_logs WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()

    assert any(row[0] == "query_execution" for row in audit_rows)


def test_remediation_ticket_proposal(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    thread_id = str(thread_config["configurable"]["thread_id"])
    staged: list[dict[str, Any]] = []

    result = open_remediation_ticket(
        device_id=patched_db["devices"]["compliance_fail"],
        check_id="screen_lock",
        note="Automated remediation for failing screen_lock.",
        company_id=COMPANY_A_ID,
        thread_id=thread_id,
        staged_actions=staged,
    )

    assert result["status"] == "pending_approval"
    assert result["action"] == {
        "action_type": "open_remediation_ticket",
        "device_id": patched_db["devices"]["compliance_fail"],
        "check_id": "screen_lock",
        "note": "Automated remediation for failing screen_lock.",
    }
    assert result["evidence"]["status"] == "fail"
    assert len(staged) == 1


def test_remediation_ticket_via_action_graph(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    graph = build_patched_graph(action_plan=True)
    graph.invoke(
        initial_state("Propose remediation tickets for compliance failures", COMPANY_B_ID),
        thread_config,
    )
    snapshot = graph_snapshot(graph, thread_config)
    proposed = snapshot.values.get("proposed_actions") or []

    remediation = [
        item
        for item in proposed
        if item.get("action", {}).get("action_type") == "open_remediation_ticket"
    ]
    assert len(remediation) >= 1
    assert remediation[0]["action"]["device_id"] == patched_db["devices"]["globex"]
    assert remediation[0]["evidence"]["check_id"] == "os_up_to_date"


def test_upgrade_order_for_low_disk(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    thread_id = str(thread_config["configurable"]["thread_id"])
    staged: list[dict[str, Any]] = []

    result = create_upgrade_order(
        device_id=patched_db["devices"]["low_disk"],
        component="disk",
        spec="1TB SSD",
        company_id=COMPANY_A_ID,
        thread_id=thread_id,
        staged_actions=staged,
    )

    assert result["action"]["action_type"] == "create_upgrade_order"
    assert result["evidence"]["disk_utilization_pct"] > 90.0

    graph = build_patched_graph(action_plan=True)
    graph.invoke(
        initial_state("Propose disk upgrade orders for storage constrained devices", COMPANY_A_ID),
        thread_config,
    )
    proposed = graph_snapshot(graph, thread_config).values.get("proposed_actions") or []
    upgrades = [
        item
        for item in proposed
        if item.get("action", {}).get("action_type") == "create_upgrade_order"
    ]
    assert any(
        item["action"]["device_id"] == patched_db["devices"]["low_disk"] for item in upgrades
    )


def test_memory_upgrade_proposal(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    graph = build_patched_graph(action_plan=True)
    graph.invoke(
        initial_state("Propose memory upgrade for RAM constrained devices", COMPANY_A_ID),
        thread_config,
    )
    proposed = graph_snapshot(graph, thread_config).values.get("proposed_actions") or []
    upgrades = [
        item
        for item in proposed
        if item.get("action", {}).get("action_type") == "create_upgrade_order"
        and item["action"].get("component") == "memory"
    ]
    assert len(upgrades) >= 1
    assert upgrades[0]["action"]["device_id"] == patched_db["devices"]["high_memory"]


def test_notify_employee_proposal(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    thread_id = str(thread_config["configurable"]["thread_id"])
    staged: list[dict[str, Any]] = []

    result = notify_employee(
        employee_id="EMP-A2",
        message="Fleet Copilot detected a device issue requiring your attention.",
        company_id=COMPANY_A_ID,
        thread_id=thread_id,
        staged_actions=staged,
    )

    assert result["action"]["action_type"] == "notify_employee"
    assert result["evidence"]["trigger"] == "battery_health_below_50"
    assert result["evidence"]["device_id"] == patched_db["devices"]["low_battery"]

    graph = build_patched_graph(action_plan=True)
    graph.invoke(
        initial_state("Notify employee about device health issues", COMPANY_A_ID),
        thread_config,
    )
    proposed = graph_snapshot(graph, thread_config).values.get("proposed_actions") or []
    notifications = [
        item
        for item in proposed
        if item.get("action", {}).get("action_type") == "notify_employee"
    ]
    assert len(notifications) >= 1


def test_action_proposed_audit_event(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    from src.agent.tools import DB_PATH

    thread_id = str(thread_config["configurable"]["thread_id"])
    open_remediation_ticket(
        device_id=patched_db["devices"]["compliance_fail"],
        check_id="screen_lock",
        note="Audit trail validation.",
        company_id=COMPANY_A_ID,
        thread_id=thread_id,
    )

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT action_type, details FROM audit_logs WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()

    assert any(row[0] == "action_proposed" for row in rows)


def test_remediation_ticket_refused_without_failure(
    patched_db: dict[str, Any], thread_config: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match=ACTION_REFUSAL_MESSAGE):
        open_remediation_ticket(
            device_id=patched_db["devices"]["healthy"],
            check_id="screen_lock",
            note="No failing check on this device.",
            company_id=COMPANY_A_ID,
            thread_id=str(thread_config["configurable"]["thread_id"]),
        )
