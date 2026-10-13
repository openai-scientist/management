import httpx

from platform_be.contracts import CONTRACT_VERSION, RunCreateRequest
from platform_be.services.engines.registry import EngineRegistry
from platform_be.services.engines.settings import EngineSettings
from tests.fakes.fake_engine import FakeEngine


def engine_overrides():
    return dict(
        engines={
            key: {"base_url": f"http://{key}.test", "service_key": "test-service-key"}
            for key in ("i2h", "exd")
        },
        pipelines={
            "default": [
                {"stage_key": "hypothesis", "engine": "i2h"},
                {"stage_key": "experiment_plan", "engine": "exd", "loop_back_to": ["hypothesis"]},
            ]
        },
    )


def make_request(**kwargs):
    return RunCreateRequest.model_validate(
        dict(
            contract_version=CONTRACT_VERSION,
            platform_run_id="p",
            stage_run_id="s",
            stage_key="hypothesis",
            interaction_mode="light",
            budget_usd="10",
            callback_url="http://platform.test/callback",
            models={
                "stage": {
                    "provider": "openai_compatible",
                    "model": "draft",
                    "api_key": "test-secret",
                },
                "review": {
                    "provider": "openai_compatible",
                    "model": "review",
                    "api_key": "test-secret",
                },
            },
        )
        | kwargs
    )


def install_fake_engines(harness):
    from platform_be.services.engines.client import EngineClient

    fakes = {
        "i2h": FakeEngine(
            engine_key="i2h",
            output_schema="i2h.hypotheses/v1",
            auto_revision=False,
            callback_transport=harness.transport,
        ),
        "exd": FakeEngine(
            engine_key="exd",
            stage_keys=["experiment_plan"],
            input_schemas=["i2h.hypotheses/v1"],
            output_schema="exd.exp_plan/v1",
            callback_transport=harness.transport,
        ),
    }
    registry = EngineRegistry(
        {
            key: EngineClient(
                key,
                EngineSettings(**engine_overrides()["engines"][key]),
                transport=httpx.ASGITransport(app=fake.app),
            )
            for key, fake in fakes.items()
        }
    )
    harness.app.state.engines = registry
    harness.engines = registry
    return fakes
