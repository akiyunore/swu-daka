import hmac
import json
import logging
import math
import secrets
import threading
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from sqlite3 import Connection

from core.config import get_settings
from core.security import (
    decrypt_email,
    decrypt_notification_code,
    encrypt_email,
    encrypt_notification_code,
    hash_email,
    hash_verification_code,
)
from services.email_service import (
    failure_message,
    safe_email_error,
    send_email,
    send_verification_email,
    verification_message,
)
from storage.database import connect

logger = logging.getLogger(__name__)
_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_WAKE_EVENT = threading.Event()
_WORKER_STOP_EVENT = threading.Event()
_WORKER_LAST_HEARTBEAT = 0.0
_VERIFICATION_RETRY_DELAYS_SECONDS = (15, 45, 120)
_FAILURE_RETRY_DELAYS_SECONDS = (120, 600, 1800)
_OUTBOX_BATCH_LIMIT = 10


class NotificationSuppressed(Exception):
    """The recipient is no longer eligible when a queued message is claimed."""


class EmailResendCooldown(ValueError):
    """A pending verification message is still inside the resend cooldown."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(f"验证码邮件可能存在延迟，请 {self.retry_after_seconds} 秒后再重新发送")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_notification_time(value: str) -> clock_time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("CHECKIN_WEB_FAILURE_NOTIFICATION_TIME 必须为 HH:MM") from exc


def validate_email_configuration() -> None:
    settings = get_settings()
    _parse_notification_time(settings.failure_notification_time)
    if settings.smtp_ssl and settings.smtp_starttls:
        raise RuntimeError("邮件 SMTP 不能同时启用 SSL 和 STARTTLS")
    if settings.email_notifications_enabled and not settings.smtp_ssl and not settings.smtp_starttls:
        raise RuntimeError("邮件 SMTP 必须启用 SSL 或 STARTTLS")
    if settings.smtp_from and ("\r" in settings.smtp_from or "\n" in settings.smtp_from):
        raise RuntimeError("邮件发件人配置包含非法字符")
    if settings.email_notifications_enabled and settings.env == "production" and not settings.email_configured:
        missing = []
        if not settings.smtp_host:
            missing.append("CHECKIN_WEB_SMTP_HOST")
        if not settings.smtp_from:
            missing.append("CHECKIN_WEB_SMTP_FROM")
        if not settings.public_base_url:
            missing.append("CHECKIN_WEB_PUBLIC_BASE_URL")
        if not settings.smtp_password:
            missing.append("CHECKIN_WEB_SMTP_PASSWORD_FILE")
        raise RuntimeError(f"生产环境邮件通知缺少必要配置: {', '.join(missing)}")
    if settings.email_notifications_enabled and settings.env == "production":
        if not settings.public_base_url.lower().startswith("https://"):
            raise RuntimeError("生产环境邮件通知必须使用 HTTPS 公共入口")
    if settings.email_notifications_enabled and settings.verification_smtp_enabled:
        if settings.verification_smtp_ssl and settings.verification_smtp_starttls:
            raise RuntimeError("验证码 SMTP 不能同时启用 SSL 和 STARTTLS")
        if not settings.verification_smtp_ssl and not settings.verification_smtp_starttls:
            raise RuntimeError("验证码 SMTP 必须启用 SSL 或 STARTTLS")
        if settings.verification_smtp_from and (
            "\r" in settings.verification_smtp_from or "\n" in settings.verification_smtp_from
        ):
            raise RuntimeError("验证码邮件发件人配置包含非法字符")
        if (
            settings.verification_smtp_username
            and settings.verification_smtp_from
            and settings.verification_smtp_username != settings.verification_smtp_from
        ):
            raise RuntimeError("阿里云 SMTP 用户名必须与发件人地址一致")
        if settings.env == "production" and not settings.verification_email_configured:
            missing = []
            if not settings.verification_smtp_host:
                missing.append("CHECKIN_WEB_VERIFICATION_SMTP_HOST")
            if not settings.verification_smtp_from:
                missing.append("CHECKIN_WEB_VERIFICATION_SMTP_FROM")
            if not settings.verification_smtp_username:
                missing.append("CHECKIN_WEB_VERIFICATION_SMTP_USERNAME")
            if not settings.verification_smtp_password:
                missing.append("CHECKIN_WEB_VERIFICATION_SMTP_PASSWORD_FILE")
            raise RuntimeError(f"生产环境验证码邮件缺少必要配置: {', '.join(missing)}")


def get_notification_settings(db: Connection, user_id: int) -> dict:
    current = _utc_now()
    row = db.execute(
        """SELECT n.email, n.email_verified_at, n.notify_on_failure,
                  COALESCE(s.enabled, 0) AS schedule_enabled,
                  EXISTS(
                      SELECT 1 FROM email_verification_tokens t
                      WHERE t.user_id = n.user_id AND t.email_hash = n.email_hash
                        AND t.used_at IS NULL AND t.expires_at > ?
                  ) AS verification_pending,
                  (
                      SELECT MAX(t.created_at) FROM email_verification_tokens t
                      WHERE t.user_id = n.user_id AND t.email_hash = n.email_hash
                        AND t.used_at IS NULL AND t.expires_at > ?
                  ) AS verification_last_sent_at
           FROM user_notification_settings n
           LEFT JOIN auto_checkin_schedules s ON s.user_id = n.user_id
           WHERE n.user_id = ?""",
        (current.isoformat(), current.isoformat(), user_id),
    ).fetchone()
    settings = get_settings()
    if not row:
        return {
            "email": None,
            "email_verified_at": None,
            "email_verified": False,
            "notify_on_failure": False,
            "schedule_enabled": False,
            "verification_pending": False,
            "verification_resend_available_at": None,
            "verification_resend_cooldown_seconds": settings.email_resend_cooldown_seconds,
            "email_service_available": settings.email_notifications_enabled
            and settings.verification_email_configured,
        }
    item = dict(row)
    last_sent_at = item.pop("verification_last_sent_at", None)
    item["verification_resend_available_at"] = None
    if last_sent_at:
        sent_at = datetime.fromisoformat(last_sent_at)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        item["verification_resend_available_at"] = (
            sent_at + timedelta(seconds=settings.email_resend_cooldown_seconds)
        ).isoformat()
    item["verification_resend_cooldown_seconds"] = settings.email_resend_cooldown_seconds
    item["email"] = decrypt_email(item["email"]) if item["email"] else None
    item["email_verified"] = bool(item["email_verified_at"])
    item["notify_on_failure"] = bool(item["notify_on_failure"])
    item["schedule_enabled"] = bool(item["schedule_enabled"])
    item["verification_pending"] = bool(item["verification_pending"])
    item["email_service_available"] = (
        settings.email_notifications_enabled and settings.verification_email_configured
    )
    return item


def _enforce_verification_resend_cooldown(
    db: Connection,
    user_id: int,
    email_digest: str,
    now: datetime,
) -> None:
    row = db.execute(
        """SELECT created_at FROM email_verification_tokens
           WHERE user_id = ? AND email_hash = ? AND used_at IS NULL AND expires_at > ?
           ORDER BY id DESC LIMIT 1""",
        (user_id, email_digest, now.isoformat()),
    ).fetchone()
    if not row:
        return
    created_at = datetime.fromisoformat(row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    available_at = created_at + timedelta(
        seconds=get_settings().email_resend_cooldown_seconds
    )
    remaining = math.ceil((available_at - now).total_seconds())
    if remaining > 0:
        raise EmailResendCooldown(remaining)


def _create_verification_outbox(db: Connection, user_id: int, email: str, now: datetime) -> None:
    email_digest = hash_email(email)
    _enforce_verification_resend_cooldown(db, user_id, email_digest, now)
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + timedelta(minutes=get_settings().email_verification_ttl_minutes)
    db.execute("UPDATE email_verification_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now.isoformat(), user_id))
    cursor = db.execute(
        """INSERT INTO email_verification_tokens
           (user_id, email, email_hash, token_hash, encrypted_code, expires_at, attempt_count, used_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
        (
            user_id,
            encrypt_email(email),
            email_digest,
            hash_verification_code(user_id, email, code),
            encrypt_notification_code(code),
            expires_at.isoformat(),
            now.isoformat(),
        ),
    )
    token_id = int(cursor.lastrowid)
    db.execute(
        """INSERT INTO notification_outbox
           (user_id, run_id, dedupe_key, event_type, recipient, recipient_hash, payload_json,
            status, attempt_count, next_attempt_at, locked_at, last_error, created_at, sent_at)
           VALUES (?, NULL, ?, 'email-verification', ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, NULL)""",
        (
            user_id,
            f"verify:{token_id}",
            encrypt_email(email),
            email_digest,
            json.dumps({"verification_token_id": token_id}, separators=(",", ":")),
            now.isoformat(),
            now.isoformat(),
        ),
    )


