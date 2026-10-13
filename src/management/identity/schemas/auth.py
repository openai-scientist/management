from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from management.core.roles import PlatformRole, ProjectRole
from management.identity.models import UserStatus

Password = Field(min_length=8, max_length=128, description="8 to 128 characters.")


Code = Field(pattern=r"^\d{6}$", description="The 6 digits from the email.")


class RegisterRequest(BaseModel):
    email: EmailStr = Field(description="The sign-in name. One account per email.")
    password: str = Password
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Defaults to the part of the email before the @.",
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class EmailRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Code
    password: str = Field(
        min_length=1, max_length=128, description="The password of the account being verified."
    )


class VerifyResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str = Code


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    reset_token: str = Field(min_length=64, max_length=64)
    new_password: str = Password


class PasswordResetGrant(BaseModel):
    reset_token: str = Field(description="One-use token for reset-password, bound to this email.")
    expires_in_seconds: int


class VerificationPending(BaseModel):
    email: str = Field(description="The address from the request.")
    expires_in_seconds: int = Field(description="How long a code works after it is sent.")
    resend_after_seconds: int = Field(
        description=(
            "The wait between two codes. A fixed setting, not the time left for this address."
        )
    )


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Password


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value


class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str | None
    avatar_url: str | None = Field(
        description=(
            "Path of the user's picture on the API origin, or null. It needs the session "
            "cookie and changes whenever the picture does."
        )
    )
    status: UserStatus
    platform_role: PlatformRole = Field(
        description="`user` unless the account is a Platform Admin."
    )
    email_verified: bool = Field(
        description="Always true for a signed-in user: an unverified account cannot sign in."
    )
    must_change_password: bool = Field(
        description=(
            "True while the user still has the temporary password an admin's creation set. "
            "Every endpoint except `me`, `csrf-token`, `change-password` and the two "
            "sign-out endpoints answers `403 PASSWORD_CHANGE_REQUIRED` until it is changed."
        )
    )
    created_at: datetime


class SessionDetails(BaseModel):
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


class SessionResult(BaseModel):
    user: UserProfile
    session: SessionDetails
    csrf_token: str


class MembershipSummary(BaseModel):
    project_id: str
    project_name: str
    role: ProjectRole
    archived: bool


class CurrentUser(BaseModel):
    user: UserProfile
    session: SessionDetails
    memberships: list[MembershipSummary] = Field(
        description="The projects you are a member of and your role in each, newest first."
    )


class CsrfToken(BaseModel):
    csrf_token: str
