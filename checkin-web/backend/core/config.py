import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    env: str
    project_root: Path
    data_dir: Path
    database_path: Path
    log_dir: Path
    cors_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    admin_page_path: str
    admin_api_prefix: str
    admin_username: str | None
    admin_password: str | None
    credential_key: str | None
    session_cookie_name: str
    user_session_cookie_name: str
    session_ttl_hours: int
    secure_cookies: bool
    max_concurrent_runs: int
    api_thread_limit: int
    max_request_body_bytes: int
    cli_timeout_seconds: int
    schedule_jitter_seconds: int
    chrome_executable: str | None
    chrome_headless: bool
    email_notifications_enabled: bool
    public_base_url: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_ssl: bool
    smtp_starttls: bool
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str | None
    verification_smtp_enabled: bool
    verification_smtp_host: str | None
    verification_smtp_port: int
    verification_smtp_ssl: bool
    verification_smtp_starttls: bool
    verification_smtp_username: str | None
    verification_smtp_password: str | None
    verification_smtp_from: str | None
    email_verification_ttl_minutes: int
    email_resend_cooldown_seconds: int
    email_max_attempts: int
    failure_notification_time: str
    cloud_ocr_allowed_hosts: tuple[str, ...]

    @property
    def email_configured(self) -> bool:
        """Backward-compatible alias for the failure-notification channel."""
        return self.failure_email_configured

    @property
    def failure_email_configured(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_from
            and self.public_base_url
            and self.smtp_password
            and self.smtp_ssl != self.smtp_starttls
        )

    @property
    def verification_email_configured(self) -> bool:
        if not self.verification_smtp_enabled:
            return self.failure_email_configured
        return bool(
            self.verification_smtp_host
            and self.verification_smtp_username
            and self.verification_smtp_from
            and self.public_base_url
            and self.verification_smtp_password
            and self.verification_smtp_ssl != self.verification_smtp_starttls
        )


def _resolve_path(value: str | None, base_dir: Path, default: Path) -> Path:
    if not value:
        return default
    path = Path(value)
    if not path.is_absolute():
        return base_dir / path
    return path


def _split_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or default


def _read_secret(name: str) -> str | None:
    file_path = os.getenv(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return os.getenv(name) or None


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _admin_route(name: str, *, api: bool) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} 必须在私有部署配置中设置")
    pattern = r"/api/[A-Za-z0-9][A-Za-z0-9_-]{2,63}" if api else r"/[A-Za-z0-9][A-Za-z0-9_-]{2,63}"
    if not re.fullmatch(pattern, value):
        raise RuntimeError(f"{name} 必须是单段路径，长度 3–64，仅允许英文字母、数字、下划线和连字符")
    segment = value.rsplit("/", 1)[-1].lower()
    reserved = {"public", "user"} if api else {"api", "assets", "account", "terms", "health", "docs", "redoc", "login"}
    if segment in reserved:
        raise RuntimeError(f"{name} 与现有路由冲突")
    return value


