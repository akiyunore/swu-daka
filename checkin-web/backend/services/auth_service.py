import secrets
import shutil
import re
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import Connection

from core.config import get_settings
from core.security import decrypt_email

_QUARANTINE_NAME_PATTERN = re.compile(r"\.deleted-[1-9][0-9]*-[0-9a-f]{12}\Z")


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def retry_pending_storage_cleanup(db: Connection) -> int:
    users_root = (get_settings().data_dir / "users").resolve()
    removed = 0
    rows = db.execute(
        "SELECT id, quarantine_name FROM storage_cleanup_queue ORDER BY id LIMIT 100"
    ).fetchall()
    for row in rows:
        name = row["quarantine_name"]
        if not _QUARANTINE_NAME_PATTERN.fullmatch(name):
            db.execute(
                "UPDATE storage_cleanup_queue SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ? WHERE id = ?",
                ("隔离目录名称校验失败", _utc_now_text(), row["id"]),
            )
            continue
        target = (users_root / name).resolve()
        if target.parent != users_root:
            continue
        try:
            if target.exists():
                shutil.rmtree(target)
        except OSError:
            db.execute(
                "UPDATE storage_cleanup_queue SET attempt_count = attempt_count + 1, last_error = ?, updated_at = ? WHERE id = ?",
                ("运行目录仍被占用", _utc_now_text(), row["id"]),
            )
        else:
            db.execute("DELETE FROM storage_cleanup_queue WHERE id = ?", (row["id"],))
            removed += 1
    db.commit()
    return removed


