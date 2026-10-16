from pydantic import SecretStr
from sqlalchemy import select

from management.core.errors import APIError
from management.infra.secret_box import SecretBox, SecretBoxError
from management.research.contracts import ModelConfig, Models
from management.research.models import IntegrationSecret, ModelProfile


def require_secret_box(box):
    if box is None:
        raise APIError(503, "SECRET_KEY_NOT_CONFIGURED", "Secret encryption is not configured")
    return box


def _open(box: SecretBox | None, ciphertext: str, code: str, message: str) -> dict:
    """A secret sealed under an earlier CONNECTION_SECRET_KEY fails; name the one to re-enter."""
    try:
        return require_secret_box(box).open(ciphertext)
    except SecretBoxError:
        raise APIError(409, code, message) from None


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
            _open(
                box,
                row.api_key_encrypted,
                "MODEL_KEY_UNREADABLE",
                f"The saved API key for the {role} model can't be read. "
                "An admin needs to enter it again.",
            )["api_key"]
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
        row.name: SecretStr(
            _open(
                box,
                row.value_encrypted,
                "INTEGRATION_SECRET_UNREADABLE",
                f"The saved secret for the {row.name} integration can't be read. "
                "An admin needs to enter it again.",
            )["value"]
        )
        for row in await db.scalars(select(IntegrationSecret))
    }
