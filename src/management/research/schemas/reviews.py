from pydantic import BaseModel


class ReviewArtifactItem(BaseModel):
    artifact_id: str
    schema_id: str
    version: int


class ReviewRoundItem(BaseModel):
    review_round: int
    artifacts: list[ReviewArtifactItem]
