import httpx
import pytest

from management.core.engine_settings import EngineSettings
from management.research.contracts import CONTRACT_VERSION
from management.research.pi.client import PIClient, PIRejected, PIUnavailable

pytestmark = pytest.mark.unit


def client(handler):
    settings = EngineSettings(base_url="http://pi.test/v1", service_key="k" * 32)
    return PIClient(settings, transport=httpx.MockTransport(handler))


async def test_get_sends_the_service_key_under_the_base_prefix():
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers["X-Service-Key"]
        return httpx.Response(
            200,
            json={"consultation_id": "c1", "status": "running"},
        )

    state = await client(handler).get("c1")
    assert state.status == "running"
    assert seen == {"url": "http://pi.test/v1/consultations/c1", "key": "k" * 32}


async def test_capabilities_reject_another_major_version():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "service_version": "x",
                "contract_version": "9.0.0",
                "kinds": ["charter"],
            },
        )

    with pytest.raises(PIRejected):
        await client(handler).capabilities()
    assert CONTRACT_VERSION.startswith("1.")


@pytest.mark.parametrize("status", [500, 503])
async def test_server_errors_are_unavailable(status):
    with pytest.raises(PIUnavailable):
        await client(lambda request: httpx.Response(status)).get("c1")


async def test_network_error_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(PIUnavailable):
        await client(handler).get("c1")


async def test_client_error_and_bad_body_are_rejected():
    with pytest.raises(PIRejected):
        await client(lambda request: httpx.Response(422)).get("c1")
    with pytest.raises(PIRejected):
        await client(lambda request: httpx.Response(200, json={"nope": 1})).get("c1")
