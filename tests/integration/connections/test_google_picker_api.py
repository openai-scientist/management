from uuid import uuid4

import pytest
from pydantic import SecretStr

from tests.fakes import access_token_for
from tests.helpers.connections import REFRESH_TOKEN, failure, google_account, with_google
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, create_project


def configure_picker(harness: Harness) -> None:
    harness.settings.google_picker_api_key = SecretStr("test-picker-key")
    harness.settings.google_picker_app_id = "123456789"


def picker_path(project_id: str, account_id: str) -> str:
    return f"{PROJECTS}/{project_id}/connections/{account_id}/picker"


async def picker(client, session: dict, project_id: str, account_id: str):
    return await client.post(
        picker_path(project_id, account_id), headers=mutation_headers(session["csrf_token"])
    )


@pytest.mark.asyncio
async def test_picker_returns_only_short_lived_credentials(google_harness: Harness) -> None:
    harness = google_harness
    configure_picker(harness)
    with_google(harness)
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        account_id = await google_account(harness, client, session, project["id"])
        response = await picker(client, session, project["id"], account_id)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["data"] == {
            "access_token": access_token_for(REFRESH_TOKEN),
            "api_key": "test-picker-key",
            "app_id": "123456789",
        }
        assert "refresh_token" not in response.json()["data"]
        assert "client_secret" not in response.json()["data"]


@pytest.mark.asyncio
async def test_picker_opens_only_the_callers_account_in_its_project(
    google_harness: Harness,
) -> None:
    harness = google_harness
    configure_picker(harness)
    with_google(harness)
    async with harness.client() as client, harness.client() as other:
        owner = await login(harness, client, uid="owner", email="owner@example.com")
        colleague = await login(harness, other, uid="colleague", email="colleague@example.com")
        project = await create_project(client, owner)
        await add_member(client, owner, project["id"], "colleague@example.com", "researcher")
        account_id = await google_account(harness, client, owner, project["id"])
        elsewhere = await create_project(client, owner, name="Other project")
        refreshed = list(harness.google_drive.refreshed)
        for browser, session, project_id, candidate in (
            (other, colleague, project["id"], account_id),
            (client, owner, elsewhere["id"], account_id),
            (client, owner, project["id"], str(uuid4())),
        ):
            assert failure(await picker(browser, session, project_id, candidate))[:2] == (
                404,
                "NOT_FOUND",
            )
        assert harness.google_drive.refreshed == refreshed


@pytest.mark.asyncio
async def test_picker_requires_csrf_contributor_access_and_a_writable_project(
    google_harness: Harness,
) -> None:
    harness = google_harness
    configure_picker(harness)
    with_google(harness)
    async with harness.client() as client, harness.client() as reviewer_client:
        owner = await login(harness, client, uid="owner", email="owner@example.com")
        reviewer = await login(
            harness, reviewer_client, uid="reviewer", email="reviewer@example.com"
        )
        project = await create_project(client, owner)
        await add_member(client, owner, project["id"], "reviewer@example.com", "reviewer")
        account_id = await google_account(harness, client, owner, project["id"])
        refreshed = list(harness.google_drive.refreshed)
        no_csrf = await client.post(picker_path(project["id"], account_id))
        assert no_csrf.status_code == 403
        denied = await picker(reviewer_client, reviewer, project["id"], account_id)
        assert denied.status_code == 403
        await client.post(
            f"{PROJECTS}/{project['id']}/archive", headers=mutation_headers(owner["csrf_token"])
        )
        assert (await picker(client, owner, project["id"], account_id)).status_code == 409
        assert harness.google_drive.refreshed == refreshed


@pytest.mark.asyncio
async def test_picker_reports_missing_config_and_revoked_google_access(
    google_harness: Harness,
) -> None:
    harness = google_harness
    with_google(harness)
    async with harness.client() as client:
        owner = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, owner)
        account_id = await google_account(harness, client, owner, project["id"])
        assert failure(await picker(client, owner, project["id"], account_id))[:2] == (
            503,
            "GOOGLE_PICKER_NOT_CONFIGURED",
        )
        configure_picker(harness)
        harness.google_drive.revoked.add(REFRESH_TOKEN)
        assert failure(await picker(client, owner, project["id"], account_id)) == (
            422,
            "CONNECTION_FAILED",
            "access_revoked",
        )
