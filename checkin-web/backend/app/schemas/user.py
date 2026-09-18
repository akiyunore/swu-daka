from pydantic import BaseModel, Field, field_validator

from app.schemas.notification import normalize_email


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


class UserRegister(BaseModel):
    invitation_token: str = Field(min_length=16, max_length=256)
    school_username: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    school_password: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    has_agreed_terms: bool

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class ScheduleUpdate(BaseModel):
    enabled: bool
