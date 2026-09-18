from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from sqlite3 import Connection

from fastapi import Depends, HTTPException, Request, status

from app.dependencies import get_db
from core.config import get_settings
from core.security import hash_token
from storage.database import connect


@dataclass(frozen=True)
class AdminContext:
    id: int
    username: str
    csrf_token: str
    expires_at: str


def _admin_context(request: Request, db: Connection) -> AdminContext:
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要管理员登录")
    row = db.execute(
        """SELECT a.id, a.username, s.csrf_token, s.expires_at
           FROM admin_sessions s JOIN admin_users a ON a.id = s.admin_user_id
           WHERE s.token_hash = ?""",
        (hash_token(token),),
    ).fetchone()
    if not row or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员会话已失效")
    return AdminContext(int(row["id"]), row["username"], row["csrf_token"], row["expires_at"])


def require_admin(request: Request, db: Connection = Depends(get_db)) -> AdminContext:
    return _admin_context(request, db)


def require_admin_stream(request: Request) -> AdminContext:
    """Authenticate an SSE request without holding a request DB connection open."""
    with connect(get_settings().database_path) as db:
        return _admin_context(request, db)


def require_admin_csrf(request: Request, admin: AdminContext = Depends(require_admin)) -> AdminContext:
    provided = request.headers.get("X-CSRF-Token", "")
    if not provided or not hmac.compare_digest(provided, admin.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    return admin
