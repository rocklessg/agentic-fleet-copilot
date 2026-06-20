import sqlite3
from typing import Any

from src.agent.tools import DB_PATH


def _disk_utilization_pct(size_bytes: int, available_bytes: int) -> float:
    if size_bytes <= 0:
        return 0.0
    return ((size_bytes - available_bytes) / size_bytes) * 100.0


def _memory_utilization_pct(total_bytes: int, used_bytes: int) -> float:
    if total_bytes <= 0:
        return 0.0
    return (used_bytes / total_bytes) * 100.0


def _build_insight(
    insight_type: str,
    device_id: str,
    finding: str,
    explanation: str,
    evidence: dict[str, Any],
    citation: str,
) -> dict[str, Any]:
    return {
        "insight_type": insight_type,
        "device_id": device_id,
        "finding": finding,
        "explanation": explanation,
        "evidence": evidence,
        "citation": citation,
    }


def detect_battery_decline_insights(
    conn: sqlite3.Connection, company_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ts.device_id, ts.snapshot_id, ts.collected_at, ts.battery_percentage
        FROM telemetry_snapshots ts
        INNER JOIN devices d ON d.device_id = ts.device_id
        WHERE d.company_id = ?
          AND ts.battery_present = 1
          AND ts.battery_percentage IS NOT NULL
        ORDER BY ts.device_id, ts.collected_at ASC
        """,
        (company_id,),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["device_id"], []).append(row)

    insights: list[dict[str, Any]] = []
    for device_id, snapshots in grouped.items():
        if len(snapshots) < 2:
            continue
        oldest, latest = snapshots[0], snapshots[-1]
        delta = oldest["battery_percentage"] - latest["battery_percentage"]
        if delta < 15:
            continue
        insights.append(
            _build_insight(
                insight_type="battery_decline",
                device_id=device_id,
                finding=(
                    f"Battery health declined from {oldest['battery_percentage']}% "
                    f"to {latest['battery_percentage']}% between "
                    f"{oldest['collected_at']} and {latest['collected_at']}."
                ),
                explanation=(
                    "Sustained battery degradation may indicate end-of-life "
                    "and replacement planning should be considered."
                ),
                evidence={
                    "oldest_battery_pct": oldest["battery_percentage"],
                    "latest_battery_pct": latest["battery_percentage"],
                    "delta_pct": delta,
                    "oldest_collected_at": oldest["collected_at"],
                    "latest_collected_at": latest["collected_at"],
                },
                citation=f"telemetry_snapshots/{latest['snapshot_id']}",
            )
        )
    return insights


def detect_storage_pressure_insights(
    conn: sqlite3.Connection, company_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ts.device_id, ts.snapshot_id, ts.collected_at,
               ts.disk_size_bytes, ts.disk_available_bytes
        FROM telemetry_snapshots ts
        INNER JOIN devices d ON d.device_id = ts.device_id
        WHERE d.company_id = ?
          AND ts.disk_size_bytes > 0
        ORDER BY ts.device_id, ts.collected_at ASC
        """,
        (company_id,),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["device_id"], []).append(row)

    insights: list[dict[str, Any]] = []
    for device_id, snapshots in grouped.items():
        if len(snapshots) < 2:
            continue
        oldest, latest = snapshots[0], snapshots[-1]
        oldest_util = _disk_utilization_pct(
            oldest["disk_size_bytes"], oldest["disk_available_bytes"]
        )
        latest_util = _disk_utilization_pct(
            latest["disk_size_bytes"], latest["disk_available_bytes"]
        )
        delta = latest_util - oldest_util
        if delta < 10.0 and latest_util < 85.0:
            continue
        insights.append(
            _build_insight(
                insight_type="storage_pressure",
                device_id=device_id,
                finding=(
                    f"Disk utilization rose from {oldest_util:.1f}% to {latest_util:.1f}% "
                    f"between {oldest['collected_at']} and {latest['collected_at']}."
                ),
                explanation=(
                    "Storage pressure is increasing over time and may require "
                    "cleanup or a disk upgrade if the trend continues."
                ),
                evidence={
                    "oldest_utilization_pct": round(oldest_util, 2),
                    "latest_utilization_pct": round(latest_util, 2),
                    "delta_pct": round(delta, 2),
                    "oldest_collected_at": oldest["collected_at"],
                    "latest_collected_at": latest["collected_at"],
                },
                citation=f"telemetry_snapshots/{latest['snapshot_id']}",
            )
        )
    return insights


