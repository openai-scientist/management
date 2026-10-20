import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from tests.fakes import FakeGoogleDrive, access_token_for
from tests.helpers.audit import audit_events, every_audit_detail
from tests.helpers.connections import (
    EMAIL,
    REFRESH_TOKEN,
    SPREADSHEET_ID,
    SPREADSHEET_URL,
    SUBJECT,
    TITLE,
    connect_account,
    connections,
    failure,
    google_grant,
    grant_ids,
    preview,
    stored_token,
    with_google,
)
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, create_project

NEW_REFRESH_TOKEN = "second-refresh-token-never-shown-either"
ANSWERS = {"type": "table", "schema": TITLE, "name": "Answers"}


def with_account(harness: Harness) -> FakeGoogleDrive:
    """The fake Drive and Sheets APIs, both open to the two test tokens, and Google Picker."""
    drive = with_google(harness)
    drive.sheets.spreadsheets[SPREADSHEET_ID].readers.add(NEW_REFRESH_TOKEN)
    harness.settings.google_picker_api_key = SecretStr("test-picker-key")
    harness.settings.google_picker_app_id = "123456789"
    return drive


async def post(client: AsyncClient, session: dict, url: str, body: dict | None = None):
    return await client.post(url, json=body, headers=mutation_headers(session["csrf_token"]))


def sheet_body(account_id: str, name: str = "Survey") -> dict:
    return {
        "name": name,
        "kind": "google_sheets",
        "config": {"spreadsheet": SPREADSHEET_URL},
        "account_id": account_id,
    }


async def signed_in_owner(harness: Harness, client: AsyncClient):
    session = await login(harness, client, uid="owner", email="owner@example.com")
    project = await create_project(client, session)
    grant_id = await google_grant(harness, client, project["id"])
    created = await connect_account(client, session, project["id"], grant_id)
    assert created.status_code == 201, created.text
    return session, project, created.json()["data"]


@pytest.mark.asyncio
async def test_an_account_is_connected_once_and_kept_for_its_owner(google_harness: Harness) -> None:
    harness = google_harness
    with_account(harness)
    async with harness.client() as client:
        session, project, account = await signed_in_owner(harness, client)
        url = f"{PROJECTS}/{project['id']}/connections/{account['id']}"

        # Nothing is chosen, and the account itself is not a source.
        assert (account["name"], account["kind"]) == ("Google account", "google")
        assert account["config"] == {"account_email": EMAIL, "google_subject": SUBJECT}
        assert failure(await preview(client, session, url, ANSWERS)) == (
            422,
            "CONNECTION_IS_ACCOUNT",
            None,
        )

        # The same account again renews it instead of adding another.
        grant_id = await google_grant(
            harness, client, project["id"], refresh_token=NEW_REFRESH_TOKEN
        )
        again = await connect_account(client, session, project["id"], grant_id)
        assert again.status_code == 200, again.text
        assert again.json()["data"]["id"] == account["id"]

        # Another account is refused until the first one is disconnected.
        grant_id = await google_grant(harness, client, project["id"], subject="google-sub-2")
        other = await connect_account(client, session, project["id"], grant_id)
        assert failure(other) == (409, "GOOGLE_ACCOUNT_CONNECTED", None)
        removed = await client.delete(url, headers=mutation_headers(session["csrf_token"]))
        assert removed.status_code == 200, removed.text
        replaced = await connect_account(client, session, project["id"], grant_id)
        assert replaced.status_code == 201, replaced.text
        assert replaced.json()["data"]["config"]["google_subject"] == "google-sub-2"

    [row] = await connections(harness)
    assert stored_token(row) == REFRESH_TOKEN
    assert await grant_ids(harness) == set()
    assert EMAIL not in await every_audit_detail(harness)
    renewals = await audit_events(harness, "connection.reauthorized")
    assert [event.details for event in renewals] == [{"kind": "google"}]


