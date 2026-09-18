import hashlib
import json
import logging
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from sqlite3 import Connection

from core.config import get_settings
from core.security import decrypt_school_password
from services.cloud_ocr_service import cloud_ocr_environment, record_cloud_ocr_usage
from storage.database import connect

AUTO_CHECKIN_HOUR = 21
AUTO_CHECKIN_MINUTE = 10

_AUTO_THREAD_LOCK = threading.Lock()
_AUTO_THREAD: threading.Thread | None = None
_AUTO_STOP_EVENT = threading.Event()
_AUTO_LAST_HEARTBEAT = 0.0
_USER_LIFECYCLE_LOCK = threading.RLock()
_ACTIVE_USERS_LOCK = threading.Lock()
_ACTIVE_USER_IDS: set[int] = set()
_RUN_SEMAPHORE = threading.BoundedSemaphore(get_settings().max_concurrent_runs)
_EXECUTOR: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=get_settings().max_concurrent_runs,
    thread_name_prefix="checkin-worker",
)
logger = logging.getLogger(__name__)
_POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)


def is_user_run_active(user_id: int) -> bool:
    with _ACTIVE_USERS_LOCK:
        return user_id in _ACTIVE_USER_IDS


def user_lifecycle_guard() -> threading.RLock:
    """Serialize user deletion with the short task-start reservation window."""
    return _USER_LIFECYCLE_LOCK


def _legacy_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _tail_output(output: str, limit: int = 40) -> list[str]:
    return [line for line in output.splitlines() if line.strip()][-limit:]


def _safe_login_tail(output: str) -> list[str]:
    """Keep actionable login stages without retaining the submitted username."""
    return [line for line in _tail_output(output) if "开始登录, 账号:" not in line]


