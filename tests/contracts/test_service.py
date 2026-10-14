from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from management.research.contracts import Capabilities, TelemetryRecord
from management.research.contracts.paths import ENGINE_RUN_GATE


def capabilities(**kwargs):
    return Capabilities.model_validate(
        dict(
            engine_key="i2h",
            engine_version="test",
            contract_version="1.0",
            stage_keys=["hypothesis"],
            output_schemas=[{"schema": "i2h.hypotheses/v1", "primary": True}],
            steps=[{"step_key": "draft", "label": "Draft", "order": 1}],
            auto_revision=False,
            profiles=[{}],
        )
        | kwargs
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profiles": [{"domain": "ml"}]},
        {"output_schemas": [{"schema": "i2h.hypotheses/v1"}]},
        {"activity_types": ["exd.critic.objection"]},
        {"integrations": ["search"], "optional_integrations": ["search"]},
    ],
)
def test_capabilities_reject_invalid_declarations(kwargs):
    with pytest.raises(ValidationError):
        capabilities(**kwargs)


def test_capabilities_providers_and_profile_support():
    caps = capabilities(profiles=[{}, {"domain": "ml"}])
    assert caps.providers == ["openai_compatible"]
    from management.research.contracts import ResearchProfile

    assert caps.supports(ResearchProfile(domain="unknown"))
    assert not caps.exact_support(ResearchProfile(domain="unknown"))
    assert caps.exact_support(ResearchProfile(domain="ml"))


def test_telemetry_message_limit_and_research_run_id_rejected():
    data = dict(
        occurred_at=datetime.now(UTC),
        level="INFO",
        message="ok",
        platform_run_id="p",
        stage_run_id="s",
        engine_run_id="e",
    )
    assert TelemetryRecord(**data).platform_run_id == "p"
    for override in ({"message": "x" * 501}, {"research_run_id": "wrong"}):
        with pytest.raises(ValidationError):
            TelemetryRecord(**(data | override))


def test_paths_format():
    assert ENGINE_RUN_GATE.format(engine_run_id="e1", gate_id="g1") == "/runs/e1/gates/g1"
