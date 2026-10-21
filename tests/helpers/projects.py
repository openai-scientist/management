from types import SimpleNamespace

from httpx import AsyncClient

from tests.helpers.harness import ORIGIN
from tests.helpers.identity import mutation_headers, sign_in

PROJECTS = "/api/v1/projects"
INVITATIONS = "/api/v1/invitations"


async def create_project(client: AsyncClient, session: dict, **fields: object) -> dict:
    response = await client.post(
        PROJECTS,
        json={"name": "Study A", **fields},
        headers=mutation_headers(session["csrf_token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def invite_member(
    client: AsyncClient, session: dict, project_id: str, email: str, role: str
) -> dict:
    """Invite a registered user; they have no access until they accept."""
    response = await client.post(
        f"{PROJECTS}/{project_id}/members",
        json={"email": email, "role": role},
        headers=mutation_headers(session["csrf_token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def accept_invitation(client: AsyncClient, session: dict, membership_id: str):
    return await client.post(
        f"{INVITATIONS}/{membership_id}/accept", headers=mutation_headers(session["csrf_token"])
    )


async def add_member(
    client: AsyncClient, session: dict, project_id: str, email: str, role: str
) -> dict:
    """Invite a registered user and accept as that user, in a browser of their own."""
    invited = await invite_member(client, session, project_id, email, role)
    async with AsyncClient(transport=client._transport, base_url=ORIGIN) as invitee:
        # The emailed code is read from the fake sender behind this client's app.
        emails = SimpleNamespace(emails=client._transport.app.state.email_sender)
        signed_in = await sign_in(emails, invitee, email)
        assert signed_in.status_code == 200, signed_in.text
        accepted = await accept_invitation(invitee, signed_in.json()["data"], invited["id"])
        assert accepted.status_code == 200, accepted.text
    return {**invited, "status": "active"}


MISSING = "00000000-0000-0000-0000-000000000000"


def code(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def answer(client: AsyncClient, session: dict, membership_id: str, action: str):
    return await client.post(
        f"{INVITATIONS}/{membership_id}/{action}", headers=mutation_headers(session["csrf_token"])
    )


async def remove(client: AsyncClient, session: dict, project_id: str, membership_id: str):
    return await client.delete(
        f"{PROJECTS}/{project_id}/members/{membership_id}",
        headers=mutation_headers(session["csrf_token"]),
    )