def _login_failure_detail(output: str) -> str:
    mappings = (
        (
            ("账号或密码错误", "用户名或密码错误", "学校认证未接受校园账号或密码"),
            "学校认证页返回：账号或密码错误",
        ),
        (
            (
                "IDM 验证码图片接口异常",
                "IDM 验证码校验接口异常",
                "IDM 登录接口异常",
            ),
            "学校认证接口拒绝了容器请求，请稍后重试",
        ),
        (("验证码校验失败", "验证码错误", "验证码不正确"), "学校认证页返回：四位验证码校验失败，请重试"),
        (("验证码长度异常",), "四位验证码 OCR 识别异常，请重试"),
        (("当前验证码类型暂不支持",), "学校临时切换了非四位图片验证码，当前无法自动验证"),
        (("等待 IDM 页超时",), "学校旧认证入口跳转超时"),
        (
            ("IDM OAuth 授权回调未完成", "SSO 已完成但未捕获 Token"),
            "学校单点登录回调未返回授权 Token，请稍后重试",
        ),
        (("Chrome 调试端口未就绪", "Connection closed while reading from the driver"), "浏览器验证环境异常，请稍后重试"),
        (("校园登录流程超时",), "学校认证流程响应超时，请稍后重试"),
    )
    matches = []
    for markers, detail in mappings:
        position = max(output.rfind(marker) for marker in markers)
        if position >= 0:
            matches.append((position, detail))
    if matches:
        # Retries can contain several historical failures. Report the latest
        # specific cause instead of an earlier captcha error.
        return max(matches, key=lambda item: item[0])[1]
    if "登录失败, 未获取到 Token" in output:
        return "校园登录流程未返回授权 Token，请稍后重试"
    return "校园账号验证失败，请检查学校登录服务状态"


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dt_text(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _next_scheduled_time(user_id: int, now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    scheduled = current.replace(hour=AUTO_CHECKIN_HOUR, minute=AUTO_CHECKIN_MINUTE, second=0, microsecond=0)
    if scheduled <= current:
        scheduled += timedelta(days=1)
    jitter_window = get_settings().schedule_jitter_seconds
    if jitter_window:
        seed = f"{scheduled.date().isoformat()}:{user_id}".encode()
        jitter = int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") % (jitter_window + 1)
        scheduled += timedelta(seconds=jitter)
    return scheduled


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _runtime_env(
    runtime_dir: Path,
    school_username: str,
    cloud_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    settings = get_settings()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "audit").mkdir(exist_ok=True)
    (runtime_dir / "network").mkdir(exist_ok=True)
    (runtime_dir / "runs").mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "SWU_USERNAME": school_username,
        "SWU_RUNTIME_DIR": str(runtime_dir),
        "SWU_CACHE_FILE": str(runtime_dir / "checkin_cache.json"),
        "SWU_TOKEN_FILE": str(runtime_dir / "token"),
        "SWU_AUDIT_LOG_DIR": str(runtime_dir / "audit"),
        "SWU_NETWORK_LOG_DIR": str(runtime_dir / "network"),
        "SWU_USER_DATA_DIR": str(runtime_dir / "browser-profile"),
        "SWU_CHROME_HEADLESS": "1" if settings.chrome_headless else "0",
    }
    if settings.chrome_executable:
        env["SWU_CHROME_EXECUTABLE"] = settings.chrome_executable
    if cloud_environment:
        if "CLOUD_OCR_ENABLED" in cloud_environment:
            env.pop("MIMO_API_KEY", None)
            env.pop("MIMO_API_KEY_FILE", None)
        env.update(cloud_environment)
    return env


def _terminate_cli_process(proc: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """Stop the isolated CLI process group, including Chromium and Playwright children."""
    if proc is None:
        return

    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            # The CLI parent may exit before Chromium's remaining processes.
            os.killpg(proc.pid, _POSIX_SIGKILL)
        except ProcessLookupError:
            pass
        return

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _execute_cli(
    runtime_dir: Path,
    school_username: str,
    school_password: str,
    mode: str,
    timeout: int | None = None,
    db: Connection | None = None,
    user_id: int | None = None,
) -> dict:
    timeout = timeout or get_settings().cli_timeout_seconds
    audit_log_path = runtime_dir / "audit" / f"{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jsonl"
    cloud_config = cloud_ocr_environment(db) if db is not None else None
    env = _runtime_env(
        runtime_dir,
        school_username,
        cloud_config,
    )
    env["SWU_PASSWORD"] = school_password
    command = [
        sys.executable,
        "login_and_checkin.py",
        "--debug-port",
        str(_free_local_port()),
        "--user-data-dir",
        str(runtime_dir / "browser-profile"),
        "--audit-log",
        str(audit_log_path),
    ]
    command.append("--login-only" if mode == "login" else "--cqtj-checkin")
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=_legacy_root(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_cli_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=1) if proc else (exc.stdout, exc.stderr)
        except (subprocess.TimeoutExpired, OSError):
            stdout, stderr = exc.stdout, exc.stderr
        output = "\n".join(str(part) for part in (stdout, stderr) if part)
        log_tail = _safe_login_tail(output) if mode == "login" else _tail_output(output)
        if mode == "login":
            logger.warning("Campus login verification timed out: %s", " | ".join(log_tail[-12:]))
        if db is not None:
            _record_cloud_ocr_usage_safely(
                db, user_id, mode, output, "timeout", cloud_config
            )
        return {
            "status": "timeout",
            "detail": "学校认证流程响应超时，请稍后重试" if mode == "login" else "任务流程超时，请查看审计日志",
            "audit_log_path": str(audit_log_path),
            "log_tail": log_tail,
        }
    finally:
        _terminate_cli_process(proc)
    output = "\n".join(part for part in (stdout, stderr) if part)
    result_status = "success" if proc.returncode == 0 else "failed"
    if db is not None:
        _record_cloud_ocr_usage_safely(
            db, user_id, mode, output, result_status, cloud_config
        )
    if mode == "login":
        successful = proc.returncode == 0 and "[OK] 登录成功" in output
        log_tail = _safe_login_tail(output)
        detail = "校园账号验证成功" if successful else _login_failure_detail(output)
        if not successful:
            logger.warning("Campus login verification failed: %s", " | ".join(log_tail[-12:]))
        return {
            "status": "success" if successful else "failed",
            "detail": detail,
            "audit_log_path": str(audit_log_path),
            "log_tail": log_tail,
        }
    if proc.returncode == 0 and "[OK] 签到成功" in output:
        status, detail = "success", "签到成功"
    elif proc.returncode == 0 and "已签到" in output and "不重复提交" in output:
        status, detail = "already-signed", "今日任务已签到，未重复提交"
    elif proc.returncode == 0:
        status, detail = "completed", "流程已完成，请在官方系统复核最终状态"
    else:
        status, detail = "failed", "签到失败，请查看日志输出"
    return {
        "status": status,
        "detail": detail,
        "audit_log_path": str(audit_log_path),
        "log_tail": _tail_output(output),
    }


def _record_cloud_ocr_usage_safely(
    db: Connection,
    user_id: int | None,
    context: str,
    output: str,
    status: str,
    cloud_config: dict[str, str] | None,
) -> None:
    try:
        record_cloud_ocr_usage(
            db,
            user_id=user_id,
            context=context,
            output=output,
            status=status,
            base_url_host=(cloud_config or {}).get("CLOUD_OCR_AUDIT_HOST"),
            model=(cloud_config or {}).get("CLOUD_OCR_AUDIT_MODEL"),
        )
    except Exception:
        db.rollback()
        logger.exception("Cloud OCR telemetry recording failed")


def verify_school_login(school_username: str, school_password: str) -> dict:
    base = get_settings().data_dir / "enrollment-checks"
    base.mkdir(parents=True, exist_ok=True)
    runtime_dir = Path(tempfile.mkdtemp(prefix="verify-", dir=base))
    try:
        if not _RUN_SEMAPHORE.acquire(blocking=False):
            return {"status": "busy", "detail": "系统正在处理其他账号，请稍后重试", "log_tail": []}
        try:
            with connect(get_settings().database_path) as config_db:
                return _execute_cli(
                    runtime_dir, school_username, school_password, "login", db=config_db
                )
        finally:
            _RUN_SEMAPHORE.release()
    finally:
        if runtime_dir.resolve().is_relative_to(base.resolve()):
            shutil.rmtree(runtime_dir, ignore_errors=True)


def _insert_run(db: Connection, user_id: int, trigger_source: str) -> int:
    cursor = db.execute(
        """INSERT INTO checkin_runs
           (user_id, trigger_source, status, detail, audit_log_path, log_tail, started_at, finished_at)
           VALUES (?, ?, 'running', '任务执行中', NULL, '[]', ?, NULL)""",
        (user_id, trigger_source, _now_text()),
    )
    db.commit()
    return int(cursor.lastrowid)


def _finish_run(db: Connection, run_id: int, result: dict) -> None:
    db.execute(
        """UPDATE checkin_runs SET status = ?, detail = ?, audit_log_path = ?,
                  log_tail = ?, finished_at = ? WHERE id = ?""",
        (
            result["status"],
            result["detail"],
            result.get("audit_log_path"),
            json.dumps(result.get("log_tail", []), ensure_ascii=False),
            _now_text(),
            run_id,
        ),
    )
    db.commit()


def run_checkin_for_user(db: Connection, user_id: int, trigger_source: str = "admin") -> dict:
    with _USER_LIFECYCLE_LOCK:
        row = db.execute(
            """SELECT u.active, c.school_username, c.encrypted_school_password, c.key_id
               FROM users u JOIN credentials c ON c.user_id = u.id WHERE u.id = ?""",
            (user_id,),
        ).fetchone()
        if not row:
            raise ValueError("用户不存在或未绑定凭据")
        if not row["active"]:
            raise ValueError("用户已停用")
        run_id = _insert_run(db, user_id, trigger_source)
        with _ACTIVE_USERS_LOCK:
            if user_id in _ACTIVE_USER_IDS:
                result = {
                    "status": "busy",
                    "detail": "该用户已有任务正在执行，请勿重复提交",
                    "audit_log_path": None,
                    "log_tail": [],
                }
                _finish_run(db, run_id, result)
                return {"run_id": run_id, **result}
            _ACTIVE_USER_IDS.add(user_id)
    if not _RUN_SEMAPHORE.acquire(blocking=False):
        result = {"status": "busy", "detail": "并发任务已满，请稍后再试", "audit_log_path": None, "log_tail": []}
        try:
            _finish_run(db, run_id, result)
        finally:
            with _ACTIVE_USERS_LOCK:
                _ACTIVE_USER_IDS.discard(user_id)
        return {"run_id": run_id, **result}
    try:
        try:
            password = decrypt_school_password(row["encrypted_school_password"], row["key_id"])
            runtime_dir = get_settings().data_dir / "users" / str(user_id)
            result = _execute_cli(
                runtime_dir,
                row["school_username"],
                password,
                "checkin",
                db=db,
                user_id=user_id,
            )
        except Exception:
            logger.exception("Check-in execution failed for user_id=%s", user_id)
            result = {
                "status": "failed",
                "detail": "任务执行异常，请稍后重试或联系管理员查看服务器日志",
                "audit_log_path": None,
                "log_tail": [],
            }
        _finish_run(db, run_id, result)
        return {"run_id": run_id, **result}
    finally:
        _RUN_SEMAPHORE.release()
        with _ACTIVE_USERS_LOCK:
            _ACTIVE_USER_IDS.discard(user_id)


def set_auto_checkin(db: Connection, user_id: int, enabled: bool) -> dict:
    row = db.execute(
        """SELECT u.id, u.active, c.id AS credential_id FROM users u
           LEFT JOIN credentials c ON c.user_id = u.id WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if not row:
        raise ValueError("用户不存在")
    if enabled and (not row["active"] or not row["credential_id"]):
        raise ValueError("用户未启用或未绑定凭据")
    now = _now_text()
    next_run = _dt_text(_next_scheduled_time(user_id)) if enabled else None
    db.execute(
        """INSERT INTO auto_checkin_schedules
           (user_id, enabled, schedule_time, next_run_at, created_at, updated_at)
           VALUES (?, ?, '21:10', ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled,
               schedule_time = excluded.schedule_time, next_run_at = excluded.next_run_at,
               updated_at = excluded.updated_at""",
        (user_id, int(enabled), next_run, now, now),
    )
    if not enabled:
        db.execute(
            "UPDATE user_notification_settings SET notify_on_failure = 0, updated_at = ? WHERE user_id = ?",
            (now, user_id),
        )
    db.commit()
    return {"user_id": user_id, "enabled": enabled, "next_run_at": next_run}


def _scheduled_worker(user_id: int, claim_id: int | None = None) -> None:
    try:
        with connect(get_settings().database_path) as db:
            if claim_id is not None:
                db.execute(
                    "UPDATE scheduled_run_claims SET status = 'running', updated_at = ? WHERE id = ?",
                    (_now_text(), claim_id),
                )
                db.commit()
            result = run_checkin_for_user(db, user_id, "scheduler")
            db.execute(
                """UPDATE auto_checkin_schedules SET last_run_at = ?, last_status = ?,
                          last_detail = ?, last_audit_log_path = ?, updated_at = ? WHERE user_id = ?""",
                (_now_text(), result["status"], result["detail"], result.get("audit_log_path"), _now_text(), user_id),
            )
            if claim_id is not None:
                db.execute(
                    """UPDATE scheduled_run_claims SET status = 'finished',
                              updated_at = ?, finished_at = ? WHERE id = ?""",
                    (_now_text(), _now_text(), claim_id),
                )
            db.commit()
    except Exception:
        logger.exception("Scheduled check-in worker failed before completing user_id=%s", user_id)


def _submit_scheduled_claim(user_id: int, claim_id: int) -> None:
    executor = _EXECUTOR
    if executor is None:
        raise RuntimeError("任务执行器正在关闭")
    executor.submit(_scheduled_worker, user_id, claim_id)


def _recover_incomplete_scheduled_claims() -> None:
    with connect(get_settings().database_path) as db:
        now = _now_text()
        retention_cutoff = _dt_text(datetime.now() - timedelta(days=30))
        db.execute(
            "DELETE FROM scheduled_run_claims WHERE status = 'finished' AND finished_at < ?",
            (retention_cutoff,),
        )
        db.execute(
            """UPDATE checkin_runs SET status = 'failed', detail = '服务重启中断，计划任务将重新确认',
                      finished_at = ? WHERE trigger_source = 'scheduler' AND status = 'running'""",
            (now,),
        )
        db.execute(
            "UPDATE scheduled_run_claims SET status = 'claimed', updated_at = ? WHERE status IN ('claimed', 'running')",
            (now,),
        )
        rows = db.execute(
            "SELECT id, user_id FROM scheduled_run_claims WHERE status = 'claimed' ORDER BY id"
        ).fetchall()
        db.commit()
    for row in rows:
        _submit_scheduled_claim(int(row["user_id"]), int(row["id"]))


def _claim_due_schedules(now: datetime) -> list[tuple[int, int]]:
    claimed_schedules: list[tuple[int, int]] = []
    with connect(get_settings().database_path) as db:
        rows = db.execute(
            """SELECT s.id AS schedule_id, s.user_id, s.next_run_at FROM auto_checkin_schedules s
               JOIN users u ON u.id = s.user_id
               WHERE s.enabled = 1 AND u.active = 1
                 AND s.next_run_at IS NOT NULL AND s.next_run_at <= ?
               ORDER BY s.next_run_at, s.user_id""",
            (_dt_text(now),),
        ).fetchall()
        for row in rows:
            user_id = int(row["user_id"])
            db.execute("BEGIN IMMEDIATE")
            claim = db.execute(
                """INSERT OR IGNORE INTO scheduled_run_claims
                   (schedule_id, user_id, scheduled_for, status, created_at, updated_at)
                   SELECT id, user_id, next_run_at, 'claimed', ?, ?
                   FROM auto_checkin_schedules
                   WHERE id = ? AND enabled = 1 AND next_run_at = ? AND next_run_at <= ?""",
                (_now_text(), _now_text(), row["schedule_id"], row["next_run_at"], _dt_text(now)),
            )
            if claim.rowcount == 1:
                claim_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                db.execute(
                    "UPDATE auto_checkin_schedules SET next_run_at = ?, updated_at = ? WHERE id = ?",
                    (_dt_text(_next_scheduled_time(user_id, now)), _now_text(), row["schedule_id"]),
                )
                db.commit()
                claimed_schedules.append((user_id, claim_id))
            else:
                db.rollback()
    return claimed_schedules


def _auto_checkin_loop() -> None:
    global _AUTO_LAST_HEARTBEAT
    while not _AUTO_STOP_EVENT.wait(15):
        _AUTO_LAST_HEARTBEAT = time.monotonic()
        try:
            now = datetime.now()
            for user_id, claim_id in _claim_due_schedules(now):
                _submit_scheduled_claim(user_id, claim_id)
        except Exception:
            logger.exception("Auto check-in scheduler iteration failed")


def start_auto_checkin_scheduler() -> None:
    global _AUTO_THREAD, _EXECUTOR, _AUTO_LAST_HEARTBEAT
    with _AUTO_THREAD_LOCK:
        if _AUTO_THREAD and _AUTO_THREAD.is_alive():
            return
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=get_settings().max_concurrent_runs,
                thread_name_prefix="checkin-worker",
            )
        _AUTO_STOP_EVENT.clear()
        _AUTO_LAST_HEARTBEAT = time.monotonic()
        _recover_incomplete_scheduled_claims()
        _AUTO_THREAD = threading.Thread(target=_auto_checkin_loop, name="auto-checkin-scheduler", daemon=True)
        _AUTO_THREAD.start()


def stop_auto_checkin_scheduler() -> None:
    global _AUTO_THREAD, _EXECUTOR
    with _AUTO_THREAD_LOCK:
        thread = _AUTO_THREAD
        _AUTO_THREAD = None
        _AUTO_STOP_EVENT.set()
    if thread and thread.is_alive():
        thread.join(timeout=20)
    with _AUTO_THREAD_LOCK:
        executor = _EXECUTOR
        _EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)


def auto_checkin_scheduler_healthy() -> bool:
    thread = _AUTO_THREAD
    return bool(
        thread
        and thread.is_alive()
        and _AUTO_LAST_HEARTBEAT
        and time.monotonic() - _AUTO_LAST_HEARTBEAT < 45
    )
