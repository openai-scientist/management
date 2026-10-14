from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from management.core.roles import ProjectRole

ProjectStatus = Literal[
    "draft", "data_ready", "researching", "needs_review", "completed", "archived"
]


def _clean_name(value: str) -> str:
    value = value.strip()
    if len(value) < 2:
        raise ValueError("name must contain at least two non-space characters")
    return value


def _clean_tags(value: list[str]) -> list[str]:
    tags: list[str] = []
    for tag in value:
        tag = tag.strip()
        if not 1 <= len(tag) <= 40:
            raise ValueError("each tag must have 1 to 40 characters")
        if tag.casefold() not in {existing.casefold() for existing in tags}:
            tags.append(tag)
    return tags


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=5000)
    domain: str | None = Field(default=None, max_length=160, description="Research domain")
    objective: str | None = Field(default=None, max_length=10000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    _name = field_validator("name")(_clean_name)
    _tags = field_validator("tags")(_clean_tags)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=5000)
    domain: str | None = Field(default=None, max_length=160)
    objective: str | None = Field(default=None, max_length=10000)
    tags: list[str] | None = Field(default=None, max_length=20)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be cleared")
        return _clean_name(value)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str]:
        return _clean_tags(value or [])


class ProjectItem(BaseModel):
    id: str
    name: str
    description: str | None
    domain: str | None
    objective: str | None
    tags: list[str]
    status: ProjectStatus = Field(description="Research progress, or `archived` (read-only).")
    owner_user_id: str
    my_role: ProjectRole | None = Field(
        description="Your role in this project; null when you see it as Platform Admin only."
    )
    created_at: datetime
    updated_at: datetime


class ProjectMemberCreate(BaseModel):
    email: EmailStr = Field(description="Email of a user who already has an account.")
    role: ProjectRole


class ProjectMemberRoleUpdate(BaseModel):
    role: ProjectRole


class ProjectMemberItem(BaseModel):
    id: str
    user_id: str
    email: str
    display_name: str | None
    avatar_url: str | None
    role: ProjectRole
    status: Literal["invited", "active"] = Field(
        description="`invited` until the user accepts; an invited user has no access yet."
    )
    invite_sent_at: datetime | None = Field(
        description="When the invitation was last sent; null for a member who was never invited."
    )
    invite_expires_at: datetime | None
    invite_expired: bool = Field(
        description="True for an invitation past its expiry: only then can it be sent again."
    )
    invite_email_sent: bool | None = Field(
        default=None,
        description="Only on the two responses that send the invitation: whether the email went.",
    )
    created_at: datetime


class ProjectInviteCandidate(BaseModel):
    id: str
    email: str
    display_name: str | None
    avatar_url: str | None
