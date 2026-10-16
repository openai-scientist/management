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


def _config(row, box) -> ModelConfig:
    key = (
        _open(
            box,
            row.api_key_encrypted,
            "MODEL_KEY_UNREADABLE",
            f"The saved API key for the {row.role} model can't be read. "
            "An admin needs to enter it again.",
        )["api_key"]
        if row.api_key_encrypted
        else None
    )
    return ModelConfig(
        provider=row.provider, base_url=row.base_url, model=row.model, api_key=key, extra=row.extra
    )


async def _profiles(db) -> dict[str, ModelProfile]:
    return {
        row.role: row
        for row in await db.scalars(select(ModelProfile).where(ModelProfile.scope == "global"))
    }


async def load_models(db, box) -> Models:
    profiles = await _profiles(db)
    if not {"stage", "review"} <= set(profiles):
        raise APIError(409, "MODELS_NOT_CONFIGURED", "Configure both stage and review models")
    return Models(**{role: _config(profiles[role], box) for role in ("stage", "review")})


async def load_critic_model(db, box) -> ModelConfig:
    """The Critic model: the global `critic` profile, else the `review` profile."""
    profiles = await _profiles(db)
    row = profiles.get("critic") or profiles.get("review")
    if row is None:
        raise APIError(409, "MODELS_NOT_CONFIGURED", "Configure both stage and review models")
    return _config(row, box)


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
