from pydantic import BaseModel


class InviteRead(BaseModel):
    label: str | None = None
    expires_at: str
    valid: bool = True
