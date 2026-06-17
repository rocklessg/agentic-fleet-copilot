import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from src.utils.logger import log_audit_event

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "telemetry.db"

FORBIDDEN_SQL_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "attach",
    "detach",
    "replace",
    "truncate",
    "pragma",
    "vacuum",
)

TABLE_MANIFEST: dict[str, dict[str, Any]] = {
    "devices": {
        "primary_key": "device_id",
        "timestamp_fields": [],
    },
    "telemetry_snapshots": {
        "primary_key": "snapshot_id",
        "timestamp_fields": ["collected_at"],
    },
    "compliance_checks": {
        "primary_key": "compliance_id",
        "timestamp_fields": ["collected_at"],
    },
}

UPGRADE_COMPONENTS = frozenset({"disk", "storage", "memory", "ram", "battery"})
REMEDIATION_CHECKS = frozenset({"disk_encryption", "screen_lock", "os_up_to_date"})


class SecurityError(Exception):
    pass


class UpgradeOrderAction(BaseModel):
    action_type: Literal["create_upgrade_order"] = "create_upgrade_order"
    device_id: str = Field(min_length=1)
    component: str = Field(min_length=1)
    spec: str = Field(min_length=1)

    @field_validator("component")
    @classmethod
    def normalize_component(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in UPGRADE_COMPONENTS:
            raise ValueError(
                f"component must be one of: {', '.join(sorted(UPGRADE_COMPONENTS))}"
            )
        return normalized


class RemediationTicketAction(BaseModel):
    action_type: Literal["open_remediation_ticket"] = "open_remediation_ticket"
    device_id: str = Field(min_length=1)
    check_id: str = Field(min_length=1)
    note: str = Field(min_length=1)

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in REMEDIATION_CHECKS:
            raise ValueError(
                f"check_id must be one of: {', '.join(sorted(REMEDIATION_CHECKS))}"
            )
        return normalized


class DeviceReplacementAction(BaseModel):
    action_type: Literal["flag_device_for_replacement"] = "flag_device_for_replacement"
    device_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EmployeeNotificationAction(BaseModel):
    action_type: Literal["notify_employee"] = "notify_employee"
    employee_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_readonly_db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_sql(sql_query: str) -> str:
    without_line_comments = re.sub(r"--[^\n]*", " ", sql_query)
    without_block_comments = re.sub(r"/\*.*?\*/", " ", without_line_comments, flags=re.DOTALL)
    return " ".join(without_block_comments.lower().split())


def _extract_tables(sql_query: str) -> list[str]:
    normalized = _normalize_sql(sql_query)
    tables = re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_]*)", normalized)
    seen: list[str] = []
    for table in tables:
        if table in TABLE_MANIFEST and table not in seen:
            seen.append(table)
    return seen


def _validate_tenant_scope(sql_query: str, company_id: str) -> None:
    normalized = _normalize_sql(sql_query)

    if not (normalized.startswith("select") or normalized.startswith("with")):
        raise SecurityError("Only read-only SELECT queries are permitted.")

    for keyword in FORBIDDEN_SQL_KEYWORDS:
        if re.search(rf"\b{keyword}\b", normalized):
            raise SecurityError(f"Forbidden SQL keyword detected: {keyword}")

    if " where " not in f" {normalized} ":
        raise SecurityError("Query must include a WHERE clause scoped to company_id.")

    company_pattern = re.compile(
        rf"(?:\w+\.)?company_id\s*=\s*(['\"]){re.escape(company_id.lower())}\1"
    )
    if not company_pattern.search(normalized):
        raise SecurityError(
            "Query must filter on company_id matching the active tenant."
        )

    if re.search(r"(?:\w+\.)?company_id\s*(?:!=|<>)", normalized):
        raise SecurityError("Query must not negate the company_id tenant filter.")

    foreign_company_literals = re.findall(
        r"(?:\w+\.)?company_id\s*=\s*['\"]([^'\"]+)['\"]", normalized
    )
    if any(value != company_id.lower() for value in foreign_company_literals):
        raise SecurityError("Query attempts to access data outside the active tenant.")


