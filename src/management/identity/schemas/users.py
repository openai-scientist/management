from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from management.core.roles import PlatformRole


class UserAdminItem(BaseModel):
    id: str
    email: str
    display_name: str | None
    avatar_url: str | None
    status: str
    platform_role: PlatformRole = Field(
        description="`user` unless the account is a Platform Admin."
    )
    email_verified: bool = Field(
        description=(
            "False until the user has entered the code emailed to them. Until then they "
            "cannot sign in, join a project or become a Platform Admin."
        )
    )
    last_login_at: datetime | None
    invite_sent_at: datetime | None = Field(
        description=(
            "When the Platform last tried to email the sign-in details; null if it never "
            "did. `invite_email_sent` on that response says whether the email went."
        )
    )
    created_by_user_id: str | None = Field(
        description="The admin who created the account; null for a self-registered one."
    )
    created_at: datetime


class UserCreated(UserAdminItem):
    invite_email_sent: bool = Field(
        description=(
            "True when the sign-in details were emailed to the user. False when no email "
            "was asked for, or when sending failed: the account exists either way."
        )
    )


class UserCreate(BaseModel):
    email: EmailStr = Field(description="The sign-in name. One account per email.")
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Defaults to the part of the email before the @.",
    )
    send_email: bool = Field(
        default=True,
        description="Email the sign-in details to the user. With false, pass them on yourself.",
    )


class UserStatusUpdate(BaseModel):
    status: Literal["active", "suspended"]


class PlatformRoleUpdate(BaseModel):
    role: PlatformRole = Field(description="`platform_admin` grants the role; `user` removes it.")
