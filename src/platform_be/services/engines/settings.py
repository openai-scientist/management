from pydantic import BaseModel, Field, SecretStr


class EngineSettings(BaseModel):
    base_url: str
    service_key: SecretStr
    timeout_seconds: float = Field(30, gt=0)


class PipelineStage(BaseModel):
    stage_key: str = Field(min_length=1)
    engine: str
    loop_back_to: list[str] = Field(default_factory=list)


def validate_pipelines(engines, pipelines, default_pipeline, callback_base) -> None:
    if not engines:
        return
    if not pipelines.get(default_pipeline):
        raise ValueError("default_pipeline must name a nonempty pipeline")
    if not callback_base:
        raise ValueError("public_base_url or engine_callback_base_url is required")
    for name, stages in pipelines.items():
        if not stages:
            raise ValueError(f"pipeline {name} must not be empty")
        seen = set()
        for stage in stages:
            if stage.engine not in engines:
                raise ValueError(f"pipeline {name} refers to unknown engine {stage.engine}")
            if stage.stage_key in seen:
                raise ValueError(f"pipeline {name} has duplicate stage_key {stage.stage_key}")
            if any(target not in seen for target in stage.loop_back_to):
                raise ValueError(f"pipeline {name} loop_back_to must point to an earlier stage")
            seen.add(stage.stage_key)