def _build_tracing_metadata(
    sql_query: str,
    rows: list[sqlite3.Row],
    company_id: str,
) -> dict[str, Any]:
    tables = _extract_tables(sql_query)
    primary_keys_observed: dict[str, list[Any]] = {}

    for table_name in tables:
        pk_field = TABLE_MANIFEST[table_name]["primary_key"]
        if rows and pk_field in rows[0].keys():
            primary_keys_observed[table_name] = [
                row[pk_field] for row in rows if pk_field in row.keys()
            ]

    return {
        "company_id": company_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {
                "table": table_name,
                "primary_key": TABLE_MANIFEST[table_name]["primary_key"],
                "timestamp_fields": TABLE_MANIFEST[table_name]["timestamp_fields"],
                "primary_keys_observed": primary_keys_observed.get(table_name, []),
                "rows_evaluated": len(rows),
            }
            for table_name in tables
        ],
    }


def _log_security_violation(
    conn: sqlite3.Connection,
    thread_id: str,
    company_id: str,
    details: dict[str, Any],
) -> None:
    log_audit_event(
        conn,
        thread_id=thread_id,
        company_id=company_id,
        actor="agent",
        action_type="security_violation",
        details_dict=details,
    )


def execute_fleet_query(
    sql_query: str,
    company_id: str,
    thread_id: str,
    natural_language_context: str = "",
) -> dict[str, Any]:
    audit_conn = _connect_db()
    try:
        try:
            _validate_tenant_scope(sql_query, company_id)
        except SecurityError as exc:
            _log_security_violation(
                audit_conn,
                thread_id,
                company_id,
                {
                    "generated_sql": sql_query,
                    "natural_language_context": natural_language_context,
                    "reason": str(exc),
                },
            )
            raise

        with _connect_readonly_db() as read_conn:
            cursor = read_conn.execute(sql_query)
            rows = cursor.fetchall()

        raw_rows = [dict(row) for row in rows]
        tracing_metadata = _build_tracing_metadata(sql_query, rows, company_id)

        log_audit_event(
            audit_conn,
            thread_id=thread_id,
            company_id=company_id,
            actor="agent",
            action_type="query_execution",
            details_dict={
                "natural_language_context": natural_language_context,
                "generated_sql": sql_query,
                "row_count": len(raw_rows),
                "tracing_metadata": tracing_metadata,
            },
        )

        return {
            "raw_rows": raw_rows,
            "tracing_metadata": tracing_metadata,
        }
    finally:
        audit_conn.close()


def _get_device_company(conn: sqlite3.Connection, device_id: str) -> str | None:
    row = conn.execute(
        "SELECT company_id FROM devices WHERE device_id = ?",
        (device_id,),
    ).fetchone()
    return row["company_id"] if row else None


def _assert_device_in_tenant(
    conn: sqlite3.Connection, device_id: str, company_id: str
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT d.device_id, d.company_id, d.employee_id
        FROM devices d
        WHERE d.device_id = ? AND d.company_id = ?
        """,
        (device_id, company_id),
    ).fetchone()
    if row is None:
        raise ValueError("Action Refused: Insufficient telemetry evidence.")
    return row


def _get_latest_snapshot(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM telemetry_snapshots
        WHERE device_id = ?
        ORDER BY collected_at DESC
        LIMIT 1
        """,
        (device_id,),
    ).fetchone()


def _disk_utilization_pct(snapshot: sqlite3.Row) -> float:
    size_bytes = snapshot["disk_size_bytes"] or 0
    available_bytes = snapshot["disk_available_bytes"] or 0
    if size_bytes <= 0:
        return 0.0
    used_bytes = size_bytes - available_bytes
    return (used_bytes / size_bytes) * 100.0


def _memory_utilization_pct(snapshot: sqlite3.Row) -> float:
    total_bytes = snapshot["total_memory_bytes"] or 0
    used_bytes = snapshot["used_memory_bytes"] or 0
    if total_bytes <= 0:
        return 0.0
    return (used_bytes / total_bytes) * 100.0


