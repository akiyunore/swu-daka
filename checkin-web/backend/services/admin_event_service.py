import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from core.config import get_settings
from core.security import hash_token
from storage.database import connect

logger = logging.getLogger(__name__)

MAX_ADMIN_EVENT_STREAMS = 8
MAX_ADMIN_EVENT_STREAMS_PER_SESSION = 2
ADMIN_EVENT_QUEUE_SIZE = 1


@dataclass(frozen=True)
class AdminEventSubscription:
    subscription_id: int
    token_hash: str
    queue: asyncio.Queue[str]


class AdminEventBroker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscriptions: dict[int, AdminEventSubscription] = {}
        self._next_id = 1
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready_event: asyncio.Event | None = None
        self._healthy = False

    async def start(self) -> None:
        with connect(get_settings().database_path) as db:
            db.execute("SELECT 1").fetchone()
        async with self._lock:
            if self._task and not self._task.done():
                return
            self._stop_event = asyncio.Event()
            self._ready_event = asyncio.Event()
            self._healthy = False
            self._task = asyncio.create_task(self._watch_database(), name="admin-event-watcher")
            ready_event = self._ready_event
        await asyncio.wait_for(ready_event.wait(), timeout=5)

    async def stop(self) -> None:
        async with self._lock:
            task = self._task
            stop_event = self._stop_event
            self._task = None
            self._stop_event = None
            self._ready_event = None
            self._subscriptions.clear()
        if stop_event:
            stop_event.set()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._healthy = False

    async def subscribe(self, raw_token: str) -> AdminEventSubscription:
        token_hash = hash_token(raw_token)
        if not self.session_is_valid(token_hash):
            raise PermissionError("管理员会话已失效")
        async with self._lock:
            if len(self._subscriptions) >= MAX_ADMIN_EVENT_STREAMS:
                raise OverflowError("管理员实时连接已达上限")
            session_count = sum(
                subscription.token_hash == token_hash
                for subscription in self._subscriptions.values()
            )
            if session_count >= MAX_ADMIN_EVENT_STREAMS_PER_SESSION:
                raise OverflowError("当前管理员会话的实时连接已达上限")
            subscription = AdminEventSubscription(
                self._next_id,
                token_hash,
                asyncio.Queue(maxsize=ADMIN_EVENT_QUEUE_SIZE),
            )
            self._subscriptions[subscription.subscription_id] = subscription
            self._next_id += 1
            return subscription

    async def unsubscribe(self, subscription_id: int) -> None:
        async with self._lock:
            self._subscriptions.pop(subscription_id, None)

    def session_is_valid(self, token_hash: str) -> bool:
        try:
            with connect(get_settings().database_path) as db:
                row = db.execute(
                    "SELECT expires_at FROM admin_sessions WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone()
            return bool(
                row
                and datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)
            )
        except Exception:
            logger.exception("Admin event session validation failed")
            return False

    def status(self) -> dict[str, int | bool]:
        return {
            "healthy": self._healthy,
            "connections": len(self._subscriptions),
            "capacity": MAX_ADMIN_EVENT_STREAMS,
        }

    async def _broadcast_update(self) -> None:
        async with self._lock:
            subscriptions = tuple(self._subscriptions.values())
        for subscription in subscriptions:
            if not subscription.queue.full():
                subscription.queue.put_nowait("update")

    async def _watch_database(self) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            return
        while not stop_event.is_set():
            try:
                with connect(get_settings().database_path) as db:
                    version = int(db.execute("PRAGMA data_version").fetchone()[0])
                    self._healthy = True
                    if self._ready_event:
                        self._ready_event.set()
                    while not stop_event.is_set():
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                        except TimeoutError:
                            pass
                        current = int(db.execute("PRAGMA data_version").fetchone()[0])
                        if current != version:
                            await self._broadcast_update()
                            version = current
            except asyncio.CancelledError:
                raise
            except Exception:
                self._healthy = False
                logger.exception("Admin event database watcher failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass


admin_event_broker = AdminEventBroker()
