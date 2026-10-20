import io

import openpyxl
import pytest
from httpx import AsyncClient

from tests.helpers.datasets import CSV, stored_files, upload_dataset
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, code, create_project

PEOPLE = (
    b"full_name,contact,phone,city,student_id,score\n"
    b"Ann Lee,ann@example.com,+84 912 345 678,Hanoi,S-001,7\n"
    b"Bo Tran,see bo@example.com,0912.345.679,Hue,S-002,8\n"
)


async def save(client: AsyncClient, session: dict, project_id: str, section: str, **values):
    url = f"{PROJECTS}/{project_id}/settings"
    version = (await client.get(url)).json()["data"]["version"]
    response = await client.patch(
        f"{url}/{section}",
        json={"version": version, "values": values},
        headers=mutation_headers(session["csrf_token"]),
    )
    assert response.status_code == 200, response.text


def xlsx(rows: list[list]) -> bytes:
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_reviewers_lose_raw_data_while_the_setting_is_off(harness: Harness) -> None:
    async with (
        harness.client() as pm,
        harness.client() as researcher,
        harness.client() as reviewer,
        harness.client() as outsider,
    ):
        manager = await login(harness, pm, uid="pm", email="pm@example.com")
        res = await login(harness, researcher, uid="res", email="res@example.com")
        await login(harness, reviewer, uid="rev", email="rev@example.com")
        out = await login(harness, outsider, uid="out", email="out@example.com")
        project = await create_project(pm, manager)
        pid = project["id"]
        await add_member(pm, manager, pid, "res@example.com", "researcher")
        await add_member(pm, manager, pid, "rev@example.com", "reviewer")
        dataset = (await upload_dataset(researcher, res, pid)).json()["data"]
        first = dataset["latest_version"]["id"]
        newer = await researcher.post(
            f"{PROJECTS}/{pid}/datasets/{dataset['id']}/versions",
            files={"file": ("v2.csv", CSV + b"3,C,90\n", "text/csv")},
            headers=mutation_headers(res["csrf_token"]),
        )
        assert newer.status_code == 201, newer.text
        base = f"{PROJECTS}/{pid}/datasets"
        item = f"{base}/{dataset['id']}"
        reads = [
            base,
            item,
            f"{item}/versions",
            f"{item}/versions/{first}/download",
            f"{item}/versions/{first}/preview",
            f"{item}/versions/{newer.json()['data']['id']}/download",
        ]

        assert [(await reviewer.get(url)).status_code for url in reads] == [200] * len(reads)
        await save(pm, manager, pid, "access", reviewers_see_data=False)
        for url in reads:
            assert code(await reviewer.get(url)) == (403, "DATASET_ACCESS_DENIED"), url
        for client in (pm, researcher):
            assert [(await client.get(url)).status_code for url in reads] == [200] * len(reads)
        context = (await reviewer.get(f"{PROJECTS}/{pid}/access-context")).json()["data"]
        assert context["permissions"]["can_read_raw_datasets"] is False
        # Results are not raw data: the project itself stays readable.
        assert (await reviewer.get(f"{PROJECTS}/{pid}")).status_code == 200

        # Another project's ids stay hidden from people outside it.
        elsewhere = await create_project(outsider, out, name="Elsewhere")
        crossed = f"{PROJECTS}/{elsewhere['id']}/datasets/{dataset['id']}/versions/{first}/preview"
        assert code(await outsider.get(crossed)) == (404, "NOT_FOUND")
        assert code(await outsider.get(reads[4])) == (404, "NOT_FOUND")

        await save(pm, manager, pid, "access", reviewers_see_data=True)
        assert [(await reviewer.get(url)).status_code for url in reads] == [200] * len(reads)


@pytest.mark.asyncio
async def test_allowed_file_types_are_checked_on_every_upload(harness: Harness, tmp_path) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="pm", email="pm@example.com")
        pid = (await create_project(client, session))["id"]
        workbook = xlsx([["a", "b"], [1, 2]])

        # Both readable types by default.
        assert (await upload_dataset(client, session, pid, name="Csv")).status_code == 201
        excel = await upload_dataset(
            client, session, pid, name="Book", content=workbook, filename="book.xlsx"
        )
        assert excel.status_code == 201, excel.text

        await save(client, session, pid, "data", types=["Excel"])
        files_before = len(stored_files(tmp_path))
        assert code(await upload_dataset(client, session, pid, name="New")) == (
            422,
            "DATASET_FILE_TYPE_NOT_ALLOWED",
        )
        add_version = await client.post(
            f"{PROJECTS}/{pid}/datasets/{excel.json()['data']['id']}/versions",
            files={"file": ("more.csv", CSV, "text/csv")},
            headers=mutation_headers(session["csrf_token"]),
        )
        assert code(add_version) == (422, "DATASET_FILE_TYPE_NOT_ALLOWED")
        # A CSV renamed to .xlsx is read as a workbook, and fails as one.
        assert code(
            await upload_dataset(client, session, pid, name="Renamed", filename="fake.xlsx")
        ) == (422, "INVALID_DATASET")
        assert len(stored_files(tmp_path)) == files_before

        await save(client, session, pid, "data", types=["CSV"])
        assert code(
            await upload_dataset(
                client, session, pid, name="Book 2", content=workbook, filename="b.xlsx"
            )
        ) == (422, "DATASET_FILE_TYPE_NOT_ALLOWED")