@lru_cache
def get_settings() -> Settings:
    project_root = Path(__file__).resolve().parents[2]
    data_dir = _resolve_path(os.getenv("CHECKIN_WEB_DATA_DIR"), project_root, project_root / "data")
    database_url = os.getenv("CHECKIN_WEB_DATABASE_URL", "sqlite:///./data/app.db")
    if database_url.startswith("sqlite:///"):
        raw_db_path = database_url.removeprefix("sqlite:///")
        database_path = _resolve_path(raw_db_path, project_root, project_root / "data" / "app.db")
    else:
        database_path = data_dir / "app.db"
    log_dir = _resolve_path(os.getenv("CHECKIN_WEB_LOG_DIR"), project_root, data_dir / "logs")
    env = os.getenv("CHECKIN_WEB_ENV", "local").strip().lower()
    if env not in {"local", "production"}:
        raise RuntimeError("CHECKIN_WEB_ENV 只能是 local 或 production")
    local_hosts = ("localhost", "127.0.0.1", "testserver")
    return Settings(
        env=env,
        project_root=project_root,
        data_dir=data_dir,
        database_path=database_path,
        log_dir=log_dir,
        cors_origins=_split_csv(
            os.getenv("CHECKIN_WEB_CORS_ORIGINS"),
            () if env == "production" else ("http://127.0.0.1:5173", "http://localhost:5173"),
        ),
        allowed_hosts=_split_csv(
            os.getenv("CHECKIN_WEB_ALLOWED_HOSTS"),
            ("localhost", "127.0.0.1") if env == "production" else local_hosts,
        ),
        admin_page_path=_admin_route("CHECKIN_WEB_ADMIN_PAGE_PATH", api=False),
        admin_api_prefix=_admin_route("CHECKIN_WEB_ADMIN_API_PREFIX", api=True),
        admin_username=os.getenv("CHECKIN_WEB_ADMIN_USERNAME") or None,
        admin_password=_read_secret("CHECKIN_WEB_ADMIN_PASSWORD"),
        credential_key=_read_secret("CHECKIN_WEB_CREDENTIAL_KEY"),
        session_cookie_name=os.getenv("CHECKIN_WEB_SESSION_COOKIE", "swu_admin_session"),
        user_session_cookie_name=os.getenv("CHECKIN_WEB_USER_SESSION_COOKIE", "swu_user_session"),
        session_ttl_hours=_as_int(os.getenv("CHECKIN_WEB_SESSION_TTL_HOURS"), 12, 1, 168),
        secure_cookies=_as_bool(os.getenv("CHECKIN_WEB_SECURE_COOKIES"), env != "local"),
        max_concurrent_runs=_as_int(os.getenv("CHECKIN_WEB_MAX_CONCURRENT_RUNS"), 1, 1, 4),
        api_thread_limit=_as_int(os.getenv("CHECKIN_WEB_API_THREAD_LIMIT"), 8, 4, 32),
        max_request_body_bytes=_as_int(
            os.getenv("CHECKIN_WEB_MAX_REQUEST_BODY_BYTES"), 64 * 1024, 4096, 1024 * 1024
        ),
        cli_timeout_seconds=_as_int(os.getenv("CHECKIN_WEB_CLI_TIMEOUT_SECONDS"), 270, 120, 900),
        schedule_jitter_seconds=_as_int(os.getenv("CHECKIN_WEB_SCHEDULE_JITTER_SECONDS"), 600, 0, 3600),
        chrome_executable=os.getenv("CHECKIN_WEB_CHROME_EXECUTABLE") or None,
        chrome_headless=_as_bool(os.getenv("CHECKIN_WEB_CHROME_HEADLESS"), True),
        email_notifications_enabled=_as_bool(
            os.getenv("CHECKIN_WEB_EMAIL_NOTIFICATIONS_ENABLED"), False
        ),
        public_base_url=(os.getenv("CHECKIN_WEB_PUBLIC_BASE_URL") or "").rstrip("/") or None,
        smtp_host=os.getenv("CHECKIN_WEB_SMTP_HOST", "smtp.resend.com") or None,
        smtp_port=_as_int(os.getenv("CHECKIN_WEB_SMTP_PORT"), 465, 1, 65535),
        smtp_ssl=_as_bool(os.getenv("CHECKIN_WEB_SMTP_SSL"), True),
        smtp_starttls=_as_bool(os.getenv("CHECKIN_WEB_SMTP_STARTTLS"), False),
        smtp_username=os.getenv("CHECKIN_WEB_SMTP_USERNAME", "resend") or None,
        smtp_password=_read_secret("CHECKIN_WEB_SMTP_PASSWORD"),
        smtp_from=os.getenv("CHECKIN_WEB_SMTP_FROM") or None,
        verification_smtp_enabled=_as_bool(
            os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_ENABLED"), False
        ),
        verification_smtp_host=os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_HOST") or None,
        verification_smtp_port=_as_int(
            os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_PORT"), 465, 1, 65535
        ),
        verification_smtp_ssl=_as_bool(
            os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_SSL"), True
        ),
        verification_smtp_starttls=_as_bool(
            os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_STARTTLS"), False
        ),
        verification_smtp_username=(
            os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_USERNAME") or None
        ),
        verification_smtp_password=_read_secret("CHECKIN_WEB_VERIFICATION_SMTP_PASSWORD"),
        verification_smtp_from=(
            os.getenv("CHECKIN_WEB_VERIFICATION_SMTP_FROM") or None
        ),
        email_verification_ttl_minutes=_as_int(
            os.getenv("CHECKIN_WEB_EMAIL_VERIFICATION_TTL_MINUTES"), 15, 5, 60
        ),
        email_resend_cooldown_seconds=_as_int(
            os.getenv("CHECKIN_WEB_EMAIL_RESEND_COOLDOWN_SECONDS"), 90, 30, 600
        ),
        email_max_attempts=_as_int(os.getenv("CHECKIN_WEB_EMAIL_MAX_ATTEMPTS"), 4, 1, 8),
        failure_notification_time=os.getenv("CHECKIN_WEB_FAILURE_NOTIFICATION_TIME", "23:00"),
        cloud_ocr_allowed_hosts=tuple(
            item.lower().rstrip(".")
            for item in _split_csv(os.getenv("CHECKIN_WEB_CLOUD_OCR_ALLOWED_HOSTS"), ())
        ),
    )
