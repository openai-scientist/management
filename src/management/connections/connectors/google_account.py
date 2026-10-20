"""A Google account's access, kept for its owner to pick spreadsheets and folders with.

It is not a source: nothing is read through it. Each spreadsheet or folder picked becomes a
connection of its own, holding a copy of this access.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from management.connections.connectors.base import (
    Column,
    ConnectorError,
    RowStream,
    TableRef,
)
from management.connections.connectors.google_api import GoogleAccount, get_json
from management.connections.connectors.google_drive import API_URL
from management.connections.google_drive_oauth import GoogleDriveOAuth

NO_DRIVE = "This Google account did not give access to its Google Drive files"


class GoogleAccountConnector:
    def __init__(
        self,
        secret: dict[str, Any],
        *,
        oauth: GoogleDriveOAuth,
        connect_timeout: float,
        query_timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._account = GoogleAccount(
            secret["refresh_token"],
            oauth=oauth,
            connect_timeout=connect_timeout,
            query_timeout=query_timeout,
            transport=transport,
        )

    async def test(self) -> None:
        """The access works and reaches Drive: one file is asked for, none is read."""
        async with await self._account.client() as client:
            await get_json(
                client,
                API_URL,
                {
                    "q": "trashed = false",
                    "fields": "files(id)",
                    "pageSize": "1",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                },
                no_access=NO_DRIVE,
                bad_request=ConnectorError("unreachable"),
            )

    async def list_schemas(self) -> list[str]:
        raise ConnectorError("unsupported_source")

    async def list_tables(self, schema: str, *, search: str | None, limit: int) -> list[TableRef]:
        raise ConnectorError("unsupported_source")

    async def list_columns(self, schema: str, table: str) -> list[Column]:
        raise ConnectorError("unsupported_source")

    @asynccontextmanager
    async def open_rows(self, source: Any, *, max_rows: int | None) -> AsyncIterator[RowStream]:
        raise ConnectorError("unsupported_source")
        yield  # pragma: no cover
