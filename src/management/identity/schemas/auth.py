from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from management.core.roles import PlatformRole, ProjectRole
from management.identity.models import UserStatus

Code = Field(pattern=r"^\d{6}$", description="The 6 digits from the email.")


class EmailRequest(BaseModel):
    email: EmailStr


class EmailCodeSignIn(BaseModel):
    email: EmailStr
    code: str = Code


class SignInLink(BaseModel):
    token: str = Field(
        min_length=64, max_length=64, description="The token after `#token=` in the emailed link."
    )


class SignInLinkInfo(BaseModel):
    email: str = Field(description="The address the link signs in as, partly hidden.")


class VerificationPending(BaseModel):
    email: str = Field(description="The address from the request.")
    expires_in_seconds: int = Field(description="How long a code works after it is sent.")
    resend_after_seconds: int = Field(
        description=(
            "The wait between two codes. A fixed setting, not the time left for this address."
        )
    )


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
    onboarded: bool = Field(
        description=(
            "False until the user has chosen a name. The app asks for one before anything else."
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
