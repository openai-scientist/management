from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from management.audit.models import AuditEvent
from management.datasets.models import Dataset
from management.identity.bootstrap_admin import bootstrap_admin
from management.projects.models import Notification, Project, ProjectMembership
from management.research.models import (
    ResearchRun,
    RunEvent,
    RunGate,
    StageDecision,
    StageRun,
)
from tests.fakes.engines import engine_overrides, install_fake_engines, seed_models
from tests.helpers.harness import Harness, open_harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, code, create_project, invite_member
from tests.helpers.research import ready_project, start_run


@pytest.mark.asyncio
async def test_the_owner_hands_the_project_to_an_active_member(harness: Harness) -> None:
    async with (
        harness.client() as owner_client,
        harness.client() as heir_client,
        harness.client() as admin_client,
        harness.client() as outsider_client,
    ):
        owner = await login(harness, owner_client, uid="owner", email="owner@example.com")
        heir = await login(harness, heir_client, uid="heir", email="heir@example.com")
        admin = await login(harness, admin_client, uid="admin", email="admin@example.com")
        outsider = await login(
            harness, outsider_client, uid="outsider", email="outsider@example.com"
        )
        async with harness.client() as other:
            await login(harness, other, uid="pending", email="pending@example.com")
        await bootstrap_admin(
            "admin@example.com", settings=harness.settings, session_factory=harness.factory
        )
        project = await create_project(owner_client, owner)
        url = f"{PROJECTS}/{project['id']}/transfer-ownership"
        await add_member(owner_client, owner, project["id"], "heir@example.com", "reviewer")
        await invite_member(owner_client, owner, project["id"], "pending@example.com", "researcher")

        async def transfer(client, session, user_id: str):
            return await client.post(
                url,
                json={"new_owner_user_id": user_id},
                headers=mutation_headers(session["csrf_token"]),
            )

        async def user_id(email: str) -> str:
            async with harness.factory() as db:
                from management.identity.models import User

                return str(await db.scalar(select(User.id).where(User.email == email)))

        heir_id, pending_id = (
            await user_id("heir@example.com"),
            await user_id("pending@example.com"),
        )
        assert code(await transfer(heir_client, heir, heir_id)) == (403, "ROLE_REQUIRED")
        assert code(await transfer(outsider_client, outsider, heir_id)) == (404, "NOT_FOUND")
        # A Platform Admin manages the project but does not own it.
        assert code(await transfer(admin_client, admin, heir_id)) == (403, "OWNER_REQUIRED")
        assert code(await transfer(owner_client, owner, owner["user"]["id"])) == (
            409,
            "ALREADY_OWNER",
        )
        assert code(await transfer(owner_client, owner, pending_id)) == (
            409,
            "NEW_OWNER_NOT_MEMBER",
        )
        assert code(await transfer(owner_client, owner, await user_id("outsider@example.com"))) == (
            409,
            "NEW_OWNER_NOT_MEMBER",
        )

        moved = await transfer(owner_client, owner, heir_id)
        assert moved.status_code == 200, moved.text
        assert moved.json()["message"] == "Ownership transferred"
        assert moved.json()["data"]["owner_user_id"] == heir_id
        assert moved.json()["data"]["my_role"] == "researcher"

        heir_view = (await heir_client.get(f"{PROJECTS}/{project['id']}")).json()["data"]
        assert heir_view["my_role"] == "project_manager"
        settings_url = f"{PROJECTS}/{project['id']}/settings"
        assert (await heir_client.get(settings_url)).status_code == 200
        assert code(await owner_client.get(settings_url)) == (403, "ROLE_REQUIRED")
        # The former owner can no longer hand it on.
        assert code(await transfer(owner_client, owner, heir_id)) == (403, "ROLE_REQUIRED")

        async with harness.factory() as db:
            managers = await db.scalar(
                select(func.count()).where(
                    ProjectMembership.project_id == UUID(project["id"]),
                    ProjectMembership.role_code == "project_manager",
                )
            )
            event = await db.scalar(
                select(AuditEvent).where(AuditEvent.action == "project.ownership_transferred")
            )
        assert managers == 1
        assert event.details == {
            "from_user_id": owner["user"]["id"],
            "to_user_id": heir_id,
            "to_previous_role": "reviewer",
        }

        archived = await heir_client.post(
            f"{PROJECTS}/{project['id']}/archive",
            headers=mutation_headers(heir["csrf_token"]),
        )
        assert archived.status_code == 200
        assert code(await transfer(heir_client, heir, owner["user"]["id"])) == (
            409,
            "PROJECT_ARCHIVED",
        )


