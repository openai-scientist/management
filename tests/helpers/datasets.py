from httpx import AsyncClient

from tests.helpers.identity import mutation_headers
from tests.helpers.projects import PROJECTS

CSV = b"student_id,school,exam_score\n1,A,70\n2,B,81\n"


async def upload_dataset(
    client: AsyncClient,
    session: dict,
    project_id: str,
    *,
    name: str = "Exam scores",
    content: bytes = CSV,
    filename: str = "scores.csv",
):
    return await client.post(
        f"{PROJECTS}/{project_id}/datasets",
        data={"name": name},
        files={"file": (filename, content, "text/csv")},
        headers=mutation_headers(session["csrf_token"]),
    )


TABLE = {"type": "table", "schema": "public", "name": "scores"}


def stored_files(tmp_path) -> list:
    return [path for path in (tmp_path / "storage").rglob("*") if path.is_file()]


async def import_dataset(
    client: AsyncClient,
    session: dict,
    project_id: str,
    connection_id: str,
    source: dict = TABLE,
    *,
    name: str = "Exam scores",
    **more: object,
):
    return await client.post(
        f"{PROJECTS}/{project_id}/datasets/from-connection",
        json={"name": name, "connection_id": connection_id, "source": source, **more},
        headers=mutation_headers(session["csrf_token"]),
    )


async def import_version(
    client: AsyncClient,
    session: dict,
    project_id: str,
    dataset_id: str,
    connection_id: str,
    source: dict = TABLE,
):
    return await client.post(
        f"{PROJECTS}/{project_id}/datasets/{dataset_id}/versions/from-connection",
        json={"connection_id": connection_id, "source": source},
        headers=mutation_headers(session["csrf_token"]),
    )
