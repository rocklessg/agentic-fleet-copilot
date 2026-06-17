import json
import sqlite3
import uuid


def log_audit_event(
    conn: sqlite3.Connection,
    thread_id: str,
    company_id: str,
    actor: str,
    action_type: str,
    details_dict: dict,
) -> str:
    log_id = str(uuid.uuid4())
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO audit_logs (
                log_id, thread_id, company_id, actor, action_type, details
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                thread_id,
                company_id,
                actor,
                action_type,
                json.dumps(details_dict),
            ),
        )
        conn.commit()
    finally:
        cursor.close()
    return log_id
