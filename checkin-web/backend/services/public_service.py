from datetime import datetime, timezone
from sqlite3 import Connection

from core.security import hash_token


def read_invite(db: Connection, token: str):
    row = db.execute(
        """SELECT id, label, expires_at, max_uses, use_count, revoked_at
           FROM invite_tokens WHERE token_hash = ?""",
        (hash_token(token),),
    ).fetchone()
    now = datetime.now(timezone.utc)
    if (
        not row
        or row["revoked_at"]
        or row["use_count"] >= row["max_uses"]
        or datetime.fromisoformat(row["expires_at"]) <= now
    ):
        raise ValueError("邀请 Token 无效、已使用、已撤销或已过期")
    return row
