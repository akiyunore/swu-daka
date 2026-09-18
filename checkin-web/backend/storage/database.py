import sqlite3
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path

from core.security import decrypt_email, encrypt_email, hash_email, is_encrypted_email
from storage.models import SCHEMA_SQL

_SQL_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
logger = logging.getLogger(__name__)


def _sql_identifier(value: str) -> str:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError("数据库迁移标识符格式无效")
    return f'"{value}"'


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may resume a synchronous yield dependency on a different worker
    # thread for cleanup, so the request-scoped connection must be closable
    # from that thread. Each request still receives its own connection.
    conn = sqlite3.connect(database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-4096")
    return conn


def init_database(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        _migrate_users_columns(conn)
        _ensure_column(conn, "invite_tokens", "encrypted_token")
        _migrate_cloud_ocr_base_url(conn)
        _ensure_column(conn, "cloud_ocr_settings", "resolved_addresses")
        _migrate_auto_checkin_schedule_time(conn)
        _migrate_email_storage(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_invite ON users(invite_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_email_hash ON user_notification_settings(email_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_tokens_hash ON email_verification_tokens(email_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notification_recipient_hash ON notification_outbox(recipient_hash)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedules_due ON auto_checkin_schedules(enabled, next_run_at)"
        )
        conn.commit()


def _migrate_users_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "has_agreed_terms" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN has_agreed_terms INTEGER NOT NULL DEFAULT 0"
        )
    if "agreed_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN agreed_at TEXT")
    if "credential_verified_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN credential_verified_at TEXT")
    if "invite_id" not in columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN invite_id INTEGER REFERENCES invite_tokens(id) ON DELETE SET NULL"
        )
    if "active" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if "admin_note" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN admin_note TEXT")
        conn.execute(
            """UPDATE users SET admin_note = (
                   SELECT i.label FROM invite_tokens i WHERE i.id = users.invite_id
               )
               WHERE admin_note IS NULL AND invite_id IS NOT NULL"""
        )


def _migrate_auto_checkin_schedule_time(conn: sqlite3.Connection) -> None:
    """Move schedules that still use the former Web default from 21:00 to 21:10."""
    rows = conn.execute(
        "SELECT user_id, next_run_at FROM auto_checkin_schedules WHERE schedule_time = '21:00'"
    ).fetchall()
    for row in rows:
        next_run_at = row["next_run_at"]
        shifted_next_run = next_run_at
        if next_run_at:
            try:
                shifted_next_run = (
                    datetime.fromisoformat(next_run_at) + timedelta(minutes=10)
                ).isoformat(timespec="seconds")
            except ValueError:
                # Keep an unexpected legacy value intact; the scheduler can still
                # surface it instead of silently discarding the persisted state.
                shifted_next_run = next_run_at
        conn.execute(
            """UPDATE auto_checkin_schedules
               SET schedule_time = '21:10', next_run_at = ?
               WHERE user_id = ? AND schedule_time = '21:00'""",
            (shifted_next_run, row["user_id"]),
        )


def _migrate_cloud_ocr_base_url(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(cloud_ocr_settings)").fetchall()
    }
    if "base_url" not in columns:
        conn.execute("ALTER TABLE cloud_ocr_settings ADD COLUMN base_url TEXT")
    conn.execute(
        """UPDATE cloud_ocr_settings
           SET base_url = CASE
               WHEN provider = 'openai' THEN 'https://api.openai.com/v1/chat/completions'
               ELSE 'https://api.xiaomimimo.com/v1/chat/completions'
           END
           WHERE base_url IS NULL OR TRIM(base_url) = ''"""
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str) -> None:
    table_sql = _sql_identifier(table)
    column_sql = _sql_identifier(column)
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_sql})").fetchall()  # nosec B608
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table_sql} ADD COLUMN {column_sql} TEXT")  # nosec B608


def _migrate_encrypted_email_column(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    value_column: str,
    hash_column: str,
) -> None:
    table_sql = _sql_identifier(table)
    id_sql = _sql_identifier(id_column)
    value_sql = _sql_identifier(value_column)
    hash_sql = _sql_identifier(hash_column)
    rows = conn.execute(
        f"SELECT {id_sql}, {value_sql}, {hash_sql} FROM {table_sql} WHERE {value_sql} IS NOT NULL"  # nosec B608
    ).fetchall()
    for row in rows:
        stored = row[value_column]
        try:
            plain = decrypt_email(stored) if is_encrypted_email(stored) else stored
        except Exception:
            now = datetime.now().astimezone().isoformat()
            conn.execute(
                """INSERT INTO migration_integrity_issues
                   (entity_table, entity_key, field_name, error_code, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, 'email-decrypt-failed', ?, ?)
                   ON CONFLICT(entity_table, entity_key, field_name) DO UPDATE SET
                       error_code = excluded.error_code,
                       last_seen_at = excluded.last_seen_at""",
                (table, str(row[id_column]), value_column, now, now),
            )
            logger.error(
                "Encrypted email migration quarantined unreadable value in %s.%s row %s",
                table,
                value_column,
                row[id_column],
            )
            continue
        expected_hash = hash_email(plain)
        encrypted = stored if is_encrypted_email(stored) else encrypt_email(plain)
        if encrypted != stored or row[hash_column] != expected_hash:
            conn.execute(
                f"UPDATE {table_sql} SET {value_sql} = ?, {hash_sql} = ? WHERE {id_sql} = ?",  # nosec B608
                (encrypted, expected_hash, row[id_column]),
            )
        conn.execute(
            "DELETE FROM migration_integrity_issues WHERE entity_table = ? AND entity_key = ? AND field_name = ?",
            (table, str(row[id_column]), value_column),
        )


def _migrate_email_storage(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "user_notification_settings", "email_hash")
    _ensure_column(conn, "email_verification_tokens", "email_hash")
    _ensure_column(conn, "notification_outbox", "recipient_hash")
    _migrate_encrypted_email_column(conn, "user_notification_settings", "user_id", "email", "email_hash")
    _migrate_encrypted_email_column(conn, "email_verification_tokens", "id", "email", "email_hash")
    _migrate_encrypted_email_column(conn, "notification_outbox", "id", "recipient", "recipient_hash")