def queue_registration_verification(db: Connection, user_id: int) -> bool:
    settings = get_settings()
    if not settings.email_notifications_enabled or not settings.verification_email_configured:
        return False
    row = db.execute("SELECT email FROM user_notification_settings WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row["email"]:
        return False
    now = _utc_now()
    db.execute("BEGIN IMMEDIATE")
    try:
        _create_verification_outbox(db, user_id, decrypt_email(row["email"]), now)
        db.commit()
        _WORKER_WAKE_EVENT.set()
        return True
    except Exception:
        db.rollback()
        raise


def update_email_and_queue(db: Connection, user_id: int, email: str) -> dict:
    settings = get_settings()
    if not settings.email_notifications_enabled or not settings.verification_email_configured:
        raise RuntimeError("邮件服务尚未配置，请联系管理员")
    now = _utc_now()
    encrypted = encrypt_email(email)
    email_digest = hash_email(email)
    existing = db.execute(
        """SELECT email_hash, email_verified_at FROM user_notification_settings
           WHERE user_id = ?""",
        (user_id,),
    ).fetchone()
    if (
        existing
        and existing["email_hash"]
        and existing["email_verified_at"]
        and hmac.compare_digest(existing["email_hash"], email_digest)
    ):
        raise ValueError("新邮箱不能与当前已验证邮箱相同")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            """INSERT INTO user_notification_settings
               (user_id, email, email_hash, email_verified_at, notify_on_failure, created_at, updated_at)
               VALUES (?, ?, ?, NULL, 0, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET email = excluded.email, email_hash = excluded.email_hash,
                   email_verified_at = NULL, notify_on_failure = 0, updated_at = excluded.updated_at""",
            (user_id, encrypted, email_digest, now.isoformat(), now.isoformat()),
        )
        _create_verification_outbox(db, user_id, email, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    _WORKER_WAKE_EVENT.set()
    return get_notification_settings(db, user_id)


def verify_email_code(db: Connection, user_id: int, code: str) -> dict:
    now = _utc_now()
    row = db.execute(
        """SELECT t.id, t.email, t.email_hash, t.token_hash, t.expires_at, t.attempt_count
           FROM email_verification_tokens t
           JOIN user_notification_settings n ON n.user_id = t.user_id AND n.email_hash = t.email_hash
           WHERE t.user_id = ? AND t.used_at IS NULL
           ORDER BY t.id DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    if not row:
        raise ValueError("没有待验证的邮箱验证码，请重新发送")
    if datetime.fromisoformat(row["expires_at"]) <= now:
        db.execute("UPDATE email_verification_tokens SET used_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
        db.commit()
        raise ValueError("邮箱验证码已过期，请重新发送")
    plain_email = decrypt_email(row["email"])
    expected = hash_verification_code(user_id, plain_email, code)
    if not hmac.compare_digest(expected, row["token_hash"]):
        attempts = int(row["attempt_count"]) + 1
        used_at = now.isoformat() if attempts >= 5 else None
        db.execute("UPDATE email_verification_tokens SET attempt_count = ?, used_at = ? WHERE id = ?", (attempts, used_at, row["id"]))
        db.commit()
        raise ValueError("邮箱验证码错误")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("UPDATE email_verification_tokens SET used_at = ? WHERE id = ?", (now.isoformat(), row["id"]))
        db.execute(
            """UPDATE user_notification_settings SET email_verified_at = ?, updated_at = ?
               WHERE user_id = ? AND email_hash = ?""",
            (now.isoformat(), now.isoformat(), user_id, row["email_hash"]),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_notification_settings(db, user_id)


def set_failure_notification(db: Connection, user_id: int, enabled: bool) -> dict:
    row = db.execute(
        """SELECT n.email_verified_at, COALESCE(s.enabled, 0) AS schedule_enabled
           FROM user_notification_settings n
           LEFT JOIN auto_checkin_schedules s ON s.user_id = n.user_id
           WHERE n.user_id = ?""",
        (user_id,),
    ).fetchone()
    if not row:
        raise ValueError("请先填写并验证邮箱")
    if enabled and not row["email_verified_at"]:
        raise ValueError("请先完成邮箱验证")
    if enabled and not row["schedule_enabled"]:
        raise ValueError("只有开启自动打卡后才能启用失败邮件提醒")
    now = _utc_now().isoformat()
    db.execute("UPDATE user_notification_settings SET notify_on_failure = ?, updated_at = ? WHERE user_id = ?", (int(enabled), now, user_id))
    db.commit()
    return get_notification_settings(db, user_id)


def _local_datetime(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_SHANGHAI)
    if now.tzinfo is None:
        return now.replace(tzinfo=_SHANGHAI)
    return now.astimezone(_SHANGHAI)


def _failure_category(status: str | None) -> str:
    return {
        "failed": "failed",
        "timeout": "timeout",
        "busy": "busy",
        "completed": "unconfirmed",
        "running": "running",
    }.get(status or "", "unconfirmed")


def reconcile_daily_failures(db: Connection, now: datetime | None = None) -> int:
    settings = get_settings()
    if not settings.email_notifications_enabled or not settings.failure_email_configured:
        return 0
    local_now = _local_datetime(now)
    if local_now.time() < _parse_notification_time(settings.failure_notification_time):
        return 0
    day = local_now.date()
    start = datetime.combine(day, clock_time.min).isoformat()
    end = datetime.combine(day + timedelta(days=1), clock_time.min).isoformat()
    rows = db.execute(
        """SELECT u.id AS user_id, n.email, n.email_hash
           FROM users u
           JOIN auto_checkin_schedules s ON s.user_id = u.id AND s.enabled = 1
           JOIN user_notification_settings n ON n.user_id = u.id
           WHERE u.active = 1 AND n.email_verified_at IS NOT NULL
             AND n.notify_on_failure = 1 AND n.email IS NOT NULL"""
    ).fetchall()
    queued = 0
    account_url = f"{settings.public_base_url}/account"
    for row in rows:
        success = db.execute(
            """SELECT 1 FROM checkin_runs
               WHERE user_id = ? AND started_at >= ? AND started_at < ?
                 AND status IN ('success', 'already-signed') LIMIT 1""",
            (row["user_id"], start, end),
        ).fetchone()
        if success:
            continue
        latest = db.execute(
            """SELECT id, status, started_at FROM checkin_runs
               WHERE user_id = ? AND trigger_source = 'scheduler'
                 AND started_at >= ? AND started_at < ?
               ORDER BY id DESC LIMIT 1""",
            (row["user_id"], start, end),
        ).fetchone()
        category = _failure_category(latest["status"]) if latest else "missed"
        run_id = int(latest["id"]) if latest else None
        payload = {
            "date": day.isoformat(),
            "category": category,
            "run_id": run_id,
            "run_time": latest["started_at"] if latest else None,
            "account_url": account_url,
        }
        cursor = db.execute(
            """INSERT OR IGNORE INTO notification_outbox
               (user_id, run_id, dedupe_key, event_type, recipient, recipient_hash, payload_json,
                status, attempt_count, next_attempt_at, locked_at, last_error, created_at, sent_at)
               VALUES (?, ?, ?, 'daily-failure', ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, NULL)""",
            (
                row["user_id"],
                run_id,
                f"daily-failure:{row['user_id']}:{day.isoformat()}",
                row["email"],
                row["email_hash"],
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                _utc_now().isoformat(),
                _utc_now().isoformat(),
            ),
        )
        queued += cursor.rowcount
    db.commit()
    return queued


def recover_stale_outbox(db: Connection, now: datetime | None = None) -> int:
    current = now or _utc_now()
    stale_before = current - timedelta(minutes=10)
    cursor = db.execute(
        """UPDATE notification_outbox SET status = 'retry', locked_at = NULL,
                  next_attempt_at = ?, last_error = '投递进程中断，等待重试'
           WHERE status = 'sending' AND locked_at <= ?""",
        (current.isoformat(), stale_before.isoformat()),
    )
    db.commit()
    return cursor.rowcount


def _claim_outbox(database_path: Path, now: datetime) -> dict | None:
    with connect(database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT * FROM notification_outbox
               WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
               ORDER BY CASE event_type WHEN 'email-verification' THEN 0 ELSE 1 END,
                        next_attempt_at, id LIMIT 1""",
            (now.isoformat(),),
        ).fetchone()
        if not row:
            db.commit()
            return None
        updated = db.execute(
            """UPDATE notification_outbox SET status = 'sending', locked_at = ?,
                      attempt_count = attempt_count + 1 WHERE id = ? AND status IN ('pending', 'retry')""",
            (now.isoformat(), row["id"]),
        )
        if updated.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        item = dict(row)
        item["attempt_count"] = int(item["attempt_count"]) + 1
        return item


def _message_for_outbox(item: dict, now: datetime) -> tuple[str, str, str]:
    payload = json.loads(item["payload_json"])
    if item["event_type"] == "daily-failure":
        try:
            event_day = datetime.strptime(str(payload["date"]), "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("失败通知日期无效") from exc
        day_start = datetime.combine(event_day, clock_time.min).isoformat()
        day_end = datetime.combine(event_day + timedelta(days=1), clock_time.min).isoformat()
        with connect(get_settings().database_path) as db:
            eligible = db.execute(
                """SELECT 1 FROM users u
                   JOIN auto_checkin_schedules s ON s.user_id = u.id AND s.enabled = 1
                   JOIN user_notification_settings n ON n.user_id = u.id
                   WHERE u.id = ? AND u.active = 1 AND n.email_hash = ?
                     AND n.email_verified_at IS NOT NULL AND n.notify_on_failure = 1
                     AND NOT EXISTS (
                         SELECT 1 FROM checkin_runs r WHERE r.user_id = u.id
                           AND r.started_at >= ? AND r.started_at < ?
                           AND r.status IN ('success', 'already-signed')
                     )""",
                (item["user_id"], item["recipient_hash"], day_start, day_end),
            ).fetchone()
        if not eligible:
            raise NotificationSuppressed("发送前通知条件已失效")
        return failure_message(payload)
    if item["event_type"] != "email-verification":
        raise ValueError("不支持的邮件事件类型")
    token_id = int(payload["verification_token_id"])
    with connect(get_settings().database_path) as db:
        row = db.execute(
            """SELECT encrypted_code, expires_at, used_at FROM email_verification_tokens
               WHERE id = ? AND user_id = ? AND email_hash = ?""",
            (token_id, item["user_id"], item["recipient_hash"]),
        ).fetchone()
    if not row or row["used_at"] or datetime.fromisoformat(row["expires_at"]) <= now:
        raise ValueError("邮箱验证码已失效")
    code = decrypt_notification_code(row["encrypted_code"])
    return verification_message(code, get_settings().email_verification_ttl_minutes)


def _finish_outbox(database_path: Path, item: dict, success: bool, error: BaseException | None, now: datetime) -> None:
    with connect(database_path) as db:
        if success:
            db.execute(
                """UPDATE notification_outbox SET status = 'sent', sent_at = ?,
                          locked_at = NULL, last_error = NULL WHERE id = ?""",
                (now.isoformat(), item["id"]),
            )
        else:
            attempts = int(item["attempt_count"])
            if isinstance(error, NotificationSuppressed):
                status = "cancelled"
                next_attempt = now
            elif isinstance(error, ValueError) or attempts >= get_settings().email_max_attempts:
                status = "dead"
                next_attempt = now
            else:
                status = "retry"
                retry_delays = (
                    _VERIFICATION_RETRY_DELAYS_SECONDS
                    if item["event_type"] == "email-verification"
                    else _FAILURE_RETRY_DELAYS_SECONDS
                )
                delay_index = min(attempts - 1, len(retry_delays) - 1)
                next_attempt = now + timedelta(seconds=retry_delays[delay_index])
            db.execute(
                """UPDATE notification_outbox SET status = ?, next_attempt_at = ?,
                          locked_at = NULL, last_error = ? WHERE id = ?""",
                (
                    status,
                    next_attempt.isoformat(),
                    "发送前通知条件已失效" if isinstance(error, NotificationSuppressed)
                    else safe_email_error(error or RuntimeError()),
                    item["id"],
                ),
            )
        db.commit()


def process_outbox_once(send_func=None, now: datetime | None = None) -> bool:
    settings = get_settings()
    if not settings.email_notifications_enabled:
        return False
    current = now or _utc_now()
    item = _claim_outbox(settings.database_path, current)
    if not item:
        return False
    try:
        from services.checkin_service import user_lifecycle_guard

        with user_lifecycle_guard():
            subject, text_body, html_body = _message_for_outbox(item, current)
            sender = send_func
            if sender is None:
                sender = (
                    send_verification_email
                    if item["event_type"] == "email-verification"
                    else send_email
                )
            sender(decrypt_email(item["recipient"]), subject, text_body, html_body)
    except Exception as exc:
        _finish_outbox(settings.database_path, item, False, exc, current)
        if isinstance(exc, NotificationSuppressed):
            logger.info("Email outbox %s suppressed because eligibility changed", item["id"])
        else:
            logger.warning("Email outbox %s delivery failed: %s", item["id"], safe_email_error(exc))
    else:
        _finish_outbox(settings.database_path, item, True, None, current)
    return True


def _notification_loop() -> None:
    global _WORKER_LAST_HEARTBEAT
    next_reconcile_at = 0.0
    while not _WORKER_STOP_EVENT.is_set():
        _WORKER_LAST_HEARTBEAT = time.monotonic()
        monotonic_now = time.monotonic()
        if monotonic_now >= next_reconcile_at:
            try:
                with connect(get_settings().database_path) as db:
                    reconcile_daily_failures(db)
            except Exception:
                logger.exception("Notification reconciliation failed")
            finally:
                next_reconcile_at = monotonic_now + 60
        try:
            for _ in range(_OUTBOX_BATCH_LIMIT):
                _WORKER_LAST_HEARTBEAT = time.monotonic()
                if not process_outbox_once():
                    break
        except Exception:
            logger.exception("Notification outbox iteration failed")
        _WORKER_WAKE_EVENT.wait(15)
        _WORKER_WAKE_EVENT.clear()


def start_notification_worker() -> None:
    global _WORKER_THREAD, _WORKER_LAST_HEARTBEAT
    settings = get_settings()
    if not settings.email_notifications_enabled:
        return
    with _WORKER_LOCK:
        if _WORKER_THREAD and _WORKER_THREAD.is_alive():
            return
        _WORKER_STOP_EVENT.clear()
        _WORKER_LAST_HEARTBEAT = time.monotonic()
        with connect(settings.database_path) as db:
            recover_stale_outbox(db)
        _WORKER_THREAD = threading.Thread(target=_notification_loop, name="email-notification-worker", daemon=True)
        _WORKER_THREAD.start()


def stop_notification_worker() -> None:
    global _WORKER_THREAD
    with _WORKER_LOCK:
        thread = _WORKER_THREAD
        _WORKER_THREAD = None
        _WORKER_STOP_EVENT.set()
        _WORKER_WAKE_EVENT.set()
    if thread and thread.is_alive():
        thread.join(timeout=30)


def notification_worker_healthy() -> bool:
    if not get_settings().email_notifications_enabled:
        return True
    thread = _WORKER_THREAD
    return bool(
        thread
        and thread.is_alive()
        and _WORKER_LAST_HEARTBEAT
        and time.monotonic() - _WORKER_LAST_HEARTBEAT < 45
    )
