import base64
import json
import ipaddress
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import MIMO_API_TIMEOUT_SECONDS, MIMO_API_URL, MIMO_MODEL


MAX_CLOUD_OCR_RESPONSE_BYTES = 1024 * 1024
MAX_CLOUD_OCR_USAGE_VALUE = 1_000_000_000
_CACHED_RUNTIME_API_KEY: str | None = None


def new_ocr_usage() -> dict[str, int]:
    return {
        "local_attempts": 0,
        "mimo_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "image_tokens": 0,
        "reasoning_tokens": 0,
        "mimo_latency_ms": 0,
    }


def add_mimo_usage(total: dict[str, int], call: dict[str, int]) -> None:
    total["mimo_calls"] += 1
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "image_tokens",
        "reasoning_tokens",
        "mimo_latency_ms",
    ):
        total[key] += max(0, int(call.get(key, 0) or 0))


def format_ocr_usage(usage: dict[str, int]) -> str:
    base_url = os.environ.get("CLOUD_OCR_BASE_URL", MIMO_API_URL).strip()
    base_host = urlsplit(base_url).hostname or "unknown"
    return (
        "[OCR_USAGE] "
        f"local_attempts={usage['local_attempts']} "
        f"mimo_calls={usage['mimo_calls']} "
        f"cloud_base_host={base_host} "
        f"cloud_calls={usage['mimo_calls']} "
        f"prompt_tokens={usage['prompt_tokens']} "
        f"image_tokens={usage['image_tokens']} "
        f"completion_tokens={usage['completion_tokens']} "
        f"reasoning_tokens={usage['reasoning_tokens']} "
        f"total_tokens={usage['total_tokens']} "
        f"mimo_latency_ms={usage['mimo_latency_ms']}"
    )


def _read_mimo_api_key() -> str | None:
    global _CACHED_RUNTIME_API_KEY
    if os.environ.get("CLOUD_OCR_ENABLED") == "0":
        return None
    cloud_key = os.environ.pop("CLOUD_OCR_API_KEY", None)
    if cloud_key:
        _CACHED_RUNTIME_API_KEY = cloud_key
        return _CACHED_RUNTIME_API_KEY
    key_file = os.environ.get("MIMO_API_KEY_FILE")
    if key_file:
        try:
            return Path(key_file).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    legacy_key = os.environ.pop("MIMO_API_KEY", None)
    if legacy_key:
        _CACHED_RUNTIME_API_KEY = legacy_key
    return _CACHED_RUNTIME_API_KEY


def _usage_from_response(data: dict, latency_ms: int) -> dict[str, int]:
    usage = data.get("usage") if isinstance(data, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    def bounded(value) -> int:
        try:
            return max(0, min(MAX_CLOUD_OCR_USAGE_VALUE, int(value or 0)))
        except (TypeError, ValueError, OverflowError):
            return 0

    return {
        "prompt_tokens": bounded(usage.get("prompt_tokens")),
        "completion_tokens": bounded(usage.get("completion_tokens")),
        "total_tokens": bounded(usage.get("total_tokens")),
        "image_tokens": bounded(prompt_details.get("image_tokens")),
        "reasoning_tokens": bounded(completion_details.get("reasoning_tokens")),
        "mimo_latency_ms": bounded(latency_ms),
    }


def _bounded_response_body(response) -> bytes:
    content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if content_length:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > MAX_CLOUD_OCR_RESPONSE_BYTES:
            raise ValueError("cloud OCR response is too large")
    body = bytearray()
    if hasattr(response, "iter_content"):
        chunks = response.iter_content(chunk_size=64 * 1024)
    else:
        chunks = (getattr(response, "content", b""),)
    for chunk in chunks:
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_CLOUD_OCR_RESPONSE_BYTES:
            raise ValueError("cloud OCR response is too large")
    return bytes(body)


def _cloud_endpoint_resolution_is_unchanged(hostname: str) -> bool:
    expected_raw = os.environ.get("CLOUD_OCR_RESOLVED_ADDRESSES")
    if not expected_raw:
        return True
    try:
        expected = {str(ipaddress.ip_address(value)) for value in json.loads(expected_raw)}
        current = {
            str(ipaddress.ip_address(record[4][0].split("%", 1)[0]))
            for record in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(current) and current == expected and all(
        ipaddress.ip_address(value).is_global for value in current
    )


def recognize_captcha_with_mimo(image_bytes: bytes) -> dict:
    """Use the configured OpenAI-compatible endpoint as an optional OCR fallback."""
    api_key = _read_mimo_api_key()
    if not api_key:
        return {"status": "disabled", "code": None, "usage": {}}
    api_url = os.environ.get("CLOUD_OCR_BASE_URL", MIMO_API_URL).strip()
    parsed_url = urlsplit(api_url)
    if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
        return {"status": "invalid_api_url", "code": None, "usage": {}}
    if not _cloud_endpoint_resolution_is_unchanged(parsed_url.hostname):
        return {"status": "invalid_api_url", "code": None, "usage": {}}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        import requests
    except Exception:
        return {"status": "dependency_unavailable", "code": None, "usage": {}}

    image_data = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": os.environ.get("CLOUD_OCR_MODEL", MIMO_MODEL),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict OCR component. Return only the four ASCII digits "
                    "visible in the supplied image, with no spaces or explanation."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_data}"},
                    },
                    {
                        "type": "text",
                        "text": "Read the four-digit number. Output exactly four digits.",
                    },
                ],
            },
        ],
        "max_completion_tokens": 16,
        "stream": False,
    }
    started = time.monotonic()
    response = None
    try:
        response = requests.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=MIMO_API_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        raw_body = _bounded_response_body(response)
        try:
            data = json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        usage = _usage_from_response(data, latency_ms)
        if response.status_code != 200:
            return {
                "status": f"http_{response.status_code}",
                "code": None,
                "usage": usage,
            }
        content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        codes = re.findall(r"(?<!\d)\d{4}(?!\d)", str(content))
        if len(codes) != 1:
            return {"status": "invalid_response", "code": None, "usage": usage}
        return {"status": "success", "code": codes[0], "usage": usage}
    except Exception:
        latency_ms = round((time.monotonic() - started) * 1000)
        return {
            "status": "request_failed",
            "code": None,
            "usage": {"mimo_latency_ms": latency_ms},
        }
    finally:
        close_response = getattr(response, "close", None)
        if callable(close_response):
            close_response()
