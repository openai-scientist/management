"""Project settings sections, shaped exactly like the web app's settings forms.

Keys are the snake_case form of the frontend draft keys and values keep the frontend's
own strings, so the web app can send its form state unchanged. Options and defaults come
from `ai-research-platform-fe/src/app/(main)/projects/[projectId]/_components/`:
`settings-section.tsx` (general), `settings-extra.tsx` (access, models, literature, review,
data) and `settings-system.tsx` (runs, spending). Keep the lists here in step with them.
`notifications` is a Platform section with no web app form yet.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.networks import validate_email
from pydantic_core import PydanticCustomError

NamedModel = Literal[
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "gpt-5", "gemini-2-5-pro"
]
Model = Literal["default"] | NamedModel
FileType = Literal["CSV", "Excel", "Parquet", "JSON"]
Count = Literal["1", "2", "3", "5"]
Tags = Annotated[list[Annotated[str, Field(min_length=1, max_length=120)]], Field(max_length=50)]


def _unique(values: list[Any]) -> list[Any]:
    """Drop repeats, keeping the first one, as the web app's tag inputs do."""
    return list(dict.fromkeys(values))


def _decimal_text(value: str, *, maximum: Decimal | None = None) -> str:
    """A positive amount typed as text, as the web app's number fields keep it."""
    if not re.fullmatch(r"\d+(\.\d+)?", value):
        raise ValueError("Enter a number such as 5 or 12.5")
    amount = Decimal(value)
    if amount <= 0:
        raise ValueError("Enter an amount above 0")
    if maximum is not None and amount > maximum:
        raise ValueError(f"Enter an amount up to {maximum}")
    return value


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralSettings(_Section):
    visibility: Literal["members", "organization"] = "members"
    timezone: str = Field(default="Asia/Ho_Chi_Minh", max_length=64)
    language: Literal["en", "vi"] = "en"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid time zone") from exc
        return value


def _domain(value: str) -> str:
    domain = value.strip().lower()
    if not domain or "@" in domain or " " in domain or "." not in domain:
        raise ValueError("Enter a domain such as example.com, without @")
    return domain


class AccessSettings(_Section):
    inviters: Literal["managers", "researchers"] = "managers"
    default_role: Literal["researcher", "reviewer"] = "researcher"
    expiry_days: Literal["3", "7", "14", "30"] = "7"
    domains: Annotated[
        list[Annotated[str, AfterValidator(_domain)]], Field(max_length=50), AfterValidator(_unique)
    ] = []
    join_link: bool = False
    join_approval: bool = True
    reviewers_see_data: bool = True


class RunsSettings(_Section):
    mode: Literal["auto", "light", "copilot", "full"] = "copilot"
    budget: Annotated[str, AfterValidator(lambda v: _decimal_text(v, maximum=Decimal(20)))] = "5"
    fields: Tags = []
    starters: Literal["researchers", "managers"] = "researchers"
    concurrent: Count = "2"
    rounds: Count = "3"
    stage_limit: Literal["30", "60", "120", "none"] = "60"
    retry: bool = True
    pause_on_failure: bool = True


class ModelsSettings(_Section):
    stage: Model = "default"
    review: Model = "default"
    fallback: Literal["none"] | NamedModel = "none"
    effort: Literal["low", "medium", "high"] = "medium"
    style: Literal["conservative", "balanced", "exploratory"] = "balanced"


def _papers_per_round(value: str) -> str:
    if not value.isdigit() or not 5 <= int(value) <= 200:
        raise ValueError("Enter a whole number from 5 to 200")
    return value


class LiteratureSettings(_Section):
    arxiv: bool = True
    semantic: bool = True
    openalex: bool = True
    pubmed: bool = False
    crossref: bool = False
    years: Literal["3", "5", "10", "any"] = "10"
    preprints: bool = True
    per_round: Annotated[str, AfterValidator(_papers_per_round)] = "40"
    languages: Tags = ["English"]
    exclude: Tags = []

    @model_validator(mode="after")
    def _one_source(self) -> "LiteratureSettings":
        if not any((self.arxiv, self.semantic, self.openalex, self.pubmed, self.crossref)):
            raise ValueError("Keep at least one literature source on")
        return self


