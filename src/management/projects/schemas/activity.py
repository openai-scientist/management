from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ActivityCategory = Literal["research", "data", "members", "settings"]


class ActivityActor(BaseModel):
    user_id: str | None
    display_name: str = Field(description="`System` for system events, `Deleted user` if gone")
    avatar_url: str | None
    type: Literal["user", "system"]


class ActivityResource(BaseModel):
    type: str = Field(
        description="research_run, dataset, data_connection, project_file, "
        "project_integration, user or project"
    )
    id: str | None
    display_name: str
    available: bool = Field(description="False when the resource no longer exists; do not link it")


class ActivityChange(BaseModel):
    field: str = Field(description="`section.field`; private fields carry null values")
    before: Any = None
    after: Any = None


class ActivityItem(BaseModel):
    """One readable event. Word the UI from `action`; `summary` is a plain English fallback."""

    id: str
    project_id: str
    category: ActivityCategory
    action: str
    actor: ActivityActor
    resource: ActivityResource
    summary: str
    changes: list[ActivityChange]
    created_at: datetime
