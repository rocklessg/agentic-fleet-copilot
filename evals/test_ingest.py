import json
import sqlite3
from pathlib import Path

from src.database.ingest import create_schema, ingest_ndjson


def test_ingest_ndjson_builds_expected_tables(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "sample.ndjson"
    db_path = tmp_path / "telemetry.db"

    records = [
        {
            "device_id": "INGEST-DEV-1",
            "company_id": "acme-001",
            "employee_id": "emp-ingest-1",
            "collected_at": "2026-02-01T10:00:00Z",
            "agent_version": "1.0.0",
            "os": {
                "platform": "darwin",
                "product_name": "macOS",
                "product_version": "15.0",
            },
            "device_identity": {
                "serial_number": "INGEST-DEV-1",
                "model_name": "MacBook Pro",
            },
            "memory": {
                "total_memory_bytes": 16_000_000_000,
                "used_memory_bytes": 8_000_000_000,
                "free_memory_bytes": 8_000_000_000,
            },
            "disk_volumes": [
                {
                    "volume_name": "Macintosh HD",
                    "size_bytes": 500_000_000_000,
                    "available_bytes": 250_000_000_000,
                    "encrypted": True,
                }
            ],
            "battery": {
                "battery_present": True,
                "percentage": 72,
                "condition": "Normal",
            },
            "compliance_results": [
                {"check_id": "disk_encryption", "status": "pass", "severity": "high"},
                {"check_id": "os_up_to_date", "status": "fail", "severity": "medium"},
            ],
        },
        {
            "device_id": "INGEST-DEV-1",
            "company_id": "acme-001",
            "employee_id": "emp-ingest-1",
            "collected_at": "2026-02-02T10:00:00Z",
            "agent_version": "1.0.0",
            "os": {"platform": "darwin", "product_name": "macOS", "product_version": "15.1"},
            "device_identity": {"serial_number": "INGEST-DEV-1", "model_name": "MacBook Pro"},
            "memory": {
                "total_memory_bytes": 16_000_000_000,
                "used_memory_bytes": 9_000_000_000,
                "free_memory_bytes": 7_000_000_000,
            },
            "disk_volumes": [
                {
                    "volume_name": "Macintosh HD",
                    "size_bytes": 500_000_000_000,
                    "available_bytes": 240_000_000_000,
                    "encrypted": True,
                }
            ],
            "battery": {"battery_present": True, "percentage": 65, "condition": "Normal"},
            "compliance_results": [
                {"check_id": "disk_encryption", "status": "pass", "severity": "high"},
                {"check_id": "os_up_to_date", "status": "pass", "severity": "medium"},
            ],
        },
    ]
    ndjson_path.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        create_schema(conn)
        count = ingest_ndjson(conn, ndjson_path, batch_size=1)
        assert count == 2

        device_count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM telemetry_snapshots"
        ).fetchone()[0]
        compliance_count = conn.execute(
            "SELECT COUNT(*) FROM compliance_checks"
        ).fetchone()[0]

        assert device_count == 1
        assert snapshot_count == 2
        assert compliance_count == 4

        latest_battery = conn.execute(
            """
            SELECT battery_percentage
            FROM telemetry_snapshots
            WHERE device_id = 'INGEST-DEV-1'
            ORDER BY collected_at DESC
            LIMIT 1
            """
        ).fetchone()[0]
        assert latest_battery == 65
    finally:
        conn.close()