def mask_identifier(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    visible = local[:1]
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def _admin_user(row) -> dict:
    item = dict(row)
    item["username"] = mask_identifier(item.get("username"))
    item["student_no"] = mask_identifier(item.get("student_no"))
    item["school_username"] = mask_identifier(item.get("school_username"))
    encrypted_email = item.get("notification_email")
    item["notification_email_unreadable"] = False
    try:
        item["notification_email"] = mask_email(decrypt_email(encrypted_email)) if encrypted_email else None
    except (RuntimeError, ValueError):
        item["notification_email"] = None
        item["notification_email_unreadable"] = True
    return item


def list_users(db: Connection) -> list[dict]:
    rows = db.execute(
        """
        SELECT u.id, u.username, u.display_name, u.student_no, u.admin_note,
               u.has_agreed_terms, u.agreed_at, u.credential_verified_at,
               u.active, u.created_at, c.school_username,
               CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS has_credential,
               CASE WHEN u.password_hash <> '' THEN 1 ELSE 0 END AS login_enabled,
               COALESCE(s.enabled, 0) AS schedule_enabled,
               s.next_run_at, s.last_run_at, s.last_status, s.last_detail,
               u.invite_id, i.label AS invite_label,
               n.email AS notification_email,
               CASE WHEN n.email_verified_at IS NULL THEN 0 ELSE 1 END AS email_verified,
               COALESCE(n.notify_on_failure, 0) AS notify_on_failure
        FROM users u
        LEFT JOIN credentials c ON c.user_id = u.id
        LEFT JOIN auto_checkin_schedules s ON s.user_id = u.id
        LEFT JOIN invite_tokens i ON i.id = u.invite_id
        LEFT JOIN user_notification_settings n ON n.user_id = u.id
        ORDER BY u.id DESC
        """
    ).fetchall()
    return [_admin_user(row) for row in rows]


def get_user(db: Connection, user_id: int) -> dict:
    row = db.execute(
        """SELECT u.id, u.username, u.display_name, u.student_no, u.admin_note,
                  u.has_agreed_terms, u.agreed_at, u.credential_verified_at,
                  u.active, u.created_at, c.school_username,
                  CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS has_credential,
                  CASE WHEN u.password_hash <> '' THEN 1 ELSE 0 END AS login_enabled,
                  COALESCE(s.enabled, 0) AS schedule_enabled,
                  s.next_run_at, s.last_run_at, s.last_status, s.last_detail,
                  u.invite_id, i.label AS invite_label,
                  n.email AS notification_email,
                  CASE WHEN n.email_verified_at IS NULL THEN 0 ELSE 1 END AS email_verified,
                  COALESCE(n.notify_on_failure, 0) AS notify_on_failure
           FROM users u
           LEFT JOIN credentials c ON c.user_id = u.id
           LEFT JOIN auto_checkin_schedules s ON s.user_id = u.id
           LEFT JOIN invite_tokens i ON i.id = u.invite_id
           LEFT JOIN user_notification_settings n ON n.user_id = u.id
           WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if not row:
        raise ValueError("用户不存在")
    return _admin_user(row)


def set_user_active(db: Connection, user_id: int, active: bool) -> dict:
    cursor = db.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
    if cursor.rowcount == 0:
        raise ValueError("用户不存在")
    if not active:
        db.execute("UPDATE auto_checkin_schedules SET enabled = 0 WHERE user_id = ?", (user_id,))
        db.execute("UPDATE user_notification_settings SET notify_on_failure = 0 WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    db.commit()
    return get_user(db, user_id)


def reset_user_login(db: Connection, user_id: int) -> dict:
    cursor = db.execute("UPDATE users SET password_hash = '' WHERE id = ?", (user_id,))
    if cursor.rowcount == 0:
        raise ValueError("用户不存在")
    db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    db.commit()
    return get_user(db, user_id)


def update_user_note(db: Connection, user_id: int, note: str | None) -> dict:
    normalized = note.strip() if note and note.strip() else None
    cursor = db.execute(
        "UPDATE users SET admin_note = ? WHERE id = ?",
        (normalized, user_id),
    )
    if cursor.rowcount == 0:
        raise ValueError("用户不存在")
    db.commit()
    return get_user(db, user_id)


def delete_user(db: Connection, user_id: int) -> dict:
    from services.checkin_service import is_user_run_active, user_lifecycle_guard

    quarantine: Path | None = None
    with user_lifecycle_guard():
        row = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ValueError("用户不存在")
        if is_user_run_active(user_id):
            raise RuntimeError("用户任务正在执行，暂时不能删除")

        users_root = (get_settings().data_dir / "users").resolve()
        runtime_dir = (users_root / str(user_id)).resolve()
        if runtime_dir.parent != users_root:
            raise RuntimeError("用户运行目录校验失败")
        if runtime_dir.exists():
            users_root.mkdir(parents=True, exist_ok=True)
            quarantine = users_root / f".deleted-{user_id}-{secrets.token_hex(6)}"
            try:
                runtime_dir.rename(quarantine)
            except OSError as exc:
                raise RuntimeError("用户运行数据正被占用，暂时不能删除") from exc
        try:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            if deleted.rowcount != 1:
                raise ValueError("用户不存在")
            db.commit()
        except Exception:
            db.rollback()
            if quarantine and quarantine.exists() and not runtime_dir.exists():
                quarantine.rename(runtime_dir)
            raise

    cleanup_pending = False
    if quarantine:
        try:
            shutil.rmtree(quarantine)
        except OSError:
            cleanup_pending = True
            now = _utc_now_text()
            db.execute(
                """INSERT INTO storage_cleanup_queue
                   (user_id, quarantine_name, attempt_count, last_error, created_at, updated_at)
                   VALUES (?, ?, 1, '运行目录仍被占用', ?, ?)
                   ON CONFLICT(quarantine_name) DO UPDATE SET
                       attempt_count = storage_cleanup_queue.attempt_count + 1,
                       last_error = excluded.last_error,
                       updated_at = excluded.updated_at""",
                (user_id, quarantine.name, now, now),
            )
            db.commit()
    return {"deleted": True, "user_id": user_id, "storage_cleanup_pending": cleanup_pending}
