import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from management.connections.models import DataConnection, GoogleConnectionGrant
from management.datasets.models import DatasetVersion
from tests.fakes import access_token_for
from tests.helpers.audit import audit_events
from tests.helpers.connections import (
    EMAIL,
    PRIVATE_ID,
    REFRESH_TOKEN,
    SPREADSHEET_ID,
    SUBJECT,
    TITLE,
    connect_account,
    connections,
    create_connection,
    create_sheets_connection,
    failure,
    google_account,
    google_grant,
    grant_ids,
    preview,
    reauthorize,
    sheets_body,
    start,
    stored_token,
    wait_until,
    with_google,
    with_sheets,
)
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, create_project

NEW_REFRESH_TOKEN = "second-refresh-token-never-shown-either"
TAB = {"type": "table", "schema": TITLE, "name": "Answers"}
ANSWERS_CSV = b'student,school,score\r\nAn,A,7.5\r\nBinh,"B, north",8\r\nChi,,6.25\r\n'


async def connected_spreadsheet(harness: Harness, client: AsyncClient):
    """A signed-in owner, their project and the URL of a saved Google Sheets connection."""
    session = await login(harness, client, uid="owner", email="owner@example.com")
    project = await create_project(client, session)
    account_id = await google_account(harness, client, session, project["id"])
    created = await create_sheets_connection(client, session, project["id"], account_id)
    assert created.status_code == 201, created.text
    url = f"{PROJECTS}/{project['id']}/connections/{created.json()['data']['id']}"
    return session, project, url


async def created_event(harness: Harness, kind: str):
    """The one `connection.created` audit event of a connection of this kind."""
    (event,) = [
        event
        for event in await audit_events(harness, "connection.created")
        if event.details["kind"] == kind
    ]
    return event


@pytest.mark.asyncio
async def test_a_spreadsheet_is_connected_browsed_and_previewed(google_harness: Harness) -> None:
    harness = google_harness
    sheets = with_sheets(harness)
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        account_id = await google_account(harness, client, session, project["id"])
        headers = mutation_headers(session["csrf_token"])

        response = await create_sheets_connection(client, session, project["id"], account_id)
        assert response.status_code == 201, response.text
        created = response.json()["data"]
        assert created["kind"] == "google_sheets"
        assert created["config"] == {
            "spreadsheet_id": SPREADSHEET_ID,
            "title": TITLE,
            "account_email": EMAIL,
            "google_subject": SUBJECT,
        }
        assert created["last_tested_at"] is not None and created["last_error_code"] is None
        # The account's access was copied, and the token is stored encrypted.
        assert await grant_ids(harness) == set()
        (row,) = await connections(harness, "google_sheets")
        assert REFRESH_TOKEN not in row.secret_ciphertext
        assert stored_token(row) == REFRESH_TOKEN
        assert sheets.requests[0][0] == f"/v4/spreadsheets/{SPREADSHEET_ID}"
        url = f"{PROJECTS}/{project['id']}/connections/{created['id']}"

        tested = await client.post(f"{url}/test", headers=headers)
        assert tested.status_code == 200, tested.text
        assert tested.json()["data"]["last_error_code"] is None

        schemas = await client.get(f"{url}/schemas")
        assert schemas.json()["data"] == [TITLE]
        tables = await client.get(f"{url}/tables", params={"schema": TITLE, "search": "s"})
        assert tables.json()["data"] == [
            {"schema": TITLE, "name": name, "type": "table", "column_count": None}
            for name in ["Answers", "Notes"]
        ]
        columns = await client.get(f"{url}/columns", params={"schema": TITLE, "table": "Answers"})
        assert columns.json()["data"] == [
            {"name": name, "type": "text", "role": None} for name in ["student", "school", "score"]
        ]
        missing = await client.get(f"{url}/columns", params={"schema": TITLE, "table": "Gone"})
        assert failure(missing) == (422, "SOURCE_INVALID", "source_not_found")

        shown = await preview(client, session, url, TAB)
        assert shown.status_code == 200, shown.text
        assert shown.json()["data"] == {
            "columns": [
                {"name": name, "type": "text", "role": None}
                for name in ["student", "school", "score"]
            ],
            "rows": [["An", "A", "7.5"], ["Binh", "B, north", "8"], ["Chi", None, "6.25"]],
            "truncated": False,
        }
        # No more than a preview shows, plus the one row that tells whether there is more.
        limit = harness.settings.connection_preview_max_rows
        # Then the rest of the grid: the rows Google left out may only be empty ones.
        assert sheets.ranges()[-2:] == [
            f"'Answers'!2:{limit + 2}",
            f"'Answers'!{limit + 3}:1000",
        ]

        requests_before = len(sheets.requests)
        query = await preview(client, session, url, {"type": "query", "sql": "SELECT 1"})
        assert failure(query) == (422, "SOURCE_INVALID", "unsupported_source")
        assert len(sheets.requests) == requests_before
        broken = await preview(client, session, url, TAB | {"name": "Broken"})
        assert failure(broken) == (422, "SOURCE_INVALID", "source_malformed")

        listed = await client.get(f"{PROJECTS}/{project['id']}/connections")
        answers = [response, tested, schemas, tables, columns, shown, query, listed]
        for answer in answers:
            assert REFRESH_TOKEN not in answer.text
            assert access_token_for(REFRESH_TOKEN) not in answer.text

    event = await created_event(harness, "google_sheets")
    # Where it points, not whose account: no address in the audit trail.
    assert event.details == {
        "name": "Survey",
        "kind": "google_sheets",
        "spreadsheet_id": SPREADSHEET_ID,
    }
    async with harness.factory() as db:
        from management.audit.models import AuditEvent

        trail = json.dumps([item.details for item in await db.scalars(select(AuditEvent))])
    assert REFRESH_TOKEN not in trail and EMAIL not in trail and SUBJECT not in trail