@pytest.mark.asyncio
async def test_a_taken_name_adds_a_version_or_a_copy(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="pm", email="pm@example.com")
        pid = (await create_project(client, session))["id"]
        first = (await upload_dataset(client, session, pid, name="Scores")).json()

        again = await upload_dataset(client, session, pid, name="SCORES ")
        assert again.status_code == 201, again.text
        body = again.json()
        assert body["meta"]["upload"]["outcome"] == "created_version"
        assert body["data"]["id"] == first["data"]["id"]
        assert body["data"]["latest_version"]["version_number"] == 2
        assert body["meta"]["upload"]["version_id"] == body["data"]["latest_version"]["id"]

        await save(client, session, pid, "data", same_name="copy")
        copies = [
            (await upload_dataset(client, session, pid, name="scores")).json() for _ in range(2)
        ]
        assert [c["meta"]["upload"]["outcome"] for c in copies] == ["created_copy"] * 2
        assert [c["data"]["name"] for c in copies] == ["scores (2)", "scores (3)"]
        assert all(c["data"]["latest_version"]["version_number"] == 1 for c in copies)

        long_name = "x" * 160
        assert (await upload_dataset(client, session, pid, name=long_name)).status_code == 201
        copied = (await upload_dataset(client, session, pid, name=long_name)).json()["data"]
        assert copied["name"] == "x" * 156 + " (2)"

        fresh = (await upload_dataset(client, session, pid, name="Brand new")).json()
        assert fresh["meta"]["upload"]["outcome"] == "created_dataset"
        listed = (await client.get(f"{PROJECTS}/{pid}/datasets")).json()["meta"]["pagination"]
        assert listed["total"] == 6


@pytest.mark.asyncio
async def test_preview_masks_personal_data_when_the_project_asks(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="pm", email="pm@example.com")
        pid = (await create_project(client, session))["id"]
        dataset = (
            await upload_dataset(client, session, pid, content=PEOPLE, filename="people.csv")
        ).json()["data"]
        url = (
            f"{PROJECTS}/{pid}/datasets/{dataset['id']}/versions/"
            f"{dataset['latest_version']['id']}/preview"
        )

        # On by default: pii and mask.
        masked = await client.get(url)
        assert masked.status_code == 200, masked.text
        assert masked.headers["cache-control"] == "private, no-store"
        data = masked.json()["data"]
        assert data["columns"] == ["full_name", "contact", "phone", "city", "student_id", "score"]
        assert data["rows"] == [
            ["[REDACTED]", "[REDACTED]", "[REDACTED]", "Hanoi", "[REDACTED]", "7"],
            ["[REDACTED]", "[REDACTED]", "[REDACTED]", "Hue", "[REDACTED]", "8"],
        ]
        assert (data["masking_applied"], data["detection_status"]) == (True, "ready")
        assert (data["masked_cells"], data["truncated"]) == (8, False)
        assert b"ann@example.com" not in masked.content

        one = (await client.get(url, params={"limit": 1})).json()["data"]
        assert (len(one["rows"]), one["truncated"]) == (1, True)

        # Mask kept on but detection off: raw rows, and the answer says so.
        await save(client, session, pid, "data", pii=False)
        raw = (await client.get(url)).json()["data"]
        assert raw["rows"][0][1] == "ann@example.com"
        assert (raw["masking_applied"], raw["detection_status"]) == (False, "off")

        # Turned back on after a raw preview: the next preview is masked again.
        await save(client, session, pid, "data", pii=True)
        assert (await client.get(url)).json()["data"]["masking_applied"] is True

        # Masking is for previews: the download is the file as uploaded.
        download = await client.get(url.replace("/preview", "/download"))
        assert download.content == PEOPLE
        assert download.headers["cache-control"] == "private, no-store"
