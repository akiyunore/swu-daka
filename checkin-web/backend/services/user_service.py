from datetime import datetime, timedelta, timezone
from functools import lru_cache
from sqlite3 import Connection, IntegrityError

from core.config import get_settings
from core.security import (
    encrypt_email,
    encrypt_school_password,
    hash_email,
    hash_token,
    hash_user_password,
    new_random_token,
    verify_web_password,
)
from services.public_service import read_invite


def _now() -> datetime:
    return datetime.now(timezone.utc)


@lru_cache
def _dummy_password_hash() -> str:
    return hash_user_password("invalid-user-password")


def _user_profile(db: Connection, user_id: int) -> dict:
    row = db.execute(
        """SELECT u.id, u.username, u.display_name, u.student_no, u.active,
                  u.has_agreed_terms, u.credential_verified_at, u.created_at,
                  CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS has_credential,
                  COALESCE(s.enabled, 0) AS schedule_enabled,
                  s.next_run_at, s.last_run_at, s.last_status, s.last_detail
           FROM users u
           LEFT JOIN credentials c ON c.user_id = u.id
           LEFT JOIN auto_checkin_schedules s ON s.user_id = u.id
           WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if not row:
        raise ValueError("用户不存在")
    return dict(row)


def create_user_session(db: Connection, user_id: int) -> tuple[str, dict]:
    token = new_random_token()
    csrf = new_random_token(24)
    now = _now()
    expires_at = now + timedelta(hours=get_settings().session_ttl_hours)
    db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now.isoformat(),))
    db.execute(
        """INSERT INTO user_sessions
           (user_id, token_hash, csrf_token, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, hash_token(token), csrf, expires_at.isoformat(), now.isoformat()),
    )
    db.commit()
    return token, {
        "csrf_token": csrf,
        "expires_at": expires_at.isoformat(),
        "user": _user_profile(db, user_id),
    }


def login_user(db: Connection, username: str, password: str) -> tuple[str, dict]:
    row = db.execute(
        "SELECT id, password_hash, active FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()
    candidate_hash = row["password_hash"] if row and row["password_hash"] else _dummy_password_hash()
    if not verify_web_password(password, candidate_hash) or not row:
        raise ValueError("用户名或密码错误")
    if not row["active"]:
        raise ValueError("用户已被管理员停用")
    return create_user_session(db, int(row["id"]))


def validate_registration_target(db: Connection, username: str) -> None:
    row = db.execute(
        "SELECT password_hash, active FROM users WHERE username = ?",
        (username.strip(),),
    ).fetchone()
    if not row:
        return
    if row["password_hash"]:
        raise ValueError("该校园账号已注册，请直接登录")
    if not row["active"]:
        raise ValueError("用户已被管理员停用，无法激活登录")


def register_user(
    db: Connection,
    invitation_token: str,
    school_username: str,
    school_password: str,
    display_name: str | None,
    email: str,
) -> tuple[int, bool]:
    invite = read_invite(db, invitation_token.strip())
    encrypted, key_id = encrypt_school_password(school_password)
    password_hash = hash_user_password(school_password)
    username = school_username.strip()
    name = display_name.strip() if display_name and display_name.strip() else None
    encrypted_email = encrypt_email(email)
    email_digest = hash_email(email)
    now = _now().isoformat()
    created = False
    try:
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute(
            """UPDATE invite_tokens
               SET use_count = use_count + 1,
                   encrypted_token = CASE
                       WHEN use_count + 1 >= max_uses THEN NULL ELSE encrypted_token END
               WHERE id = ? AND revoked_at IS NULL AND use_count < max_uses AND expires_at > ?""",
            (invite["id"], now),
        )
        if claimed.rowcount != 1:
            raise ValueError("邀请 Token 已被使用、撤销或过期")

        existing = db.execute(
            "SELECT id, password_hash, active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if existing:
            if existing["password_hash"]:
                raise ValueError("该校园账号已注册，请直接登录")
            if not existing["active"]:
                raise ValueError("用户已被管理员停用，无法激活登录")
            user_id = int(existing["id"])
            db.execute(
                """UPDATE users SET display_name = COALESCE(?, display_name),
                          admin_note = COALESCE(?, admin_note),
                          password_hash = ?, has_agreed_terms = 1, agreed_at = ?,
                          credential_verified_at = ?, invite_id = ?
                   WHERE id = ?""",
                (name, invite["label"], password_hash, now, now, invite["id"], user_id),
            )
        else:
            cursor = db.execute(
                """INSERT INTO users (
                       username, display_name, student_no, admin_note, password_hash,
                       has_agreed_terms, agreed_at, credential_verified_at,
                       invite_id, active, created_at
                   ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?)""",
                (username, name, username, invite["label"], password_hash, now, now, invite["id"], now),
            )
            user_id = int(cursor.lastrowid)
            created = True

        db.execute(
            """INSERT INTO credentials
               (user_id, school_username, encrypted_school_password, key_id, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   school_username = excluded.school_username,
                   encrypted_school_password = excluded.encrypted_school_password,
                   key_id = excluded.key_id,
                   updated_at = excluded.updated_at""",
            (user_id, username, encrypted, key_id, now),
        )
        db.execute(
            """INSERT INTO user_notification_settings
               (user_id, email, email_hash, email_verified_at, notify_on_failure, created_at, updated_at)
               VALUES (?, ?, ?, NULL, 0, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   email = excluded.email,
                   email_hash = excluded.email_hash,
                   email_verified_at = CASE
                       WHEN user_notification_settings.email_hash = excluded.email_hash
                       THEN user_notification_settings.email_verified_at ELSE NULL END,
                   notify_on_failure = CASE
                       WHEN user_notification_settings.email_hash = excluded.email_hash
                       THEN user_notification_settings.notify_on_failure ELSE 0 END,
                   updated_at = excluded.updated_at""",
            (user_id, encrypted_email, email_digest, now, now),
        )
        db.execute(
            "UPDATE email_verification_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now, user_id),
        )
        db.commit()
        return user_id, created
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("该校园账号已注册") from exc
    except Exception:
        db.rollback()
        raise


def get_user_profile(db: Connection, user_id: int) -> dict:
    return _user_profile(db, user_id)