@pytest.mark.asyncio
async def test_a_grant_works_for_its_user_in_its_project_before_it_expires(
    google_harness: Harness,
) -> None:
    harness = google_harness
    sheets = with_sheets(harness)
    async with harness.client() as client, harness.client() as colleague_client:
        owner = await login(harness, client, uid="owner", email="owner@example.com")
        colleague = await login(
            harness, colleague_client, uid="colleague", email="colleague@example.com"
        )
        project = await create_project(client, owner)
        other_project = await create_project(client, owner, name="Another project")
        await add_member(client, owner, project["id"], "colleague@example.com", "researcher")
        grant_id = await google_grant(harness, client, project["id"])

        # Somebody else's grant, though they may create connections in the same project.
        stolen = await connect_account(colleague_client, colleague, project["id"], grant_id)
        assert failure(stolen) == (422, "GOOGLE_GRANT_INVALID", None)
        elsewhere = await connect_account(client, owner, other_project["id"], grant_id)
        assert failure(elsewhere) == (422, "GOOGLE_GRANT_INVALID", None)
        unknown = await connect_account(client, owner, project["id"], str(uuid4()))
        assert failure(unknown) == (422, "GOOGLE_GRANT_INVALID", None)
        # Started, and Google has not answered: there is no token in it yet.
        await start(client, project["id"])
        async with harness.factory() as db:
            unanswered = await db.scalar(
                select(GoogleConnectionGrant.id).where(
                    GoogleConnectionGrant.secret_ciphertext.is_(None)
                )
            )
        early = await connect_account(client, owner, project["id"], str(unanswered))
        assert failure(early) == (422, "GOOGLE_GRANT_INVALID", None)

        # Nothing was tried and nothing was used up: the grant is still good for its owner.
        assert sheets.requests == [] and harness.google_drive.refreshed == []
        assert await connections(harness) == []
        assert await grant_ids(harness) == {grant_id, str(unanswered)}

        async with harness.factory() as db:
            await db.execute(
                update(GoogleConnectionGrant)
                .where(GoogleConnectionGrant.secret_ciphertext.is_not(None))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await db.commit()
        late = await connect_account(client, owner, project["id"], grant_id)
        assert failure(late) == (422, "GOOGLE_GRANT_INVALID", None)
        # An expired grant does not wait for the next visit to Google to be removed.
        assert await grant_ids(harness) == {str(unanswered)}
        assert harness.google_drive.refreshed == [] and await connections(harness) == []


@pytest.mark.asyncio
async def test_a_spreadsheet_is_picked_only_with_the_callers_account_in_its_project(
    google_harness: Harness,
) -> None:
    harness = google_harness
    sheets = with_sheets(harness)
    async with harness.client() as client, harness.client() as colleague_client:
        owner = await login(harness, client, uid="owner", email="owner@example.com")
        colleague = await login(
            harness, colleague_client, uid="colleague", email="colleague@example.com"
        )
        project = await create_project(client, owner)
        other_project = await create_project(client, owner, name="Another project")
        await add_member(client, owner, project["id"], "colleague@example.com", "researcher")
        account_id = await google_account(harness, client, owner, project["id"])

        # Another member's account, the account in another project, or none at all.
        for browser, session, project_id, account in (
            (colleague_client, colleague, project["id"], account_id),
            (client, owner, other_project["id"], account_id),
            (client, owner, project["id"], str(uuid4())),
        ):
            refused = await create_sheets_connection(browser, session, project_id, account)
            assert failure(refused)[:2] == (404, "NOT_FOUND")

        for spreadsheet in ("", "not a spreadsheet", "https://example.com/spreadsheets/d/abc"):
            invalid = await create_sheets_connection(
                client, owner, project["id"], account_id, spreadsheet=spreadsheet
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
        for extra in ({"secret": {"refresh_token": "mine"}}, {"grant_id": str(uuid4())}):
            strict = await client.post(
                f"{PROJECTS}/{project['id']}/connections",
                json=sheets_body(account_id) | extra,
                headers=mutation_headers(owner["csrf_token"]),
            )
            assert strict.status_code == 422

        assert sheets.requests == []
        assert await connections(harness, "google_sheets") == []


@pytest.mark.asyncio
async def test_a_spreadsheet_that_does_not_open_saves_nothing(google_harness: Harness) -> None:
    harness = google_harness
    with_sheets(harness)
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        account_id = await google_account(harness, client, session, project["id"])

        refused = await create_sheets_connection(
            client, session, project["id"], account_id, spreadsheet=PRIVATE_ID
        )
        assert failure(refused) == (422, "CONNECTION_FAILED", "permission_denied")
        assert "Google account" in refused.json()["message"]
        assert await connections(harness, "google_sheets") == []

        # The same account, another address: no second sign-in with Google.
        created = await create_sheets_connection(client, session, project["id"], account_id)
        assert created.status_code == 201, created.text

    (event,) = await audit_events(harness, "connection.test_failed")
    assert event.details == {
        "kind": "google_sheets",
        "spreadsheet_id": PRIVATE_ID,
        "reason": "permission_denied",
    }


@pytest.mark.asyncio
async def test_a_grant_that_expires_while_the_account_is_tried_keeps_nothing(
    google_harness: Harness,
) -> None:
    harness = google_harness
    drive = with_google(harness)
    drive.hold = asyncio.Event()
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        grant_id = await google_grant(harness, client, project["id"])

        attempt = asyncio.create_task(connect_account(client, session, project["id"], grant_id))
        await wait_until(lambda: drive.held == 1)
        async with harness.factory() as db:
            await db.execute(
                update(GoogleConnectionGrant).values(
                    expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
            await db.commit()
        drive.hold.set()

        assert failure(await attempt) == (422, "GOOGLE_GRANT_INVALID", None)
        assert await connections(harness) == []


@pytest.mark.asyncio
async def test_an_account_removed_while_the_spreadsheet_is_tried_makes_no_connection(
    google_harness: Harness,
) -> None:
    harness = google_harness
    sheets = with_sheets(harness)
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        account_id = await google_account(harness, client, session, project["id"])
        sheets.hold = asyncio.Event()

        attempt = asyncio.create_task(
            create_sheets_connection(client, session, project["id"], account_id)
        )
        await wait_until(lambda: len(sheets.requests) == 1)
        async with harness.factory() as db:
            await db.execute(delete(DataConnection).where(DataConnection.kind == "google"))
            await db.commit()
        sheets.hold.set()

        assert failure(await attempt)[:2] == (404, "NOT_FOUND")
        assert await connections(harness) == []


@pytest.mark.asyncio
async def test_google_connections_follow_roles_archive_and_the_feature_switch(
    google_harness: Harness, harness: Harness
) -> None:
    with_sheets(google_harness)
    async with google_harness.client() as client, google_harness.client() as reviewer_client:
        owner = await login(google_harness, client, uid="owner", email="owner@example.com")
        reviewer = await login(
            google_harness, reviewer_client, uid="reviewer", email="reviewer@example.com"
        )
        project = await create_project(client, owner)
        await add_member(client, owner, project["id"], "reviewer@example.com", "reviewer")
        account_id = await google_account(google_harness, client, owner, project["id"])
        created = await create_sheets_connection(client, owner, project["id"], account_id)
        url = f"{PROJECTS}/{project['id']}/connections/{created.json()['data']['id']}"
        spare = await google_grant(google_harness, client, project["id"])

        denied = await create_sheets_connection(
            reviewer_client, reviewer, project["id"], account_id
        )
        assert denied.status_code == 403
        denied = await connect_account(reviewer_client, reviewer, project["id"], spare)
        assert denied.status_code == 403
        denied = await reauthorize(reviewer_client, reviewer, url, spare)
        assert denied.status_code == 403
        no_csrf = await client.post(f"{url}/reauthorize", json={"grant_id": spare})
        assert no_csrf.status_code == 403

        archived = await client.post(
            f"{PROJECTS}/{project['id']}/archive", headers=mutation_headers(owner["csrf_token"])
        )
        assert archived.status_code == 200
        frozen = await create_sheets_connection(
            client, owner, project["id"], account_id, name="Later"
        )
        assert frozen.status_code == 409
        frozen = await reauthorize(client, owner, url, spare)
        assert frozen.status_code == 409
        assert await grant_ids(google_harness) == {spare}
        assert len(await connections(google_harness, "google_sheets")) == 1

    # Without the Google settings the kind is off, and nothing is tried.
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        off = await create_sheets_connection(client, session, project["id"], str(uuid4()))
        assert off.status_code == 503
        assert off.json()["error"]["code"] == "CONNECTIONS_NOT_CONFIGURED"
        assert harness.connectors.built == []
        # The kinds that were there before work as they did.
        assert (await create_connection(client, session, project["id"])).status_code == 201


@pytest.mark.asyncio
async def test_a_revoked_connection_is_reauthorized_by_the_same_google_account(
    google_harness: Harness,
) -> None:
    harness = google_harness
    async with harness.client() as client:
        # While the factory is still the fake one: a connection of another kind.
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        database = await create_connection(client, session, project["id"])
        database_url = f"{PROJECTS}/{project['id']}/connections/{database.json()['data']['id']}"

        sheets = with_sheets(harness)
        account_id = await google_account(harness, client, session, project["id"])
        created = await create_sheets_connection(client, session, project["id"], account_id)
        assert created.status_code == 201, created.text
        url = f"{PROJECTS}/{project['id']}/connections/{created.json()['data']['id']}"
        headers = mutation_headers(session["csrf_token"])

        async def sheets_row() -> DataConnection:
            return next(row for row in await connections(harness) if row.kind == "google_sheets")

        # Google takes the access back: after 7 days in testing mode, or when the user does.
        harness.google_drive.revoked.add(REFRESH_TOKEN)
        tested = await client.post(f"{url}/test", headers=headers)
        assert tested.json()["data"]["last_error_code"] == "access_revoked"
        shown = await preview(client, session, url, TAB)
        assert failure(shown) == (422, "CONNECTION_FAILED", "access_revoked")
        assert "Reauthorize" in shown.json()["message"]

        # Another Google account, even one that can open the spreadsheet, is not accepted.
        sheets.spreadsheets[SPREADSHEET_ID].readers |= {"token-of-another-account"}
        other = await google_grant(
            harness,
            client,
            project["id"],
            refresh_token="token-of-another-account",
            subject="google-sub-2",
        )
        mismatch = await reauthorize(client, session, url, other)
        assert failure(mismatch) == (422, "GOOGLE_ACCOUNT_MISMATCH", None)
        assert stored_token(await sheets_row()) == REFRESH_TOKEN
        assert (await sheets_row()).last_error_code == "access_revoked"

        wrong_kind = await reauthorize(client, session, database_url, other)
        assert failure(wrong_kind) == (422, "CONNECTION_NOT_GOOGLE", None)
        unknown = await reauthorize(client, session, url, str(uuid4()))
        assert failure(unknown) == (422, "GOOGLE_GRANT_INVALID", None)

        # The right account, and a token that does not open the spreadsheet: nothing changes
        # and the grant can be tried again.
        fresh = await google_grant(harness, client, project["id"], refresh_token=NEW_REFRESH_TOKEN)
        closed = await reauthorize(client, session, url, fresh)
        assert failure(closed) == (422, "CONNECTION_FAILED", "permission_denied")
        assert stored_token(await sheets_row()) == REFRESH_TOKEN
        assert fresh in await grant_ids(harness)

        sheets.spreadsheets[SPREADSHEET_ID].readers |= {NEW_REFRESH_TOKEN}
        renewed = await reauthorize(client, session, url, fresh)
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["data"]["id"] == created.json()["data"]["id"]
        assert renewed.json()["data"]["last_error_code"] is None
        assert renewed.json()["data"]["config"] == created.json()["data"]["config"]
        assert stored_token(await sheets_row()) == NEW_REFRESH_TOKEN
        assert fresh not in await grant_ids(harness)
        for answer in (tested, shown, mismatch, closed, renewed):
            assert REFRESH_TOKEN not in answer.text and NEW_REFRESH_TOKEN not in answer.text

        spent = await reauthorize(client, session, url, fresh)
        assert failure(spent) == (422, "GOOGLE_GRANT_INVALID", None)
        shown = await preview(client, session, url, TAB)
        assert shown.status_code == 200, shown.text
        assert harness.google_drive.refreshed[-1] == NEW_REFRESH_TOKEN

    (event,) = await audit_events(harness, "connection.reauthorized")
    assert event.details == {"kind": "google_sheets", "spreadsheet_id": SPREADSHEET_ID}


@pytest.mark.asyncio
async def test_a_tab_is_imported_as_a_dataset_version(google_harness: Harness, tmp_path) -> None:
    harness = google_harness
    with_sheets(harness)
    async with harness.client() as client:
        session, project, url = await connected_spreadsheet(harness, client)
        connection_id = url.rsplit("/", 1)[-1]
        base = f"{PROJECTS}/{project['id']}/datasets"
        headers = mutation_headers(session["csrf_token"])

        def body(source: dict, name: str = "Survey answers") -> dict:
            return {"name": name, "connection_id": connection_id, "source": source}

        imported = await client.post(f"{base}/from-connection", json=body(TAB), headers=headers)
        assert imported.status_code == 201, imported.text
        version = imported.json()["data"]["latest_version"]
        assert version["source_type"] == "connection"
        assert version["source"]["connection_kind"] == "google_sheets"
        assert version["source"]["connection_name"] == "Survey"
        assert version["source"]["source"] == TAB
        assert version["original_filename"] == "Answers.csv"
        assert (version["row_count"], version["column_names"]) == (
            3,
            ["student", "school", "score"],
        )
        dataset_id = imported.json()["data"]["id"]
        download = await client.get(f"{base}/{dataset_id}/versions/{version['id']}/download")
        assert download.content == ANSWERS_CSV
        assert list(csv.reader(io.StringIO(download.text)))[3] == ["Chi", "", "6.25"]

        query = await client.post(
            f"{base}/from-connection",
            json=body({"type": "query", "sql": "SELECT 1"}, "From a query"),
            headers=headers,
        )
        assert failure(query) == (422, "SOURCE_INVALID", "unsupported_source")
        broken = await client.post(
            f"{base}/from-connection",
            json=body(TAB | {"name": "Broken"}, "Broken"),
            headers=headers,
        )
        assert failure(broken) == (422, "SOURCE_INVALID", "source_malformed")

    async with harness.factory() as db:
        (stored,) = await db.scalars(select(DatasetVersion))
    assert stored.source_type == "connection"
    assert stored.source_details["connection_kind"] == "google_sheets"
    files = [path for path in (tmp_path / "storage").rglob("*") if path.is_file()]
    assert len(files) == 1
