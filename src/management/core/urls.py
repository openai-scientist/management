from pathlib import PurePosixPath
from uuid import UUID

from fastapi import Request


def api_prefix(request: Request) -> str:
    return request.app.state.settings.api_prefix.rstrip("/")


def avatar_url(prefix: str, user_id: UUID | str, storage_key: str | None) -> str | None:
    """Where the user's avatar is served, or None without one.

    The ``v`` value changes with every upload so a browser never shows a replaced picture.
    """
    if not storage_key:
        return None
    return f"{prefix}/users/{user_id}/avatar?v={avatar_version(storage_key)}"


def avatar_version(storage_key: str) -> str:
    return PurePosixPath(storage_key).stem[:8]
