from sqlite3 import Connection

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.dependencies import get_db
from app.schemas.public import InviteRead
from core.rate_limit import enforce_rate_limit
from services.public_service import read_invite

router = APIRouter()


@router.get("/invites/{token}", response_model=InviteRead)
def get_invite(token: str, request: Request, db: Connection = Depends(get_db)) -> InviteRead:
    enforce_rate_limit(request, "invite-check", 60, 15 * 60)
    try:
        row = read_invite(db, token)
        return InviteRead(label=row["label"], expires_at=row["expires_at"])
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