@pytest.mark.asyncio
async def test_files_are_picked_with_the_saved_account_for_the_whole_team(
    google_harness: Harness,
) -> None:
    harness = google_harness
    with_account(harness)
    async with harness.client() as client, harness.client() as member_client:
        session, project, account = await signed_in_owner(harness, client)
        member = await login(harness, member_client, uid="member", email="member@example.com")
        await add_member(client, session, project["id"], "member@example.com", "researcher")
        base = f"{PROJECTS}/{project['id']}/connections"
        account_url = f"{base}/{account['id']}"

        # Picker opens with the saved account; no new grant is needed.
        picked = await post(client, session, f"{account_url}/picker")
        assert picked.status_code == 200, picked.text
        assert picked.headers["cache-control"] == "no-store"
        assert picked.json()["data"]["access_token"] == access_token_for(REFRESH_TOKEN)

        sheet = await post(client, session, base, sheet_body(account["id"]))
        assert sheet.status_code == 201, sheet.text
        assert sheet.json()["data"]["config"] == {
            "spreadsheet_id": SPREADSHEET_ID,
            "account_email": EMAIL,
            "google_subject": SUBJECT,
            "title": TITLE,
        }
        sheet_url = f"{base}/{sheet.json()['data']['id']}"

        # A member sees and reads the spreadsheet, never the account behind it.
        listed = (await member_client.get(base)).json()["data"]
        assert [item["kind"] for item in listed] == ["google_sheets"]
        read = await preview(member_client, member, sheet_url, ANSWERS)
        assert read.status_code == 200, read.text
        for hidden in (
            await member_client.get(account_url),
            await post(member_client, member, f"{account_url}/picker"),
            await post(member_client, member, base, sheet_body(account["id"], "Mine")),
        ):
            assert failure(hidden)[:2] == (404, "NOT_FOUND")

        # Access comes from a grant or from the account, never both.
        both = await post(
            client, session, base, {**sheet_body(account["id"]), "grant_id": account["id"]}
        )
        assert both.status_code == 422

    rows = {row.kind: row for row in await connections(harness)}
    assert stored_token(rows["google_sheets"]) == REFRESH_TOKEN


@pytest.mark.asyncio
async def test_only_its_creator_or_a_manager_changes_a_connection(google_harness: Harness) -> None:
    harness = google_harness
    with_account(harness)
    async with (
        harness.client() as manager_client,
        harness.client() as owner_client,
        harness.client() as other_client,
    ):
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        owner = await login(harness, owner_client, uid="owner", email="owner@example.com")
        other = await login(harness, other_client, uid="other", email="other@example.com")
        project = await create_project(manager_client, manager)
        for email in ("owner@example.com", "other@example.com"):
            await add_member(manager_client, manager, project["id"], email, "researcher")
        grant_id = await google_grant(harness, owner_client, project["id"])
        account = await connect_account(owner_client, owner, project["id"], grant_id)
        base = f"{PROJECTS}/{project['id']}/connections"
        sheet = await post(owner_client, owner, base, sheet_body(account.json()["data"]["id"]))
        url = f"{base}/{sheet.json()['data']['id']}"
        other_headers = mutation_headers(other["csrf_token"])

        for refused in (
            await other_client.patch(url, json={"name": "Mine"}, headers=other_headers),
            await other_client.delete(url, headers=other_headers),
        ):
            assert failure(refused) == (403, "CONNECTION_OWNER_ONLY", None)
        renamed = await manager_client.patch(
            url, json={"name": "Survey 2026"}, headers=mutation_headers(manager["csrf_token"])
        )
        assert renamed.status_code == 200, renamed.text
        removed = await owner_client.delete(url, headers=mutation_headers(owner["csrf_token"]))
        assert removed.status_code == 200, removed.text


