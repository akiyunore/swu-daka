from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from sqlite3 import Connection

from fastapi import Depends, HTTPException, Request, status

from app.dependencies import get_db
from core.config import get_settings
from core.security import hash_token


@dataclass(frozen=True)
class UserContext:
    id: int
    username: str
    csrf_token: str
    expires_at: str


def require_user(request: Request, db: Connection = Depends(get_db)) -> UserContext:
    token = request.cookies.get(get_settings().user_session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要用户登录")
    row = db.execute(
        """SELECT u.id, u.username, u.active, s.csrf_token, s.expires_at
           FROM user_sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ?""",
        (hash_token(token),),
    ).fetchone()
    if (
        not row
        or not row["active"]
        or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户会话已失效")
    return UserContext(int(row["id"]), row["username"], row["csrf_token"], row["expires_at"])


def require_user_csrf(request: Request, user: UserContext = Depends(require_user)) -> UserContext:
    provided = request.headers.get("X-CSRF-Token", "")
    if not provided or not hmac.compare_digest(provided, user.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    return user
