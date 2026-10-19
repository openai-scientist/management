"""Removes a project and everything recorded under it.

Most tables point at projects with ON DELETE RESTRICT, so rows go child first. The audit
log has no foreign key to projects and keeps its history.
"""

from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from management.connections.models import DataConnection, GoogleConnectionGrant
from management.datasets.models import Dataset, DatasetVersion
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from management.projects.models import (
    Comment,
    Notification,
    NotificationPreference,
    Project,
    ProjectFile,
    ProjectMembership,
    ProjectSetting,
    SpendingAlert,
)
from management.research.models import (
    CostEntry,
    FrameReview,
    PiConsultation,
    PiNote,
    ResearchContext,
    ResearchRun,
    RunArtifact,
    RunEvent,
    RunGate,
    StageDecision,
    StageRun,
)


async def delete_project_rows(db: AsyncSession, project_id: UUID) -> list[str]:
    """Delete the project's rows; return the stored-file keys to remove once committed."""
    runs = select(ResearchRun.id).where(ResearchRun.project_id == project_id)
    stages = select(StageRun.id).where(StageRun.research_run_id.in_(runs))
    datasets = select(Dataset.id).where(Dataset.project_id == project_id)
    integrations = select(ProjectIntegration.id).where(ProjectIntegration.project_id == project_id)

    keys: list[str] = []
    for column, where in (
        (ProjectFile.storage_key, ProjectFile.project_id == project_id),
        (DatasetVersion.storage_key, DatasetVersion.dataset_id.in_(datasets)),
        (RunArtifact.storage_key, RunArtifact.run_id.in_(runs)),
        (FrameReview.request_storage_key, FrameReview.run_id.in_(runs)),
        (FrameReview.decision_storage_key, FrameReview.run_id.in_(runs)),
    ):
        keys.extend(key for key in await db.scalars(select(column).where(where)) if key)

    # Stage runs and decisions point at each other; break those links first.
    await db.execute(
        update(StageRun)
        .where(StageRun.id.in_(stages))
        .values(prior_stage_run_id=None, origin_decision_id=None)
    )
    for statement in (
        delete(Comment).where(Comment.run_id.in_(runs)),
        delete(PiNote).where(PiNote.research_run_id.in_(runs)),
        delete(PiConsultation).where(PiConsultation.research_run_id.in_(runs)),
        delete(StageDecision).where(StageDecision.stage_run_id.in_(stages)),
        delete(RunArtifact).where(RunArtifact.run_id.in_(runs)),
        delete(RunEvent).where(RunEvent.run_id.in_(runs)),
        delete(RunGate).where(RunGate.run_id.in_(runs)),
        delete(FrameReview).where(FrameReview.run_id.in_(runs)),
        delete(StageRun).where(StageRun.id.in_(stages)),
        delete(Notification).where(
            (Notification.project_id == project_id) | Notification.run_id.in_(runs)
        ),
        delete(CostEntry).where(CostEntry.project_id == project_id),
        delete(ResearchRun).where(ResearchRun.project_id == project_id),
        delete(ResearchContext).where(ResearchContext.project_id == project_id),
        delete(DatasetVersion).where(DatasetVersion.dataset_id.in_(datasets)),
        delete(Dataset).where(Dataset.project_id == project_id),
        delete(DataConnection).where(DataConnection.project_id == project_id),
        delete(GoogleConnectionGrant).where(GoogleConnectionGrant.project_id == project_id),
        delete(IntegrationDelivery).where(IntegrationDelivery.integration_id.in_(integrations)),
        delete(ProjectIntegration).where(ProjectIntegration.project_id == project_id),
        delete(ProjectFile).where(ProjectFile.project_id == project_id),
        delete(ProjectSetting).where(ProjectSetting.project_id == project_id),
        delete(NotificationPreference).where(NotificationPreference.project_id == project_id),
        delete(SpendingAlert).where(SpendingAlert.project_id == project_id),
        delete(ProjectMembership).where(ProjectMembership.project_id == project_id),
        delete(Project).where(Project.id == project_id),
    ):
        await db.execute(statement.execution_options(synchronize_session=False))
    # Keys may repeat (a review re-read); remove each file once.
    return list(dict.fromkeys(keys))