@pytest.mark.asyncio
async def test_signing_in_again_restores_the_files_picked_with_the_account(
    google_harness: Harness,
) -> None:
    harness = google_harness
    drive = with_account(harness)
    member_token, third_token = "token-of-the-member", "third-refresh-token-never-shown"
    drive.readers |= {member_token, third_token}
    async with harness.client() as client, harness.client() as member_client:
        session, project, account = await signed_in_owner(harness, client)
        member = await login(harness, member_client, uid="member", email="member@example.com")
        await add_member(client, session, project["id"], "member@example.com", "researcher")
        base = f"{PROJECTS}/{project['id']}/connections"
        sheet = await post(client, session, base, sheet_body(account["id"]))
        sheet_url = f"{base}/{sheet.json()['data']['id']}"

        # The member's own copy of the spreadsheet, picked with another Google account.
        grant_id = await google_grant(
            harness, member_client, project["id"], refresh_token=member_token, subject="sub-2"
        )
        theirs = await connect_account(member_client, member, project["id"], grant_id)
        mine = await post(
            member_client, member, base, sheet_body(theirs.json()["data"]["id"], "Theirs")
        )
        assert mine.status_code == 201, mine.text

        # Google takes the owner's access back: every file picked with it stops at once.
        harness.google_drive.revoked.add(REFRESH_TOKEN)
        assert failure(await preview(client, session, sheet_url, ANSWERS)) == (
            422,
            "CONNECTION_FAILED",
            "access_revoked",
        )

        # Signing in again with the same account renews the account and the file together.
        grant_id = await google_grant(
            harness, client, project["id"], refresh_token=NEW_REFRESH_TOKEN
        )
        renewed = await connect_account(client, session, project["id"], grant_id)
        assert renewed.status_code == 200, renewed.text
        shown = await preview(client, session, sheet_url, ANSWERS)
        assert shown.status_code == 200, shown.text

        # Disconnected and connected again: the file follows the account once more.
        removed = await client.delete(
            f"{base}/{account['id']}", headers=mutation_headers(session["csrf_token"])
        )
        assert removed.status_code == 200, removed.text
        grant_id = await google_grant(harness, client, project["id"], refresh_token=third_token)
        again = await connect_account(client, session, project["id"], grant_id)
        assert again.status_code == 201, again.text

    rows = {row.name: row for row in await connections(harness, "google_sheets")}
    assert stored_token(rows["Survey"]) == third_token
    assert rows["Survey"].last_error_code is None
    assert stored_token(rows["Theirs"]) == member_token
    renewals = [
        event.details
        for event in await audit_events(harness, "connection.reauthorized")
        if event.details["kind"] == "google_sheets"
    ]
    assert renewals == [{"kind": "google_sheets", "spreadsheet_id": SPREADSHEET_ID}] * 2


@pytest.mark.asyncio
async def test_one_reconnect_restores_the_account_and_its_other_files(
    google_harness: Harness,
) -> None:
    harness = google_harness
    with_account(harness)
    async with harness.client() as client:
        session, project, account = await signed_in_owner(harness, client)
        base = f"{PROJECTS}/{project['id']}/connections"
        first = await post(client, session, base, sheet_body(account["id"], "First"))
        second = await post(client, session, base, sheet_body(account["id"], "Second"))
        harness.google_drive.revoked.add(REFRESH_TOKEN)

        # Reconnect on one file renews the other file and the account behind both.
        grant_id = await google_grant(
            harness, client, project["id"], refresh_token=NEW_REFRESH_TOKEN
        )
        renewed = await post(
            client,
            session,
            f"{base}/{first.json()['data']['id']}/reauthorize",
            {"grant_id": grant_id},
        )
        assert renewed.status_code == 200, renewed.text
        shown = await preview(client, session, f"{base}/{second.json()['data']['id']}", ANSWERS)
        assert shown.status_code == 200, shown.text
        picker = await post(client, session, f"{base}/{account['id']}/picker")
        assert picker.json()["data"]["access_token"] == access_token_for(NEW_REFRESH_TOKEN)

    assert {stored_token(row) for row in await connections(harness)} == {NEW_REFRESH_TOKEN}
