from pydantic import BaseModel, Field


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class AdminSessionRead(BaseModel):
    username: str
    csrf_token: str
    expires_at: str


class InviteCreate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    expires_in_hours: int = Field(default=24, ge=1, le=168)


class InviteCreated(BaseModel):
    id: int
    token: str
    registration_path: str
    label: str | None = None
    expires_at: str


class ScheduleUpdate(BaseModel):
    enabled: bool


class UserNoteUpdate(BaseModel):
    note: str | None = Field(default=None, max_length=120)


class UserDeleteConfirm(BaseModel):
    confirm: bool


class CloudOcrUpdate(BaseModel):
    enabled: bool
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=120)
    api_key: str | None = Field(default=None, max_length=512)
    clear_api_key: bool = False
