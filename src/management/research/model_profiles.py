from pydantic import SecretStr
from sqlalchemy import select

from management.core.errors import APIError
from management.research.contracts import ModelConfig, Models
from management.research.models import IntegrationSecret, ModelProfile


def require_secret_box(box):
    if box is None:
        raise APIError(503, "SECRET_KEY_NOT_CONFIGURED", "Secret encryption is not configured")
    return box


async def load_models(db, box) -> Models:
    profiles = {
        row.role: row
        for row in await db.scalars(select(ModelProfile).where(ModelProfile.scope == "global"))
    }
    if set(profiles) != {"stage", "review"}:
        raise APIError(409, "MODELS_NOT_CONFIGURED", "Configure both stage and review models")
    models = {}
    for role, row in profiles.items():
        key = (
            require_secret_box(box).open(row.api_key_encrypted)["api_key"]
            if row.api_key_encrypted
            else None
        )
        models[role] = ModelConfig(
            provider=row.provider,
            base_url=row.base_url,
            model=row.model,
            api_key=key,
            extra=row.extra,
        )
    return Models(**models)


async def load_integrations(db, box) -> dict[str, SecretStr]:
    return {
        row.name: SecretStr(require_secret_box(box).open(row.value_encrypted)["value"])
        for row in await db.scalars(select(IntegrationSecret))
    }
