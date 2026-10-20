import pytest
from pydantic import ValidationError

from management.core.config import Settings


def config(**kwargs):
    return Settings(_env_file=None, **kwargs)


def configured(**kwargs):
    return config(
        engines={
            "i2h": {"base_url": "http://i2h:8000/v1", "service_key": "test-secret"},
            "exd": {"base_url": "http://exd:8000", "service_key": "other-secret"},
        },
        pipelines=kwargs.pop(
            "pipelines",
            {
                "default": [
                    {"stage_key": "hypothesis", "engine": "i2h"},
                    {
                        "stage_key": "experiment_plan",
                        "engine": "exd",
                        "loop_back_to": ["hypothesis"],
                    },
                ]
            },
        ),
        **kwargs,
    )


def test_engines_empty_by_default():
    assert config().engines == {}


def test_pipeline_refers_unknown_engine_rejected():
    with pytest.raises(ValidationError, match="unknown engine"):
        configured(pipelines={"default": [{"stage_key": "hypothesis", "engine": "missing"}]})


def test_loop_back_must_point_backwards():
    with pytest.raises(ValidationError, match="loop_back_to"):
        configured(
            pipelines={
                "default": [
                    {"stage_key": "hypothesis", "engine": "i2h", "loop_back_to": ["hypothesis"]}
                ]
            }
        )


def test_service_key_not_in_repr():
    assert "test-secret" not in repr(configured())


def test_valid_default_pipeline_parses():
    settings = configured(engine_callback_base_url="http://api:8080")
    assert settings.pipelines["default"][1].loop_back_to == ["hypothesis"]
    assert settings.engines["i2h"].base_url.endswith("/v1")
    assert settings.engine_callback_base_url == "http://api:8080"


@pytest.mark.parametrize(
    "pipelines",
    [
        {},
        {"default": []},
        {
            "default": [
                {"stage_key": "same", "engine": "i2h"},
                {"stage_key": "same", "engine": "exd"},
            ]
        },
    ],
)
def test_empty_or_duplicate_pipeline_rejected(pipelines):
    with pytest.raises(ValidationError):
        configured(pipelines=pipelines)