class ReviewSettings(_Section):
    approvals: Literal["1", "2", "3"] = "1"
    approvers: Literal["reviewers", "reviewers-managers", "anyone"] = "reviewers"
    self_approval: bool = False
    format: Literal["latex", "markdown", "docx"] = "latex"
    citation: Literal["apa", "ieee", "chicago", "harvard", "vancouver"] = "apa"
    language: Literal["en", "vi", "ja", "fr", "de"] = "en"
    appendix: bool = True
    watermark: bool = True


class DataSettings(_Section):
    pinning: Literal["latest", "pinned"] = "latest"
    same_name: Literal["version", "copy"] = "version"
    # Both file types the Platform reads, so a project that never saved this keeps accepting them.
    types: Annotated[list[FileType], Field(min_length=1), AfterValidator(_unique)] = [
        "CSV",
        "Excel",
    ]
    pii: bool = True
    mask: bool = True
    artifacts: Literal["90", "180", "365", "forever"] = "forever"
    trash: Literal["7", "14", "30"] = "30"
    region: Literal["auto", "us", "eu", "apac"] = "auto"


def _billing_email(value: str) -> str:
    value = value.strip()
    if value:
        try:
            validate_email(value)
        except PydanticCustomError as exc:
            raise ValueError("Enter a valid email address or leave it empty") from exc
    return value


class SpendingSettings(_Section):
    capped: bool = False
    limit: Annotated[str, AfterValidator(_decimal_text)] = "100"
    warn_at: Literal["50", "75", "80", "90"] = "80"
    at_limit: Literal["stop", "pause", "notify"] = "stop"
    billing_email: Annotated[str, Field(max_length=320), AfterValidator(_billing_email)] = ""


class NotificationSettings(_Section):
    """Project Manager's email switch per notice group, for every member of the project.

    Keys match the notice groups of members' preferences. All off until turned on; only
    notices raised while a group is on are emailed. In-app notices are unaffected.
    """

    review: bool = False
    finished: bool = False
    comments: bool = False
    members: bool = False
    budget: bool = False
    integrations: bool = False


SECTIONS: dict[str, type[_Section]] = {
    "general": GeneralSettings,
    "access": AccessSettings,
    "runs": RunsSettings,
    "models": ModelsSettings,
    "literature": LiteratureSettings,
    "review": ReviewSettings,
    "data": DataSettings,
    "spending": SpendingSettings,
    "notifications": NotificationSettings,
}

SettingsSection = Literal[
    "general",
    "access",
    "runs",
    "models",
    "literature",
    "review",
    "data",
    "spending",
    "notifications",
]


class Capability(BaseModel):
    supported: bool = Field(description="True when the Platform applies this setting today.")
    reason_code: str | None = Field(
        default=None, description="Why the setting is stored but not applied yet."
    )


class ProjectSettingsData(BaseModel):
    version: int = Field(description="Send it back on PATCH; 0 until the first save.")
    updated_at: datetime | None
    updated_by_user_id: str | None
    sections: dict[SettingsSection, dict[str, Any]]
    capabilities: dict[str, Capability] = Field(
        description="Keyed `<section>.<field>`; every stored field is listed."
    )


class ProjectSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0, description="The version the client read.")
    values: dict[str, Any] = Field(
        description="Fields of this section to change; arrays replace the stored list."
    )


class AccessPermissions(BaseModel):
    can_invite: bool = Field(description="May invite people; follows `access.inviters`.")
    can_view_invite_candidates: bool = Field(description="Same as `can_invite`.")
    can_manage_members: bool = Field(
        description="May change roles, remove members, resend or cancel invitations."
    )
    can_manage_settings: bool
    can_read_raw_datasets: bool = Field(
        description="May list, preview and download datasets; follows `reviewers_see_data`."
    )


class InvitationDefaults(BaseModel):
    default_role: Literal["researcher", "reviewer"] = Field(
        description="Preselect it in the invite dialog; any allowed role may still be sent."
    )
    allowed_roles: list[Literal["project_manager", "researcher", "reviewer"]]
    allowed_domains: list[str] = Field(description="Empty means any domain.")
    configured_expiry_days: Literal["3", "7", "14", "30"]
    effective_ttl_hours: int = Field(
        description="What an invitation sent now lasts: the setting, capped by the Platform."
    )


class AccessContext(BaseModel):
    settings_version: int
    permissions: AccessPermissions
    invitation: InvitationDefaults | None = Field(description="Null when the caller cannot invite.")
