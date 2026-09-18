import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from ipaddress import ip_address
from math import ceil

from fastapi import HTTPException, Request, status

_LOCK = threading.Lock()
_MAX_BUCKETS = 4096
_SWEEP_INTERVAL_SECONDS = 60


@dataclass
class _Bucket:
    events: deque[float]
    window_seconds: int
    last_seen: float


_EVENTS: OrderedDict[str, _Bucket] = OrderedDict()
_LAST_SWEEP = 0.0


def client_key(request: Request, scope: str) -> str:
    host = request.client.host if request.client else "unknown"
    forwarded_host = request.headers.get("x-real-ip")
    if forwarded_host:
        try:
            host = ip_address(forwarded_host.strip()).compressed
        except ValueError:
            pass
    return f"{scope}:{host}"


def enforce_rate_limit(request: Request, scope: str, limit: int, window_seconds: int) -> None:
    global _LAST_SWEEP
    key = client_key(request, scope)
    now = time.monotonic()
    with _LOCK:
        if now - _LAST_SWEEP >= _SWEEP_INTERVAL_SECONDS:
            for bucket_key, candidate in list(_EVENTS.items()):
                cutoff = now - candidate.window_seconds
                while candidate.events and candidate.events[0] <= cutoff:
                    candidate.events.popleft()
                if not candidate.events:
                    _EVENTS.pop(bucket_key, None)
            _LAST_SWEEP = now

        bucket = _EVENTS.get(key)
        if bucket is None:
            if len(_EVENTS) >= _MAX_BUCKETS:
                _EVENTS.popitem(last=False)
            bucket = _Bucket(deque(), window_seconds, now)
            _EVENTS[key] = bucket
        else:
            bucket.window_seconds = window_seconds
            bucket.last_seen = now
            _EVENTS.move_to_end(key)

        cutoff = now - window_seconds
        while bucket.events and bucket.events[0] <= cutoff:
            bucket.events.popleft()
        if len(bucket.events) >= limit:
            retry_after = max(1, ceil(bucket.events[0] + window_seconds - now))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍后再试",
                headers={"Retry-After": str(retry_after)},
            )
        bucket.events.append(now)
