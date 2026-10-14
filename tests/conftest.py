import asyncio
import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from tests.helpers.harness import (
    APP_URL,
    GOOGLE_CLIENT_ID,
    GOOGLE_CONNECTIONS_REDIRECT_URI,
    Harness,
    open_harness,
)

# Production runs on uvloop and the rest of the suite on asyncio. TLS, timeouts and the
# hand-over between a thread and the loop depend on the loop, so a test that asks for this is
# a coroutine run once on each.
if sys.platform == "win32":
    _uvloop_runner = pytest.param(
        None, marks=pytest.mark.skip(reason="uvloop is not supported on Windows"), id="uvloop"
    )
else:
    import uvloop

    _uvloop_runner = pytest.param(uvloop.run, id="uvloop")


@pytest.fixture(params=[pytest.param(asyncio.run, id="asyncio"), _uvloop_runner])
def run(request):
    return request.param


@pytest_asyncio.fixture
async def harness(tmp_path) -> AsyncIterator[Harness]:
    async with open_harness(tmp_path) as harness:
        yield harness


@pytest_asyncio.fixture
async def google_harness(tmp_path) -> AsyncIterator[Harness]:
    """A harness with sign-in with Google and Google connections turned on.

    Google itself is `harness.google` for sign-in and `harness.google_drive` for Drive access.
    """
    async with open_harness(
        tmp_path,
        app_url=APP_URL,
        google_oauth_client_id=GOOGLE_CLIENT_ID,
        google_oauth_client_secret="test-google-client-secret",
        google_oauth_redirect_uri="http://localhost:8080/api/v1/auth/google/callback",
        google_oauth_connections_redirect_uri=GOOGLE_CONNECTIONS_REDIRECT_URI,
    ) as harness:
        yield harness
