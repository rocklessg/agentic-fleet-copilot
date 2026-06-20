import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "data" / "telemetry.db"
NDJSON_PATH = PROJECT_ROOT / "data" / "device-telemetry-dataset.ndjson"
BATCH_SIZE = 100

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    serial_number TEXT,
    model_name TEXT,
    model_identifier TEXT,
    processor TEXT,
    hardware_uuid TEXT,
    total_memory TEXT
);

CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    battery_full_charge_capacity INTEGER,
    FOREIGN KEY (device_id) REFERENCES devices (device_id),
    UNIQUE (device_id, collected_at)
);

CREATE TABLE IF NOT EXISTS compliance_checks (
    compliance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    check_id TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES telemetry_snapshots (snapshot_id),
    UNIQUE (snapshot_id, check_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_device_collected
    ON telemetry_snapshots (device_id, collected_at);

CREATE INDEX IF NOT EXISTS idx_snapshots_company_device
    ON telemetry_snapshots (device_id);

CREATE INDEX IF NOT EXISTS idx_compliance_device_check
    ON compliance_checks (device_id, check_id, status);

CREATE INDEX IF NOT EXISTS idx_compliance_snapshot
    ON compliance_checks (snapshot_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id TEXT PRIMARY KEY,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    thread_id TEXT,
    company_id TEXT,
    actor TEXT,
    action_type TEXT,
    details TEXT
);
"""

UPSERT_DEVICE_SQL = """
INSERT INTO devices (
    device_id,
    company_id,
    employee_id,
    serial_number,
    model_name,
    model_identifier,
    processor,
    hardware_uuid,
    total_memory
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(device_id) DO UPDATE SET
    company_id = excluded.company_id,
    employee_id = excluded.employee_id,
    serial_number = excluded.serial_number,
    model_name = excluded.model_name,
    model_identifier = excluded.model_identifier,
    processor = excluded.processor,
    hardware_uuid = excluded.hardware_uuid,
    total_memory = excluded.total_memory;
"""

INSERT_SNAPSHOT_SQL = """
INSERT INTO telemetry_snapshots (
    device_id,
    collected_at,
    agent_version,
    os_platform,
    os_product_name,
    os_product_version,
    os_build_version,
    os_architecture,
    os_kernel_name,
    os_kernel_release,
    os_hostname,
    ram_bytes,
    total_memory_bytes,
    used_memory_bytes,
    free_memory_bytes,
    page_size_bytes,
    disk_volume_name,
    disk_file_system,
    disk_mount_point,
    disk_size_bytes,
    disk_available_bytes,
    disk_encrypted,
    battery_present,
    battery_charging_status,
    battery_percentage,
    battery_condition,
    battery_cycle_count,
    battery_full_charge_capacity
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(device_id, collected_at) DO UPDATE SET
    agent_version = excluded.agent_version,
    os_platform = excluded.os_platform,
    os_product_name = excluded.os_product_name,
    os_product_version = excluded.os_product_version,
    os_build_version = excluded.os_build_version,
    os_architecture = excluded.os_architecture,
    os_kernel_name = excluded.os_kernel_name,
    os_kernel_release = excluded.os_kernel_release,
    os_hostname = excluded.os_hostname,
    ram_bytes = excluded.ram_bytes,
    total_memory_bytes = excluded.total_memory_bytes,
    used_memory_bytes = excluded.used_memory_bytes,
    free_memory_bytes = excluded.free_memory_bytes,
    page_size_bytes = excluded.page_size_bytes,
    disk_volume_name = excluded.disk_volume_name,
    disk_file_system = excluded.disk_file_system,
    disk_mount_point = excluded.disk_mount_point,
    disk_size_bytes = excluded.disk_size_bytes,
    disk_available_bytes = excluded.disk_available_bytes,
    disk_encrypted = excluded.disk_encrypted,
    battery_present = excluded.battery_present,
    battery_charging_status = excluded.battery_charging_status,
    battery_percentage = excluded.battery_percentage,
    battery_condition = excluded.battery_condition,
    battery_cycle_count = excluded.battery_cycle_count,
    battery_full_charge_capacity = excluded.battery_full_charge_capacity;
"""

INSERT_COMPLIANCE_SQL = """
INSERT INTO compliance_checks (
    snapshot_id,
    device_id,
    collected_at,
    check_id,
    status,
    severity
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(snapshot_id, check_id) DO UPDATE SET
    status = excluded.status,
    severity = excluded.severity;
"""

SELECT_SNAPSHOT_ID_SQL = """
SELECT snapshot_id
FROM telemetry_snapshots
WHERE device_id = ? AND collected_at = ?;
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _primary_disk_volume(record: dict) -> dict:
    volumes = record.get("disk_volumes") or []
    return volumes[0] if volumes else {}


def extract_device_row(record: dict) -> tuple:
    identity = record.get("device_identity") or {}
    return (
        record["device_id"],
        record["company_id"],
        record["employee_id"],
        identity.get("serial_number"),
        identity.get("model_name"),
        identity.get("model_identifier"),
        identity.get("processor"),
        identity.get("hardware_uuid"),
        identity.get("total_memory"),
    )


def extract_snapshot_row(record: dict) -> tuple:
    os_info = record.get("os") or {}
    memory = record.get("memory") or {}
    disk = _primary_disk_volume(record)
    battery = record.get("battery") or {}

    return (
        record["device_id"],
        record["collected_at"],
        record.get("agent_version"),
        os_info.get("platform"),
        os_info.get("product_name"),
        os_info.get("product_version"),
        os_info.get("build_version"),
        os_info.get("architecture"),
        os_info.get("kernel_name"),
        os_info.get("kernel_release"),
        os_info.get("hostname"),
        memory.get("ram_bytes"),
        memory.get("total_memory_bytes"),
        memory.get("used_memory_bytes"),
        memory.get("free_memory_bytes"),
        memory.get("page_size_bytes"),
        disk.get("volume_name"),
        disk.get("file_system"),
        disk.get("mount_point"),
        disk.get("size_bytes"),
        disk.get("available_bytes"),
        int(disk.get("encrypted", False)),
        int(battery.get("battery_present", False)),
        battery.get("charging_status"),
        battery.get("percentage"),
        battery.get("condition"),
        battery.get("cycle_count"),
        battery.get("full_charge_capacity"),
    )


def extract_compliance_rows(
    record: dict, snapshot_id: int
) -> list[tuple[int, str, str, str, str, str]]:
    rows = []
    for check in record.get("compliance_results") or []:
        rows.append(
            (
                snapshot_id,
                record["device_id"],
                record["collected_at"],
                check["check_id"],
                check["status"],
                check["severity"],
            )
        )
    return rows


def upsert_record(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(UPSERT_DEVICE_SQL, extract_device_row(record))
    conn.execute(INSERT_SNAPSHOT_SQL, extract_snapshot_row(record))

    snapshot_row = conn.execute(
        SELECT_SNAPSHOT_ID_SQL,
        (record["device_id"], record["collected_at"]),
    ).fetchone()
    if snapshot_row is None:
        raise RuntimeError(
            f"Snapshot not found after insert: {record['device_id']} @ {record['collected_at']}"
        )

    snapshot_id = snapshot_row[0]
    compliance_rows = extract_compliance_rows(record, snapshot_id)
    if compliance_rows:
        conn.executemany(INSERT_COMPLIANCE_SQL, compliance_rows)


def ingest_ndjson(
    conn: sqlite3.Connection,
    ndjson_path: Path,
    batch_size: int = BATCH_SIZE,
) -> int:
    batch: list[dict] = []
    total_records = 0

    with ndjson_path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                batch.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}") from exc

            if len(batch) >= batch_size:
                total_records += _flush_batch(conn, batch)
                batch.clear()

    if batch:
        total_records += _flush_batch(conn, batch)

    return total_records


def _flush_batch(conn: sqlite3.Connection, batch: list[dict]) -> int:
    conn.execute("BEGIN")
    try:
        for record in batch:
            upsert_record(conn, record)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(batch)


def main() -> None:
    if not NDJSON_PATH.exists():
        raise FileNotFoundError(
            f"Dataset not found: {NDJSON_PATH}. "
            "Run `python scripts/bootstrap.py` to download and ingest telemetry data."
        )

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        create_schema(conn)
        record_count = ingest_ndjson(conn, NDJSON_PATH)

        device_count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry_snapshots"
        ).fetchone()[0]
        compliance_count = conn.execute(
            "SELECT COUNT(*) FROM compliance_checks"
        ).fetchone()[0]
        audit_log_count = conn.execute(
            "SELECT COUNT(*) FROM audit_logs"
        ).fetchone()[0]

    print(f"Ingested {record_count} telemetry records into {DB_PATH}")
    print(f"  devices: {device_count}")
    print(f"  telemetry_snapshots: {snapshot_count}")
    print(f"  compliance_checks: {compliance_count}")
    print(f"  audit_logs: {audit_log_count} (table ready)")


if __name__ == "__main__":
    main()
