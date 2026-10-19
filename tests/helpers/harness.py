from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from management.core.config import Settings
from management.db.base import Base
from management.main import create_app
from tests.fakes import (
    FakeConnectorFactory,
    FakeEmailSender,
    FakeGoogleDriveOAuth,
    FakeGoogleOAuth,
    FakePopperClient,
    FakeWebhookSender,
)

ORIGIN = "http://localhost:3000"
CALLBACK_KEY = "test-popper-callback-key"
APP_URL = "http://localhost:3000"
GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"
GOOGLE_CONNECTIONS_REDIRECT_URI = "http://localhost:8080/api/v1/connections/google/callback"


# A Fernet key for tests only.
CONNECTION_KEY = "dGVzdC1vbmx5LWNvbm5lY3Rpb24tc2VjcmV0LWtleSE="


class Harness:
    def __init__(
        self,
        app: Any,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.app = app
        self.factory = factory
        self.settings = settings
        self.engines = app.state.engines
        self.popper = FakePopperClient()
        app.state.popper_client = self.popper
        self.emails = FakeEmailSender()
        app.state.email_sender = self.emails
        self.google = FakeGoogleOAuth()
        # Only where the settings turn the feature on; elsewhere it stays off.
        if app.state.google_oauth is not None:
            app.state.google_oauth = self.google
        self.google_drive = FakeGoogleDriveOAuth()
        if app.state.google_drive_oauth is not None:
            app.state.google_drive_oauth = self.google_drive
        self.connectors = FakeConnectorFactory()
        app.state.connector_factory = self.connectors
        self.webhooks = FakeWebhookSender()
        app.state.webhook_sender = self.webhooks
        self.transport = ASGITransport(app=app, raise_app_exceptions=False)

    def client(self) -> AsyncClient:
        return AsyncClient(transport=self.transport, base_url=ORIGIN)


@asynccontextmanager
async def open_harness(tmp_path, **overrides: Any) -> AsyncIterator[Harness]:
    settings = Settings(
        # Never read the developer's settings file: it may point at real services.
        _env_file=None,
        storage_backend="local",
        app_env="test",
        storage_local_root=str(tmp_path / "storage"),
        popper_callback_key=CALLBACK_KEY,
        popper_timeout_seconds=1,
        connection_secret_key=CONNECTION_KEY,
        database_url="sqlite+aiosqlite:///:memory:",
        cors_allowed_origins=ORIGIN,
        session_signing_secret="test-session-signing-secret-is-long-enough",
        password_scrypt_log2_n=4,
        # Every test signs several users in from one address; the throttle tests lower these.
        auth_session_rate_limit=1000,
        auth_code_rate_limit=1000,
        **overrides,
    )
    engine = create_async_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        # Run events wake their streams with Postgres NOTIFY; SQLite has nothing to wake.
        connection.create_function("pg_notify", 2, lambda _channel, _payload: None)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    app = create_app(settings, engine=engine, session_factory=factory)
    try:
        yield Harness(app, factory, settings)
    finally:
        await app.state.notification_hub.close()
        await app.state.invite_candidates_hub.close()
        await engine.dispose()
