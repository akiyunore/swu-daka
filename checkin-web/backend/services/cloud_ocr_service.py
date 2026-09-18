import ipaddress
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from sqlite3 import Connection
from urllib.parse import urlsplit, urlunsplit

from core.config import get_settings
from core.security import decrypt_cloud_api_key, encrypt_cloud_api_key

DEFAULT_CLOUD_OCR_BASE_URL = "https://api.xiaomimimo.com/v1/chat/completions"
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")
_USAGE_PAIR_PATTERN = re.compile(r"([a-z_]+)=([^\s]+)")
_MAX_USAGE_VALUE = 1_000_000_000
_USAGE_RETENTION_DAYS = 90


def _resolve_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Base URL 域名无法解析") from exc
    addresses = sorted({record[4][0].split("%", 1)[0] for record in records})
    if not addresses:
        raise ValueError("Base URL 域名没有可用地址")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("Base URL 域名解析结果无效") from exc
        if not address.is_global:
            raise ValueError("Base URL 域名解析到了本机、内网或保留地址")
    return tuple(addresses)


def _validate_base_url(value: str) -> tuple[str, tuple[str, ...]]:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Base URL 格式无效") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Base URL 必须是有效的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL 不能包含账号、密码、查询参数或片段")
    hostname = parsed.hostname.lower().rstrip(".")
    allowed_hosts = get_settings().cloud_ocr_allowed_hosts
    if allowed_hosts and hostname not in allowed_hosts:
        raise ValueError("Base URL 主机不在部署允许列表中")
    if (
        hostname in {"localhost", "localhost.localdomain"}
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise ValueError("Base URL 不能指向本机地址")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Base URL 不能指向本机或内网地址")
    if port not in (None, 443):
        raise ValueError("Base URL 只允许使用 HTTPS 443 端口")
    resolved_addresses = (str(address),) if address is not None else _resolve_public_addresses(hostname, 443)
    host_for_url = f"[{hostname}]" if address is not None and address.version == 6 else hostname
    netloc = host_for_url if port is None else f"{host_for_url}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, "", "")), resolved_addresses


def _normalize_base_url(value: str) -> str:
    return _validate_base_url(value)[0]


def get_cloud_ocr_settings(db: Connection) -> dict:
    row = db.execute(
        "SELECT enabled, base_url, model, encrypted_api_key, updated_at FROM cloud_ocr_settings WHERE id = 1"
    ).fetchone()
    if not row:
        return {
            "enabled": False,
            "base_url": DEFAULT_CLOUD_OCR_BASE_URL,
            "model": "mimo-v2.5",
            "api_key_configured": False,
            "updated_at": None,
        }
    return {
        "enabled": bool(row["enabled"]),
        "base_url": row["base_url"] or DEFAULT_CLOUD_OCR_BASE_URL,
        "model": row["model"],
        "api_key_configured": bool(row["encrypted_api_key"]),
        "updated_at": row["updated_at"],
    }


def update_cloud_ocr_settings(
    db: Connection,
    admin_id: int,
    *,
    enabled: bool,
    base_url: str,
    model: str,
    api_key: str | None,
    clear_api_key: bool,
) -> dict:
    base_url, resolved_addresses = _validate_base_url(base_url)
    model = model.strip()
    if clear_api_key and api_key is not None:
        raise ValueError("不能同时清除并设置 API Key")
    if not _MODEL_PATTERN.fullmatch(model):
        raise ValueError("模型名称格式无效")
    existing = db.execute(
        "SELECT encrypted_api_key FROM cloud_ocr_settings WHERE id = 1"
    ).fetchone()
    encrypted_key = existing["encrypted_api_key"] if existing else None
    if clear_api_key:
        encrypted_key = None
    if api_key is not None:
        clean_key = api_key.strip()
        if not clean_key:
            raise ValueError("API Key 不能为空")
        encrypted_key = encrypt_cloud_api_key(clean_key)
    if enabled and not encrypted_key:
        raise ValueError("启用云端大模型前必须配置 API Key")
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO cloud_ocr_settings
           (id, enabled, provider, base_url, resolved_addresses, model, encrypted_api_key, updated_at, updated_by_admin_id)
           VALUES (1, ?, 'direct', ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled,
               provider = 'direct', base_url = excluded.base_url,
               resolved_addresses = excluded.resolved_addresses, model = excluded.model,
               encrypted_api_key = excluded.encrypted_api_key,
               updated_at = excluded.updated_at,
               updated_by_admin_id = excluded.updated_by_admin_id""",
        (int(enabled), base_url, json.dumps(resolved_addresses), model, encrypted_key, now, admin_id),
    )
    db.commit()
    return get_cloud_ocr_settings(db)


def cloud_ocr_environment(db: Connection) -> dict[str, str]:
    row = db.execute(
        "SELECT enabled, base_url, resolved_addresses, model, encrypted_api_key FROM cloud_ocr_settings WHERE id = 1"
    ).fetchone()
    if not row:
        return {}
    if not row["enabled"]:
        return {"CLOUD_OCR_ENABLED": "0"}
    if not row["encrypted_api_key"]:
        raise RuntimeError("云端大模型已启用但 API Key 缺失")
    normalized_url, current_addresses = _validate_base_url(
        row["base_url"] or DEFAULT_CLOUD_OCR_BASE_URL
    )
    try:
        stored_addresses = tuple(json.loads(row["resolved_addresses"])) if row["resolved_addresses"] else ()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("云端 OCR DNS 校验记录损坏，请管理员重新保存配置") from exc
    if any(not isinstance(value, str) for value in stored_addresses):
        raise RuntimeError("云端 OCR DNS 校验记录损坏，请管理员重新保存配置")
    if stored_addresses and set(stored_addresses) != set(current_addresses):
        raise RuntimeError("云端 OCR 域名解析结果已变化，请管理员重新保存并确认配置")
    if not stored_addresses:
        db.execute(
            "UPDATE cloud_ocr_settings SET resolved_addresses = ? WHERE id = 1",
            (json.dumps(current_addresses),),
        )
        db.commit()
    return {
        "CLOUD_OCR_ENABLED": "1",
        "CLOUD_OCR_BASE_URL": normalized_url,
        "CLOUD_OCR_MODEL": row["model"],
        "CLOUD_OCR_API_KEY": decrypt_cloud_api_key(row["encrypted_api_key"]),
        "CLOUD_OCR_RESOLVED_ADDRESSES": json.dumps(current_addresses),
        "CLOUD_OCR_AUDIT_HOST": urlsplit(normalized_url).hostname or "unknown",
        "CLOUD_OCR_AUDIT_MODEL": row["model"],
    }


def record_cloud_ocr_usage(
    db: Connection,
    *,
    user_id: int | None,
    context: str,
    output: str,
    status: str,
    base_url_host: str | None = None,
    model: str | None = None,
) -> None:
    usage_line = next(
        (line for line in reversed(output.splitlines()) if line.startswith("[OCR_USAGE] ")),
        None,
    )
    if not usage_line:
        return
    values = dict(_USAGE_PAIR_PATTERN.findall(usage_line))
    try:
        cloud_calls = max(0, int(values.get("cloud_calls", "0")))
    except ValueError:
        return
    if cloud_calls == 0:
        return
    base_host = base_url_host or values.get("cloud_base_host") or "unknown"
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", base_host):
        base_host = "unknown"
    model = model or "unknown"

    def number(key: str) -> int:
        try:
            return max(0, min(_MAX_USAGE_VALUE, int(values.get(key, "0"))))
        except (ValueError, OverflowError):
            return 0

    db.execute(
        """INSERT INTO cloud_ocr_calls
           (user_id, context, base_url_host, model, status, cloud_calls,
            prompt_tokens, image_tokens, completion_tokens, reasoning_tokens,
            total_tokens, latency_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            context,
            base_host,
            model,
            status,
            min(cloud_calls, _MAX_USAGE_VALUE),
            number("prompt_tokens"),
            number("image_tokens"),
            number("completion_tokens"),
            number("reasoning_tokens"),
            number("total_tokens"),
            number("mimo_latency_ms"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    retention_cutoff = datetime.now(timezone.utc) - timedelta(days=_USAGE_RETENTION_DAYS)
    db.execute(
        "DELETE FROM cloud_ocr_calls WHERE created_at < ?",
        (retention_cutoff.isoformat(),),
    )
    db.commit()


def list_cloud_ocr_calls(db: Connection, limit: int = 500) -> list[dict]:
    rows = db.execute(
        """SELECT c.id, c.user_id, u.display_name, u.student_no, c.context,
                  c.base_url_host, c.model, c.status, c.cloud_calls,
                  c.prompt_tokens, c.image_tokens, c.completion_tokens,
                  c.reasoning_tokens, c.total_tokens, c.latency_ms, c.created_at
           FROM cloud_ocr_calls c
           LEFT JOIN users u ON u.id = c.user_id
           ORDER BY c.id DESC LIMIT ?""",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(row) for row in rows]