def _stage_action(
    conn: sqlite3.Connection,
    thread_id: str,
    company_id: str,
    action_payload: dict[str, Any],
    evidence: dict[str, Any],
    staged_actions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    staged = {
        "status": "pending_approval",
        "proposed_at": datetime.now(timezone.utc).isoformat(),
        "action": action_payload,
        "evidence": evidence,
    }
    if staged_actions is not None:
        staged_actions.append(staged)

    log_audit_event(
        conn,
        thread_id=thread_id,
        company_id=company_id,
        actor="agent",
        action_type="action_proposed",
        details_dict={
            "action": action_payload,
            "evidence": evidence,
        },
    )
    return staged


def create_upgrade_order(
    device_id: str,
    component: str,
    spec: str,
    company_id: str,
    thread_id: str,
    staged_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = UpgradeOrderAction(device_id=device_id, component=component, spec=spec)
    conn = _connect_db()
    try:
        _assert_device_in_tenant(conn, action.device_id, company_id)
        snapshot = _get_latest_snapshot(conn, action.device_id)
        if snapshot is None:
            raise ValueError("Action Refused: Insufficient telemetry evidence.")

        evidence: dict[str, Any] = {
            "device_id": action.device_id,
            "collected_at": snapshot["collected_at"],
        }
        justified = False

        if action.component in {"disk", "storage"}:
            utilization = _disk_utilization_pct(snapshot)
            evidence["disk_utilization_pct"] = round(utilization, 2)
            justified = utilization > 90.0
        elif action.component in {"memory", "ram"}:
            utilization = _memory_utilization_pct(snapshot)
            evidence["memory_utilization_pct"] = round(utilization, 2)
            justified = utilization > 90.0
        elif action.component == "battery":
            evidence["battery_percentage"] = snapshot["battery_percentage"]
            evidence["battery_condition"] = snapshot["battery_condition"]
            justified = (
                snapshot["battery_present"] == 1
                and snapshot["battery_percentage"] is not None
                and snapshot["battery_percentage"] < 50
            )

        if not justified:
            raise ValueError("Action Refused: Insufficient telemetry evidence.")

        return _stage_action(
            conn,
            thread_id,
            company_id,
            action.model_dump(),
            evidence,
            staged_actions,
        )
    finally:
        conn.close()


def open_remediation_ticket(
    device_id: str,
    check_id: str,
    note: str,
    company_id: str,
    thread_id: str,
    staged_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = RemediationTicketAction(device_id=device_id, check_id=check_id, note=note)
    conn = _connect_db()
    try:
        _assert_device_in_tenant(conn, action.device_id, company_id)
        row = conn.execute(
            """
            SELECT cc.check_id, cc.status, cc.severity, cc.collected_at
            FROM compliance_checks cc
            INNER JOIN devices d ON d.device_id = cc.device_id
            WHERE cc.device_id = ?
              AND d.company_id = ?
              AND cc.check_id = ?
            ORDER BY cc.collected_at DESC
            LIMIT 1
            """,
            (action.device_id, company_id, action.check_id),
        ).fetchone()

        if row is None or row["status"] != "fail":
            raise ValueError("Action Refused: Insufficient telemetry evidence.")

        evidence = {
            "device_id": action.device_id,
            "check_id": row["check_id"],
            "status": row["status"],
            "severity": row["severity"],
            "collected_at": row["collected_at"],
        }
        return _stage_action(
            conn,
            thread_id,
            company_id,
            action.model_dump(),
            evidence,
            staged_actions,
        )
    finally:
        conn.close()


def flag_device_for_replacement(
    device_id: str,
    reason: str,
    company_id: str,
    thread_id: str,
    staged_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = DeviceReplacementAction(device_id=device_id, reason=reason)
    conn = _connect_db()
    try:
        _assert_device_in_tenant(conn, action.device_id, company_id)
        snapshot = _get_latest_snapshot(conn, action.device_id)
        if snapshot is None:
            raise ValueError("Action Refused: Insufficient telemetry evidence.")

        disk_util = _disk_utilization_pct(snapshot)
        battery_pct = snapshot["battery_percentage"]
        evidence = {
            "device_id": action.device_id,
            "collected_at": snapshot["collected_at"],
            "battery_percentage": battery_pct,
            "disk_utilization_pct": round(disk_util, 2),
        }

        battery_failing = (
            snapshot["battery_present"] == 1
            and battery_pct is not None
            and battery_pct < 50
        )
        disk_critical = disk_util > 90.0

        if not (battery_failing or disk_critical):
            raise ValueError("Action Refused: Insufficient telemetry evidence.")

        evidence["replacement_trigger"] = (
            "battery_health_below_50" if battery_failing else "disk_utilization_above_90"
        )
        return _stage_action(
            conn,
            thread_id,
            company_id,
            action.model_dump(),
            evidence,
            staged_actions,
        )
    finally:
        conn.close()


def notify_employee(
    employee_id: str,
    message: str,
    company_id: str,
    thread_id: str,
    staged_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    action = EmployeeNotificationAction(employee_id=employee_id, message=message)
    conn = _connect_db()
    try:
        devices = conn.execute(
            """
            SELECT d.device_id
            FROM devices d
            WHERE d.employee_id = ? AND d.company_id = ?
            """,
            (action.employee_id, company_id),
        ).fetchall()
        if not devices:
            raise ValueError("Action Refused: Insufficient telemetry evidence.")

        device_ids = [row["device_id"] for row in devices]
        placeholders = ",".join("?" for _ in device_ids)
        issue = conn.execute(
            f"""
            SELECT ts.device_id, ts.collected_at, ts.battery_percentage,
                   ts.disk_size_bytes, ts.disk_available_bytes,
                   ts.used_memory_bytes, ts.total_memory_bytes
            FROM telemetry_snapshots ts
            INNER JOIN (
                SELECT device_id, MAX(collected_at) AS collected_at
                FROM telemetry_snapshots
                WHERE device_id IN ({placeholders})
                GROUP BY device_id
            ) latest
              ON ts.device_id = latest.device_id
             AND ts.collected_at = latest.collected_at
            """,
            device_ids,
        ).fetchall()

        justified_device = None
        issue_evidence: dict[str, Any] = {}
        for snapshot in issue:
            if snapshot["battery_percentage"] is not None and snapshot["battery_percentage"] < 50:
                justified_device = snapshot["device_id"]
                issue_evidence = {
                    "trigger": "battery_health_below_50",
                    "battery_percentage": snapshot["battery_percentage"],
                    "collected_at": snapshot["collected_at"],
                }
                break
            if _disk_utilization_pct(snapshot) > 90.0:
                justified_device = snapshot["device_id"]
                issue_evidence = {
                    "trigger": "disk_utilization_above_90",
                    "disk_utilization_pct": round(_disk_utilization_pct(snapshot), 2),
                    "collected_at": snapshot["collected_at"],
                }
                break
            if _memory_utilization_pct(snapshot) > 90.0:
                justified_device = snapshot["device_id"]
                issue_evidence = {
                    "trigger": "memory_utilization_above_90",
                    "memory_utilization_pct": round(_memory_utilization_pct(snapshot), 2),
                    "collected_at": snapshot["collected_at"],
                }
                break

        if justified_device is None:
            failed_check = conn.execute(
                f"""
                SELECT cc.device_id, cc.check_id, cc.status, cc.collected_at
                FROM compliance_checks cc
                INNER JOIN (
                    SELECT device_id, check_id, MAX(collected_at) AS collected_at
                    FROM compliance_checks
                    WHERE device_id IN ({placeholders})
                    GROUP BY device_id, check_id
                ) latest
                  ON cc.device_id = latest.device_id
                 AND cc.check_id = latest.check_id
                 AND cc.collected_at = latest.collected_at
                WHERE cc.status = 'fail'
                LIMIT 1
                """,
                device_ids,
            ).fetchone()
            if failed_check is None:
                raise ValueError("Action Refused: Insufficient telemetry evidence.")
            justified_device = failed_check["device_id"]
            issue_evidence = {
                "trigger": "compliance_failure",
                "check_id": failed_check["check_id"],
                "status": failed_check["status"],
                "collected_at": failed_check["collected_at"],
            }

        evidence = {
            "employee_id": action.employee_id,
            "device_id": justified_device,
            **issue_evidence,
        }
        return _stage_action(
            conn,
            thread_id,
            company_id,
            action.model_dump(),
            evidence,
            staged_actions,
        )
    finally:
        conn.close()
