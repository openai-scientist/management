from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from management.connections.connectors.base import (
    Source,
)


class DatasetVersionItem(BaseModel):
    id: str
    dataset_id: str
    version_number: int
    original_filename: str
    size_bytes: int
    sha256: str = Field(description="SHA-256 of the stored file, as lowercase hex.")
    row_count: int = Field(description="Data rows, not counting the header row.")
    column_names: list[str]
    source_type: Literal["upload", "connection"]
    source: dict[str, Any] | None = Field(
        description=(
            "For a version read from a data connection: `connection_id`, `connection_name`, "
            "`connection_kind`, the `source` that was read and `fetched_at`. They describe "
            "the connection as it was then; it may have been renamed or deleted since. "
            "The `source` of a time series holds the whole form: the metric, the fields "
            "and tags, `start` and `end` in UTC, the bucket and the aggregate. "
            "Null for an upload."
        )
    )
    created_by_user_id: str
    created_at: datetime


class DatasetItem(BaseModel):
    id: str
    project_id: str
    name: str
    description: str | None
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime
    latest_version: DatasetVersionItem | None


class DatasetPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=5000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be cleared")
        return _clean_name(value)


class ImportVersion(BaseModel, extra="forbid"):
    connection_id: UUID
    source: Source = Field(
        description=(
            "One table or view, one SELECT statement, or for a `prometheus` or `influxdb` "
            "connection one metric or measurement over a span of time."
        )
    )


class ImportDataset(ImportVersion):
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=5000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _clean_name(value)


def _clean_name(value: str) -> str:
    value = value.strip()
    if len(value) < 2:
        raise ValueError("name must contain at least two non-space characters")
    return value
