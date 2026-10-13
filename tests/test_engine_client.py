import httpx
import pytest

from management.core.engine_settings import EngineSettings
from management.core.responses import request_id_context
from management.services.engines.client import (
    EngineClient,
    EngineContractMismatch,
    EngineRejected,
    EngineUnavailable,
)
from tests.engine_fakes import make_request
from tests.fakes.fake_engine import FakeEngine


def client(fake):
    return EngineClient(
        "fake",
        EngineSettings(base_url="http://fake.test", service_key=fake.service_key),
        transport=httpx.ASGITransport(app=fake.app),
    )


async def test_health_and_capabilities_roundtrip():
    fake = FakeEngine()
    engine = client(fake)
    assert (await engine.health()).status == "ok"
    assert (await engine.capabilities()).engine_key == "fake"
    fake.capabilities.engine_version = "changed"
    assert (await engine.capabilities()).engine_version == "test"


async def test_create_run_sends_service_key_request_id_and_real_api_key():
    fake = FakeEngine()
    token = request_id_context.set("request-from-platform")
    try:
        response = await client(fake).create_run(make_request())
    finally:
        request_id_context.reset(token)
    assert response.created
    assert fake.request_headers[0]["x-request-id"] == "request-from-platform"
    assert fake.request_headers[0]["x-service-key"] == fake.service_key
    assert len(fake.request_headers[0]["traceparent"]) == 55
    assert fake.requests[0].models.stage.api_key.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(fake.requests[0])


async def test_unreachable_raises_engine_unavailable():
    fake = FakeEngine()
    fake.set_unreachable(True)
    with pytest.raises(EngineUnavailable):
        await client(fake).health()


async def test_4xx_raises_engine_rejected():
    with pytest.raises(EngineRejected) as info:
        await client(FakeEngine()).get_run("missing")
    assert info.value.status_code == 404 and info.value.code == "RUN_NOT_FOUND"


async def test_base_prefix_preserved_and_events_page():
    paths = []

    async def response(request):
        paths.append(str(request.url))
        return httpx.Response(200, json={"events": []})

    engine = EngineClient(
        "fake",
        EngineSettings(base_url="http://fake.test/v1", service_key="test"),
        transport=httpx.MockTransport(response),
    )
    assert (await engine.events("e", after=3, limit=7)).events == []
    assert paths == ["http://fake.test/v1/runs/e/events?after=3&limit=7"]


async def test_contract_major_mismatch_rejected():
    fake = FakeEngine()
    fake.capabilities.contract_version = "2.0.0"
    with pytest.raises(EngineContractMismatch):
        await client(fake).capabilities()
