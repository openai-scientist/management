from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from management.audit.service import record_audit
from management.core.errors import APIError
from management.core.responses import ok
from management.core.sessions import Principal, require_active_csrf, require_platform_admin
from management.db.session import get_db
from management.research.model_profiles import require_secret_box
from management.research.models import IntegrationSecret, ModelProfile

router = APIRouter(prefix="/admin", tags=["model configuration"])


class ModelProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai_compatible", "bedrock"]
    base_url: str | None = None
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    input_usd_per_mtok: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=6)
    output_usd_per_mtok: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=6)


class ModelProfileOut(BaseModel):
    provider: str
    base_url: str | None
    model: str
    extra: dict[str, Any]
    has_api_key: bool
    input_usd_per_mtok: Decimal | None
    output_usd_per_mtok: Decimal | None
    updated_at: datetime


def profile_out(row):
    if row is None:
        return None
    return ModelProfileOut(
        provider=row.provider,
        base_url=row.base_url,
        model=row.model,
        extra=row.extra,
        has_api_key=bool(row.api_key_encrypted),
        updated_at=row.updated_at,
        input_usd_per_mtok=row.input_usd_per_mtok,
        output_usd_per_mtok=row.output_usd_per_mtok,
    )


class IntegrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: SecretStr = Field(min_length=1)


@router.get("/models")
async def get_models(principal: Principal = Depends(require_platform_admin), db=Depends(get_db)):
    profiles = {
        p.role: p
        for p in await db.scalars(select(ModelProfile).where(ModelProfile.scope == "global"))
    }
    return ok({role: profile_out(profiles.get(role)) for role in ("stage", "review", "critic")})


@router.put("/models/{role}", dependencies=[Depends(require_active_csrf)])
async def put_model(
    role: Literal["stage", "review", "critic"],
    body: ModelProfileIn,
    request: Request,
    principal: Principal = Depends(require_platform_admin),
    db=Depends(get_db),
):
    box = require_secret_box(request.app.state.secret_box)
    # Only stage and review must differ; the Critic may share a model with the lead.
    other = (
        await db.scalar(
            select(ModelProfile)
            .where(
                ModelProfile.role == ("review" if role == "stage" else "stage"),
                ModelProfile.scope == "global",
            )
            .with_for_update()
        )
        if role != "critic"
        else None
    )
    if other and (other.provider, other.model) == (body.provider, body.model):
        raise APIError(
            422, "REVIEW_MODEL_SAME_AS_STAGE", "Review model must differ from stage model"
        )
    row = await db.scalar(
        select(ModelProfile)
        .where(ModelProfile.role == role, ModelProfile.scope == "global")
        .with_for_update()
    )
    if row is None:
        row = ModelProfile(role=role, scope="global")
        db.add(row)
    row.provider, row.base_url, row.model, row.extra = (
        body.provider,
        body.base_url,
        body.model,
        body.extra,
    )
    row.input_usd_per_mtok, row.output_usd_per_mtok = (
        body.input_usd_per_mtok,
        body.output_usd_per_mtok,
    )
    if body.api_key is not None and body.api_key.get_secret_value().strip():
        row.api_key_encrypted = box.seal({"api_key": body.api_key.get_secret_value()})
    row.updated_by_user_id = principal.user.id
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="model.updated",
        resource_type="model_profile",
        resource_id=role,
        request_id=getattr(request.state, "request_id", None),
        details={"role": role, "provider": body.provider, "model": body.model},
    )
    await db.flush()
    return ok(profile_out(row))


@router.get("/integrations")
async def get_integrations(
    principal: Principal = Depends(require_platform_admin), db=Depends(get_db)
):
    rows = await db.scalars(select(IntegrationSecret).order_by(IntegrationSecret.name))
    return ok(
        [{"name": row.name, "configured": True, "updated_at": row.updated_at} for row in rows]
    )


@router.put("/integrations/{name}", dependencies=[Depends(require_active_csrf)])
async def put_integration(
    name: str,
    body: IntegrationIn,
    request: Request,
    principal: Principal = Depends(require_platform_admin),
    db=Depends(get_db),
):
    box = require_secret_box(request.app.state.secret_box)
    row = await db.get(IntegrationSecret, name)
    if row is None:
        row = IntegrationSecret(name=name)
        db.add(row)
    row.value_encrypted = box.seal({"value": body.value.get_secret_value()})
    row.updated_by_user_id = principal.user.id
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="integration.updated",
        resource_type="integration",
        resource_id=name,
        request_id=getattr(request.state, "request_id", None),
    )
    await db.flush()
    return ok({"name": name, "configured": True, "updated_at": row.updated_at})


@router.delete("/integrations/{name}", dependencies=[Depends(require_active_csrf)])
async def delete_integration(
    name: str,
    request: Request,
    principal: Principal = Depends(require_platform_admin),
    db=Depends(get_db),
):
    require_secret_box(request.app.state.secret_box)
    row = await db.get(IntegrationSecret, name)
    if row is not None:
        await db.delete(row)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="integration.deleted",
        resource_type="integration",
        resource_id=name,
        request_id=getattr(request.state, "request_id", None),
    )
    return ok({"name": name, "configured": False})
