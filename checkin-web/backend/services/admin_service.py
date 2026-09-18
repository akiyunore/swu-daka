from datetime import datetime, timedelta, timezone
from functools import lru_cache
from sqlite3 import Connection

from core.config import get_settings
from core.security import (
    decrypt_invite_token,
    encrypt_invite_token,
    hash_token,
    hash_web_password,
    new_random_token,
    verify_web_password,
)
from services.auth_service import mask_identifier


def _now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache
def _dummy_password_hash() -> str:
    return hash_web_password("invalid-admin-password")


def bootstrap_admin(db: Connection) -> bool:
    settings = get_settings()
    if not settings.admin_username or not settings.admin_password:
        return False
    now = _now().isoformat()
    row = db.execute("SELECT id, password_hash FROM admin_users WHERE username = ?", (settings.admin_username,)).fetchone()
    if row:
        if not verify_web_password(settings.admin_password, row["password_hash"]):
            db.execute(
                "UPDATE admin_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_web_password(settings.admin_password), now, row["id"]),
            )
            db.execute("DELETE FROM admin_sessions WHERE admin_user_id = ?", (row["id"],))
            db.commit()
        return True
    db.execute(
        "INSERT INTO admin_users (username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (settings.admin_username, hash_web_password(settings.admin_password), now, now),
    )
    db.commit()
    return True


def login_admin(db: Connection, username: str, password: str) -> tuple[str, dict]:
    row = db.execute("SELECT id, username, password_hash FROM admin_users WHERE username = ?", (username,)).fetchone()
    candidate_hash = row["password_hash"] if row else _dummy_password_hash()
    if not verify_web_password(password, candidate_hash) or not row:
        raise ValueError("用户名或密码错误")
    token = new_random_token()
    csrf = new_random_token(24)
    now = _now()
    expires_at = now + timedelta(hours=get_settings().session_ttl_hours)
    db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now.isoformat(),))
    db.execute(
        "INSERT INTO admin_sessions (admin_user_id, token_hash, csrf_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (row["id"], hash_token(token), csrf, expires_at.isoformat(), now.isoformat()),
    )
    db.commit()
    return token, {"username": row["username"], "csrf_token": csrf, "expires_at": expires_at.isoformat()}


def create_invite(db: Connection, admin_id: int, label: str | None, expires_in_hours: int) -> dict:
    token = new_random_token()
    now = _now()
    expires_at = now + timedelta(hours=expires_in_hours)
    cursor = db.execute(
        """INSERT INTO invite_tokens
           (token_hash, encrypted_token, label, expires_at, max_uses, use_count,
            revoked_at, created_by_admin_id, created_at)
           VALUES (?, ?, ?, ?, 1, 0, NULL, ?, ?)""",
        (
            hash_token(token),
            encrypt_invite_token(token),
            label,
            expires_at.isoformat(),
            admin_id,
            now.isoformat(),
        ),
    )
    db.commit()
    return {
        "id": int(cursor.lastrowid),
        "token": token,
        "registration_path": "/?mode=register",
        "label": label,
        "expires_at": expires_at.isoformat(),
    }


def _invite_item(row) -> dict:
    item = dict(row)
    now = _now()
    if item["revoked_at"]:
        item["status"] = "revoked"
    elif item["use_count"] >= item["max_uses"]:
        item["status"] = "used"
    elif datetime.fromisoformat(item["expires_at"]) <= now:
        item["status"] = "expired"
    else:
        item["status"] = "active"
    item["token_revealable"] = int(
        item["status"] == "active" and bool(item.get("token_revealable"))
    )
    item["used_by_username"] = mask_identifier(item.get("used_by_username"))
    return item


def list_invites(db: Connection) -> list[dict]:
    rows = db.execute(
        """SELECT i.id, i.label, i.expires_at, i.max_uses, i.use_count,
                  i.revoked_at, i.created_at, a.username AS created_by,
                  CASE WHEN i.encrypted_token IS NULL THEN 0 ELSE 1 END AS token_revealable,
                  u.id AS used_by_user_id, u.display_name AS used_by_display_name,
                  u.username AS used_by_username
           FROM invite_tokens i
           JOIN admin_users a ON a.id = i.created_by_admin_id
           LEFT JOIN users u ON u.invite_id = i.id
           ORDER BY i.id DESC LIMIT 100"""
    ).fetchall()
    return [_invite_item(row) for row in rows]