@pytest.mark.asyncio
async def test_deleting_a_project_removes_its_data_and_files(harness: Harness) -> None:
    async with harness.client() as owner_client, harness.client() as member_client:
        owner = await login(harness, owner_client, uid="owner", email="owner@example.com")
        member = await login(harness, member_client, uid="member", email="member@example.com")
        project, version = await ready_project(owner_client, owner)
        project_id = UUID(project["id"])
        url = f"{PROJECTS}/{project['id']}"
        await add_member(owner_client, owner, project["id"], "member@example.com", "researcher")
        storage = Path(harness.settings.storage_local_root)
        stored = [path for path in storage.rglob("*") if path.is_file()]
        assert stored

        started = await start_run(owner_client, owner, project["id"], version["id"])
        assert started.status_code == 201, started.text
        delete_headers = mutation_headers(owner["csrf_token"])
        assert code(
            await member_client.delete(url, headers=mutation_headers(member["csrf_token"]))
        ) == (
            403,
            "ROLE_REQUIRED",
        )
        assert code(await owner_client.delete(url, headers=delete_headers)) == (
            409,
            "PROJECT_HAS_ACTIVE_RUNS",
        )
        async with harness.factory() as db:
            await db.execute(
                update(ResearchRun)
                .where(ResearchRun.project_id == project_id)
                .values(status="failed", pipeline_key=None)
            )
            await db.commit()

        deleted = await owner_client.delete(url, headers=delete_headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["data"] is None
        assert deleted.json()["message"] == "Project deleted"

        assert code(await owner_client.get(url)) == (404, "NOT_FOUND")
        assert code(await member_client.get(url)) == (404, "NOT_FOUND")
        assert not any(path.exists() for path in stored)
        async with harness.factory() as db:
            for model, column in (
                (Project, Project.id),
                (ProjectMembership, ProjectMembership.project_id),
                (Dataset, Dataset.project_id),
                (ResearchRun, ResearchRun.project_id),
                (Notification, Notification.project_id),
            ):
                left = await db.scalar(
                    select(func.count()).select_from(model).where(column == project_id)
                )
                assert left == 0, model.__name__
            # The audit history stays.
            actions = set(
                await db.scalars(
                    select(AuditEvent.action).where(AuditEvent.project_id == project_id)
                )
            )
        assert {"project.created", "project.deleted"} <= actions


@pytest.mark.asyncio
async def test_deleting_a_project_removes_a_run_with_stages_and_decisions(tmp_path) -> None:
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        for fake in install_fake_engines(harness).values():
            fake.scripted()
        async with harness.client() as client:
            session = await login(harness, client, uid="owner", email="owner@example.com")
            headers = mutation_headers(session["csrf_token"])
            project = await create_project(client, session)
            await seed_models(harness, session["user"]["id"])
            started = await client.post(
                f"{PROJECTS}/{project['id']}/runs",
                json={
                    "topic": "Compare two methods",
                    "domains": ["ml"],
                    "interaction_mode": "light",
                },
                headers=headers,
            )
            assert started.status_code == 201, started.text
            run_url = f"{PROJECTS}/{project['id']}/runs/{started.json()['data']['id']}"

            # Accept the first stage, then send the second one back: the new stage run
            # points at the decision that made it, and the decision at the stage it came from.
            for _ in range(2):
                stage = (await client.get(run_url)).json()["data"]["current_stage"]
                base = f"{run_url}/stages/{stage['stage_run_id']}"
                gate = stage["open_gate"]["gate_id"]
                approved = await client.post(
                    f"{base}/gates/{gate}", json={"action": "approve"}, headers=headers
                )
                assert approved.status_code == 200, approved.text
                if stage["stage_key"] == "hypothesis":
                    accepted = await client.post(
                        f"{base}/accept",
                        json={"selection": {"hypothesis_ids": ["H1"]}},
                        headers=headers,
                    )
                    assert accepted.status_code == 200, accepted.text
            sent_back = await client.post(
                f"{base}/send-back",
                json={"target_stage_key": "hypothesis", "note": "Too broad", "artifact_ids": []},
                headers=headers,
            )
            assert sent_back.status_code == 200, sent_back.text

            project_id = UUID(project["id"])
            async with harness.factory() as db:
                assert await db.scalar(
                    select(func.count()).where(StageRun.origin_decision_id.is_not(None))
                )
                await db.execute(
                    update(ResearchRun)
                    .where(ResearchRun.project_id == project_id)
                    .values(status="failed", pipeline_key=None)
                )
                await db.commit()

            deleted = await client.delete(f"{PROJECTS}/{project['id']}", headers=headers)
            assert deleted.status_code == 200, deleted.text
            async with harness.factory() as db:
                for model in (StageRun, StageDecision, RunEvent, RunGate, ResearchRun):
                    left = await db.scalar(select(func.count()).select_from(model))
                    assert left == 0, model.__name__
