from evals.helpers import (
    COMPANY_A_ID,
    DEVICE_DRIFT,
    DEVICE_TREND_BATTERY,
    DEVICE_TREND_DISK,
    DEVICE_TREND_MEMORY,
)
from src.agent import tools as tools_module
from src.agent.insights import (
    detect_battery_decline_insights,
    detect_compliance_drift_insights,
    detect_fleet_insights,
    detect_memory_pressure_insights,
    detect_storage_pressure_insights,
    format_insights_markdown,
)
import sqlite3


def test_battery_decline_insight_detection(patched_db: dict) -> None:
    conn = sqlite3.connect(tools_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        insights = detect_battery_decline_insights(conn, COMPANY_A_ID)
    finally:
        conn.close()

    battery_insights = [
        item for item in insights if item["device_id"] == DEVICE_TREND_BATTERY
    ]
    assert len(battery_insights) == 1
    assert battery_insights[0]["insight_type"] == "battery_decline"
    assert battery_insights[0]["evidence"]["delta_pct"] == 42
    assert "finding" in battery_insights[0]
    assert "explanation" in battery_insights[0]
    assert battery_insights[0]["citation"].startswith("telemetry_snapshots/")


def test_storage_pressure_insight_detection(patched_db: dict) -> None:
    conn = sqlite3.connect(tools_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        insights = detect_storage_pressure_insights(conn, COMPANY_A_ID)
    finally:
        conn.close()

    disk_insights = [item for item in insights if item["device_id"] == DEVICE_TREND_DISK]
    assert len(disk_insights) == 1
    assert disk_insights[0]["insight_type"] == "storage_pressure"
    assert disk_insights[0]["evidence"]["latest_utilization_pct"] >= 85.0


def test_memory_pressure_insight_detection(patched_db: dict) -> None:
    conn = sqlite3.connect(tools_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        insights = detect_memory_pressure_insights(conn, COMPANY_A_ID)
    finally:
        conn.close()

    memory_insights = [
        item for item in insights if item["device_id"] == DEVICE_TREND_MEMORY
    ]
    assert len(memory_insights) == 1
    assert memory_insights[0]["insight_type"] == "memory_pressure"
    assert memory_insights[0]["evidence"]["delta_pct"] >= 10.0


def test_compliance_drift_insight_detection(patched_db: dict) -> None:
    conn = sqlite3.connect(tools_module.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        insights = detect_compliance_drift_insights(conn, COMPANY_A_ID)
    finally:
        conn.close()

    drift_insights = [item for item in insights if item["device_id"] == DEVICE_DRIFT]
    assert len(drift_insights) == 1
    assert drift_insights[0]["insight_type"] == "compliance_drift"
    assert drift_insights[0]["evidence"]["prior_status"] == "pass"
    assert drift_insights[0]["evidence"]["latest_status"] == "fail"
    assert drift_insights[0]["citation"].startswith("compliance_checks/")


def test_detect_fleet_insights_aggregate(patched_db: dict) -> None:
    insights = detect_fleet_insights(COMPANY_A_ID)
    insight_types = {item["insight_type"] for item in insights}
    assert "battery_decline" in insight_types
    assert "storage_pressure" in insight_types
    assert "memory_pressure" in insight_types
    assert "compliance_drift" in insight_types

    markdown = format_insights_markdown(insights)
    assert "### Fleet insights" in markdown
    assert "[Source:" in markdown
