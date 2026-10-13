from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from platform_be.contracts import ArtifactMeta, Capabilities, Health, TelemetryRecord
from platform_be.contracts.paths import ENGINE_RUN_GATE


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
    from platform_be.contracts import ResearchProfile

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


def test_artifact_metadata_roundtrip():
    meta = ArtifactMeta(
        schema="i2h.hypotheses/v1",
        version=1,
        filename="hypotheses.json",
        media_type="application/json",
        sha256="0" * 64,
    )
    assert meta.metadata == {}
    assert ArtifactMeta.model_validate_json(meta.model_dump_json(by_alias=True)) == meta


def test_health_details_optional():
    health = Health(status="ok", engine_version="test", contract_version="1.0", active_runs=0)
    assert health.checks is None and health.last_error is None


def test_paths_format():
    assert ENGINE_RUN_GATE.format(engine_run_id="e1", gate_id="g1") == "/runs/e1/gates/g1"