def get_invite(db: Connection, invite_id: int) -> dict:
    row = db.execute(
        """SELECT i.id, i.label, i.expires_at, i.max_uses, i.use_count,
                  i.revoked_at, i.created_at, a.username AS created_by,
                  CASE WHEN i.encrypted_token IS NULL THEN 0 ELSE 1 END AS token_revealable,
                  u.id AS used_by_user_id, u.display_name AS used_by_display_name,
                  u.username AS used_by_username
           FROM invite_tokens i
           JOIN admin_users a ON a.id = i.created_by_admin_id
           LEFT JOIN users u ON u.invite_id = i.id
           WHERE i.id = ?""",
        (invite_id,),
    ).fetchone()
    if not row:
        raise ValueError("邀请不存在")
    return _invite_item(row)


def reveal_invite_token(db: Connection, invite_id: int) -> dict:
    row = db.execute(
        """SELECT encrypted_token, expires_at, max_uses, use_count, revoked_at
           FROM invite_tokens WHERE id = ?""",
        (invite_id,),
    ).fetchone()
    if not row:
        raise ValueError("邀请不存在")
    if (
        row["revoked_at"]
        or row["use_count"] >= row["max_uses"]
        or datetime.fromisoformat(row["expires_at"]) <= _now()
    ):
        raise ValueError("Token 已使用、撤销或过期，不能查看明文")
    if not row["encrypted_token"]:
        raise ValueError("此 Token 的明文无法恢复")
    return {"id": invite_id, "token": decrypt_invite_token(row["encrypted_token"])}


def revoke_invite(db: Connection, invite_id: int) -> dict:
    now = _now().isoformat()
    cursor = db.execute(
        """UPDATE invite_tokens SET revoked_at = ?
           WHERE id = ? AND revoked_at IS NULL AND use_count < max_uses AND expires_at > ?""",
        (now, invite_id, now),
    )
    if cursor.rowcount == 0:
        existing = db.execute("SELECT id FROM invite_tokens WHERE id = ?", (invite_id,)).fetchone()
        if not existing:
            raise ValueError("邀请不存在")
        raise ValueError("邀请已使用、已撤销或已过期")
    db.commit()
    return get_invite(db, invite_id)


def list_runs(db: Connection, limit: int = 100) -> list[dict]:
    rows = db.execute(
        """SELECT r.id, r.user_id, u.display_name, u.student_no, r.trigger_source,
                  r.status, r.detail, r.started_at, r.finished_at
           FROM checkin_runs r JOIN users u ON u.id = r.user_id
           ORDER BY r.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def dashboard(db: Connection) -> dict:
    counts = db.execute(
        "SELECT COUNT(*) AS users, COALESCE(SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END), 0) AS active_users FROM users"
    ).fetchone()
    enabled = db.execute("SELECT COUNT(*) AS count FROM auto_checkin_schedules WHERE enabled = 1").fetchone()["count"]
    return {
        "users": counts["users"],
        "active_users": counts["active_users"],
        "enabled_schedules": enabled,
        "recent_runs": list_runs(db, 10),
    }


def list_system_logs(db: Connection, limit: int = 500) -> list[dict]:
    rows = db.execute(
        """SELECT id, module, level, message, created_at FROM (
               SELECT r.id AS id, 'checkin' AS module,
                      CASE WHEN r.status IN ('failed', 'timeout') THEN 'ERROR'
                           WHEN r.status IN ('busy') THEN 'WARN' ELSE 'INFO' END AS level,
                      r.detail AS message, r.started_at AS created_at
               FROM checkin_runs r
               UNION ALL
               SELECT (1000000000 + o.id) AS id, 'mail' AS module,
                      CASE WHEN o.status = 'failed' THEN 'ERROR'
                           WHEN o.status IN ('pending', 'sending') THEN 'WARN' ELSE 'INFO' END AS level,
                      CASE WHEN o.event_type = 'email-verification' THEN '邮箱验证码任务：' || o.status
                           ELSE '失败提醒任务：' || o.status END AS message,
                      o.created_at AS created_at
               FROM notification_outbox o
               UNION ALL
               SELECT (2000000000 + c.id) AS id, 'cloud-ocr' AS module,
                      CASE WHEN c.status = 'success' THEN 'INFO' ELSE 'WARN' END AS level,
                      '云端识别 ' || c.status || '，调用 ' || c.cloud_calls || ' 次，合计 ' || c.total_tokens || ' tokens' AS message,
                      c.created_at AS created_at
               FROM cloud_ocr_calls c
           ) ORDER BY created_at DESC LIMIT ?""",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(row) for row in rows]
