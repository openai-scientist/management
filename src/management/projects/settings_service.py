"""Reads and saves project settings. Fields missing from the stored row use the defaults."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.service import record_audit
from management.core.config import Settings
from management.core.errors import APIError
from management.projects.models import ProjectSetting
from management.projects.schemas.settings import (
    SECTIONS,
    Capability,
    ProjectSettingsData,
)

# Settings the Platform applies today. Every other stored field is kept for the web app
# but changes nothing yet, and says so in `capabilities`.
SUPPORTED_FIELDS = frozenset(
    {
        "access.inviters",
        "access.default_role",
        "access.expiry_days",
        "access.domains",
        "access.reviewers_see_data",
        "data.types",
        "data.same_name",
        "data.pii",
        "data.mask",
        "spending.capped",
        "spending.limit",
        "spending.warn_at",
        "spending.at_limit",
        "notifications.email_enabled",
    }
)

# File types the Platform can read. The form offers more; those cannot be saved yet.
READABLE_FILE_TYPES = frozenset({"CSV", "Excel"})

# Free-text fields: the audit log records that they changed, never their values.
PRIVATE_FIELDS = frozenset({"domains", "billing_email", "languages", "exclude", "fields"})


def _section_values(stored: dict[str, Any], section: str) -> dict[str, Any]:
    """Stored values over the defaults. A stored field no longer valid falls back to default."""
    model = SECTIONS[section]
    defaults = model().model_dump()
    merged = {**defaults, **{k: v for k, v in stored.get(section, {}).items() if k in defaults}}
    try:
        return model.model_validate(merged).model_dump()
    except ValidationError:
        return defaults


def capabilities() -> dict[str, Capability]:
    return {
        f"{section}.{field}": (
            Capability(supported=True)
            if f"{section}.{field}" in SUPPORTED_FIELDS
            else Capability(supported=False, reason_code="NOT_ENFORCED_YET")
        )
        for section, model in SECTIONS.items()
        for field in model.model_fields
    }


async def _row(db: AsyncSession, project_id: UUID) -> ProjectSetting | None:
    return await db.scalar(
        select(ProjectSetting)
        .where(ProjectSetting.project_id == project_id)
        .execution_options(populate_existing=True)
    )


def _snapshot(row: ProjectSetting | None) -> ProjectSettingsData:
    stored = row.sections if row else {}
    return ProjectSettingsData(
        version=row.version if row else 0,
        updated_at=row.updated_at if row else None,
        updated_by_user_id=str(row.updated_by_user_id) if row and row.updated_by_user_id else None,
        sections={section: _section_values(stored, section) for section in SECTIONS},
        capabilities=capabilities(),
    )


async def get_snapshot(db: AsyncSession, project_id: UUID) -> ProjectSettingsData:
    return _snapshot(await _row(db, project_id))


async def patch_section(
    db: AsyncSession,
    *,
    project_id: UUID,
    section: str,
    version: int,
    values: dict[str, Any],
    actor_user_id: UUID,
    request_id: str | None,
) -> ProjectSettingsData:
    """Save part of one section. The caller holds the project lock (`_manage_project`)."""
    model = SECTIONS.get(section)
    if model is None:
        raise APIError(404, "SETTINGS_SECTION_NOT_FOUND", "This settings section does not exist")
    if not values:
        raise APIError(422, "EMPTY_UPDATE", "Provide at least one setting to change")
    if section == "general" and values.get("visibility") == "organization":
        raise APIError(
            422,
            "SETTING_UNSUPPORTED",
            "Organization-wide visibility is not available yet. Keep the project to members.",
        )
    if section == "spending" and values.get("at_limit") == "pause":
        raise APIError(
            422,
            "SETTING_UNSUPPORTED",
            "Pausing runs at the limit is not available yet. Choose stop or notify.",
        )
    if section == "data" and isinstance(values.get("types"), list):
        unreadable = sorted({str(t) for t in values["types"]} - READABLE_FILE_TYPES)
        if unreadable:
            raise APIError(
                422,
                "SETTING_UNSUPPORTED",
                f"{', '.join(unreadable)} files can't be read yet. Allow CSV or Excel.",
            )

    row = await _row(db, project_id)
    current_version = row.version if row else 0
    if version != current_version:
        raise APIError(
            409,
            "SETTINGS_VERSION_CONFLICT",
            "Someone else saved these settings. Reload the page to see their changes.",
        )

    stored = row.sections if row else {}
    before = _section_values(stored, section)
    try:
        # Objects merge field by field; a list in `values` replaces the stored list.
        after = model.model_validate({**before, **values}).model_dump()
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", "values", *error["loc"])} for error in exc.errors()]
        ) from exc

    changed = sorted(field for field in after if after[field] != before[field])
    if not changed:
        # Saving the same form again keeps the version, so other open tabs stay current.
        return _snapshot(row)
    now = datetime.now(UTC)
    if row is None:
        row = ProjectSetting(project_id=project_id, version=0, sections={}, created_at=now)
        db.add(row)
    # Assign a new dict: an in-place change to a JSON column is not saved.
    row.sections = {**stored, section: after}
    row.version = current_version + 1
    row.updated_by_user_id = actor_user_id
    row.updated_at = now
    record_audit(
        db,
        actor_user_id=actor_user_id,
        action="project.settings_updated",
        resource_type="project",
        resource_id=project_id,
        project_id=project_id,
        request_id=request_id,
        details={
            "section": section,
            "changed_fields": changed,
            "values": {
                field: {"from": before[field], "to": after[field]}
                for field in changed
                if field not in PRIVATE_FIELDS
            },
        },
    )
    await db.flush()
    return _snapshot(row)


async def effective_section(db: AsyncSession, project_id: UUID, section: str) -> dict[str, Any]:
    """One section as the Platform applies it now: stored values over the defaults."""
    row = await _row(db, project_id)
    return _section_values(row.sections if row else {}, section)


def invite_ttl_hours(access: dict[str, Any], settings: Settings) -> int:
    """Invitation lifetime: the project's choice, never above the Platform ceiling."""
    return min(int(access["expiry_days"]) * 24, settings.project_invite_ttl_hours)


async def effective_invite_ttl_hours(db: AsyncSession, project_id: UUID, settings: Settings) -> int:
    return invite_ttl_hours(await effective_section(db, project_id, "access"), settings)


def email_domain_allowed(email: str, domains: list[str]) -> bool:
    """An empty list allows every domain; otherwise the domain must match one exactly.

    `example.com` does not allow `sub.example.com` or `evil-example.com`.
    """
    if not domains:
        return True
    return email.strip().rsplit("@", 1)[-1].lower() in domains
