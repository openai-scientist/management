from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from management.connections.connectors.base import (
    Source,
)
from management.core.responses import ApiResponse, ResponseMeta


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


class UploadResult(BaseModel):
    outcome: Literal["created_dataset", "created_version", "created_copy"] = Field(
        description=(
            "`created_version`: the name was taken and `data.same_name` is `version`, so the "
            "file became the next version of that dataset. `created_copy`: `same_name` is "
            "`copy`, so a new dataset was made under a free name such as `Scores (2)`."
        )
    )
    dataset_id: str
    version_id: str


class DatasetUploadMeta(ResponseMeta):
    upload: UploadResult


class DatasetUploadResponse(ApiResponse[DatasetItem]):
    meta: DatasetUploadMeta


class DatasetPreview(BaseModel):
    dataset_id: str
    version_id: str
    columns: list[str] = Field(description="In file order.")
    rows: list[list[str]] = Field(
        description="Each row has one string per column; an empty cell is an empty string."
    )
    truncated: bool = Field(description="True when the version has more rows than are shown.")
    masking_applied: bool = Field(
        description="True when `data.pii` and `data.mask` are both on and the rows were masked."
    )
    masked_cells: int = Field(description="Cells replaced by `[REDACTED]`.")
    detection_status: Literal["ready", "off"] = Field(
        description="`ready`: detection ran on these rows. `off`: no masking was asked for."
    )


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
