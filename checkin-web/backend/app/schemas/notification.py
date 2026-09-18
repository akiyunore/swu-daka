import re

from pydantic import BaseModel, Field, field_validator

_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        raise ValueError("请输入有效的邮箱地址")
    return email


class NotificationEmailUpdate(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class NotificationVerify(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class NotificationSettingsUpdate(BaseModel):
    notify_on_failure: bool
