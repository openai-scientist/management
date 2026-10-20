from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

ProviderKey = Literal["slack", "teams", "discord"]
IntegrationName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^\x00]+$"),
]
WebhookUrl = Annotated[str, StringConstraints(max_length=4096)]


class CatalogProvider(BaseModel):
    key: ProviderKey
    label: str
    url_placeholder: str
    setup_steps: list[str]
    docs_url: str


class CatalogEvent(BaseModel):
    key: str
    label: str
    description: str
    default: bool = Field(description="Ticked when a new integration is added.")


class CatalogLimits(BaseModel):
    max_per_project: int


class IntegrationCatalog(BaseModel):
    providers: list[CatalogProvider]
    events: list[CatalogEvent]
    limits: CatalogLimits


class CreateIntegration(BaseModel, extra="forbid"):
    provider: ProviderKey
    name: IntegrationName
    webhook_url: WebhookUrl
    events: list[str] = Field(description="Event keys from the catalog; at least one.")
    enabled: bool = True


class UpdateIntegration(BaseModel, extra="forbid"):
    name: IntegrationName | None = None
    webhook_url: WebhookUrl | None = Field(
        default=None, description="Leave out to keep the saved URL."
    )
    events: list[str] | None = None
    enabled: bool | None = Field(
        default=None,
        description="false pauses the integration; true turns it on again, also after an error.",
    )


class IntegrationError(BaseModel):
    code: str
    message: str
    at: datetime | None


class IntegrationItem(BaseModel):
    id: str
    project_id: str
    provider: ProviderKey
    name: str
    url_hint: str = Field(description="Host and the URL's last characters; the URL is secret.")
    events: list[str]
    status: Literal["active", "paused", "error"]
    last_delivered_at: datetime | None
    last_error: IntegrationError | None
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime


class WebhookTest(BaseModel, extra="forbid"):
    provider: ProviderKey
    webhook_url: WebhookUrl


class WebhookTestFailure(BaseModel):
    code: str
    message: str


class WebhookTestResult(BaseModel):
    ok: bool
    error: WebhookTestFailure | None = Field(
        default=None, description="Why the provider did not take the test message."
    )


class DeliveryItem(BaseModel):
    id: str
    event: str
    status: Literal["pending", "sending", "sent", "failed"]
    attempts: int
    response_code: int | None
    error_code: str | None
    error_message: str | None = Field(description="Plain-English meaning of `error_code`.")
    created_at: datetime
    sent_at: datetime | None