def detect_memory_pressure_insights(
    conn: sqlite3.Connection, company_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ts.device_id, ts.snapshot_id, ts.collected_at,
               ts.total_memory_bytes, ts.used_memory_bytes
        FROM telemetry_snapshots ts
        INNER JOIN devices d ON d.device_id = ts.device_id
        WHERE d.company_id = ?
          AND ts.total_memory_bytes > 0
        ORDER BY ts.device_id, ts.collected_at ASC
        """,
        (company_id,),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["device_id"], []).append(row)

    insights: list[dict[str, Any]] = []
    for device_id, snapshots in grouped.items():
        if len(snapshots) < 2:
            continue
        oldest, latest = snapshots[0], snapshots[-1]
        oldest_util = _memory_utilization_pct(
            oldest["total_memory_bytes"], oldest["used_memory_bytes"]
        )
        latest_util = _memory_utilization_pct(
            latest["total_memory_bytes"], latest["used_memory_bytes"]
        )
        delta = latest_util - oldest_util
        if delta < 10.0 and latest_util < 85.0:
            continue
        insights.append(
            _build_insight(
                insight_type="memory_pressure",
                device_id=device_id,
                finding=(
                    f"Memory utilization rose from {oldest_util:.1f}% to {latest_util:.1f}% "
                    f"between {oldest['collected_at']} and {latest['collected_at']}."
                ),
                explanation=(
                    "The device is becoming increasingly memory-constrained, "
                    "which can affect performance and stability."
                ),
                evidence={
                    "oldest_utilization_pct": round(oldest_util, 2),
                    "latest_utilization_pct": round(latest_util, 2),
                    "delta_pct": round(delta, 2),
                    "oldest_collected_at": oldest["collected_at"],
                    "latest_collected_at": latest["collected_at"],
                },
                citation=f"telemetry_snapshots/{latest['snapshot_id']}",
            )
        )
    return insights


def detect_compliance_drift_insights(
    conn: sqlite3.Connection, company_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT cc.compliance_id, cc.device_id, cc.check_id, cc.status,
               cc.severity, cc.collected_at
        FROM compliance_checks cc
        INNER JOIN devices d ON d.device_id = cc.device_id
        WHERE d.company_id = ?
        ORDER BY cc.device_id, cc.check_id, cc.collected_at ASC
        """,
        (company_id,),
    ).fetchall()

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (row["device_id"], row["check_id"])
        grouped.setdefault(key, []).append(row)

    insights: list[dict[str, Any]] = []
    for (device_id, check_id), checks in grouped.items():
        if len(checks) < 2:
            continue
        prior, latest = checks[-2], checks[-1]
        if prior["status"] == latest["status"]:
            continue
        if not (prior["status"] == "pass" and latest["status"] == "fail"):
            continue
        insights.append(
            _build_insight(
                insight_type="compliance_drift",
                device_id=device_id,
                finding=(
                    f"Compliance check '{check_id}' drifted from pass to fail "
                    f"between {prior['collected_at']} and {latest['collected_at']}."
                ),
                explanation=(
                    "A previously compliant control has degraded and requires "
                    "remediation to restore fleet security posture."
                ),
                evidence={
                    "check_id": check_id,
                    "prior_status": prior["status"],
                    "latest_status": latest["status"],
                    "severity": latest["severity"],
                    "prior_collected_at": prior["collected_at"],
                    "latest_collected_at": latest["collected_at"],
                },
                citation=f"compliance_checks/{latest['compliance_id']}",
            )
        )
    return insights


def detect_fleet_insights(company_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        insights: list[dict[str, Any]] = []
        insights.extend(detect_battery_decline_insights(conn, company_id))
        insights.extend(detect_storage_pressure_insights(conn, company_id))
        insights.extend(detect_memory_pressure_insights(conn, company_id))
        insights.extend(detect_compliance_drift_insights(conn, company_id))
        return insights
    finally:
        conn.close()


def format_insights_markdown(insights: list[dict[str, Any]]) -> str:
    if not insights:
        return ""

    lines = ["### Fleet insights"]
    for item in insights:
        lines.append(
            f"- **{item['insight_type'].replace('_', ' ').title()}** on `{item['device_id']}`: "
            f"{item['finding']} {item['explanation']} "
            f"[Source: {item['citation']}]."
        )
    return "\n".join(lines)
