import pytest
from sqlalchemy import select

from management.audit.models import AuditEvent
from management.identity.bootstrap_admin import bootstrap_admin
from management.identity.models import User, UserPlatformRole
from tests.helpers.harness import ORIGIN, Harness
from tests.helpers.identity import login, mutation_headers, sign_in

USERS = "/api/v1/users"
AUTH = "/api/v1/auth"
PROJECTS = "/api/v1/projects"


async def _admin(harness: Harness, client) -> dict[str, str]:
    session = await login(harness, client, uid="pa", email="pa@example.com")
    await bootstrap_admin(
        "pa@example.com", settings=harness.settings, session_factory=harness.factory
    )
    return mutation_headers(session["csrf_token"])


@pytest.mark.asyncio
async def test_created_user_gets_a_name_from_the_email_and_no_password(
    harness: Harness,
) -> None:
    async with harness.client() as admin, harness.client() as client:
        headers = await _admin(harness, admin)
        # A password in the body is ignored: there are none.
        created = await admin.post(
            USERS, json={"email": "Dat.Ngo@Example.com", "password": "secret"}, headers=headers
        )
        named = await admin.post(
            USERS, json={"email": "c@example.com", "display_name": " Chi "}, headers=headers
        )
        blank = await admin.post(
            USERS, json={"email": "d@example.com", "display_name": "   "}, headers=headers
        )
        unnamed_sign_in = await sign_in(harness, client, "dat.ngo@example.com")
        named_sign_in = await sign_in(harness, client, "c@example.com")

    assert created.status_code == 201, created.text
    user = created.json()["data"]
    assert user["display_name"] == "dat.ngo"
    assert user["platform_role"] == "user"
    assert user["last_login_at"] is None
    assert "temporary_password" not in user
    assert named.json()["data"]["display_name"] == "Chi"
    assert blank.json()["data"]["display_name"] == "d"
    # Only a name the admin chose spares the user the name step.
    assert unnamed_sign_in.json()["data"]["user"]["onboarded"] is False
    assert named_sign_in.json()["data"]["user"]["onboarded"] is True


@pytest.mark.asyncio
async def test_created_user_is_a_plain_user_until_the_first_sign_in(harness: Harness) -> None:
    async with harness.client() as admin, harness.client() as client:
        headers = await _admin(harness, admin)
        # A role in the body is ignored: creation never makes a Platform Admin.
        created = await admin.post(
            USERS,
            json={"email": "boss@example.com", "platform_role": "platform_admin"},
            headers=headers,
        )
        role_url = f"{USERS}/{created.json()['data']['id']}/platform-role"
        unverified = await admin.put(role_url, json={"role": "platform_admin"}, headers=headers)
        await sign_in(harness, client, "boss@example.com")
        granted = await admin.put(role_url, json={"role": "platform_admin"}, headers=headers)

    assert created.status_code == 201, created.text
    assert created.json()["data"]["platform_role"] == "user"
    assert unverified.status_code == 409, unverified.text
    assert unverified.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
    assert granted.status_code == 200, granted.text
    assert granted.json()["data"]["platform_role"] == "platform_admin"
    async with harness.factory() as db:
        boss = await db.scalar(select(User).where(User.email_normalized == "boss@example.com"))
        # `user` is not stored: the only role row is the one granted afterwards.
        assert (await db.get(UserPlatformRole, boss.id)).role_code == "platform_admin"
        actions = (
            await db.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == str(boss.id))
            )
        ).all()
    assert "platform_admin.role_granted" in actions


@pytest.mark.asyncio
async def test_user_edits_own_profile_and_admin_edits_anyone(harness: Harness) -> None:
    async with harness.client() as admin, harness.client() as member:
        headers = await _admin(harness, admin)
        session = await login(harness, member, uid="member", email="member@example.com")
        own = mutation_headers(session["csrf_token"])
        member_id = session["user"]["id"]

        no_csrf = await member.patch(
            f"{AUTH}/me", json={"display_name": "X"}, headers={"Origin": ORIGIN}
        )
        blank = await member.patch(f"{AUTH}/me", json={"display_name": "   "}, headers=own)
        mine = await member.patch(f"{AUTH}/me", json={"display_name": " Minh "}, headers=own)
        not_admin = await member.patch(
            f"{USERS}/{member_id}", json={"display_name": "Y"}, headers=own
        )
        by_admin = await admin.patch(
            f"{USERS}/{member_id}", json={"display_name": "Minh Tran"}, headers=headers
        )
        unknown = await admin.patch(
            f"{USERS}/00000000-0000-0000-0000-000000000000",
            json={"display_name": "Z"},
            headers=headers,
        )
        admin_profile = await admin.get(f"{AUTH}/me")

    assert no_csrf.status_code == 403
    assert blank.status_code == 422
    assert mine.status_code == 200, mine.text
    assert mine.json()["data"]["display_name"] == "Minh"
    assert not_admin.status_code == 403
    assert not_admin.json()["error"]["code"] == "ROLE_REQUIRED"
    assert by_admin.status_code == 200, by_admin.text
    assert by_admin.json()["data"]["display_name"] == "Minh Tran"
    assert unknown.status_code == 404
    # Editing someone else leaves the editor's own profile alone.
    assert admin_profile.json()["data"]["user"]["display_name"] == "Pa"
    async with harness.factory() as db:
        events = (
            await db.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "user.profile_updated")
                .order_by(AuditEvent.created_at)
            )
        ).all()
    # The earlier events are each sign-in helper naming its user.
    assert [event.details["display_name"]["after"] for event in events[-2:]] == [
        "Minh",
        "Minh Tran",
    ]


@pytest.mark.asyncio
async def test_platform_role_is_granted_and_removed_by_name(harness: Harness) -> None:
    async with harness.client() as admin, harness.client() as member:
        headers = await _admin(harness, admin)
        session = await login(harness, member, uid="member", email="member@example.com")
        role_url = f"{USERS}/{session['user']['id']}/platform-role"
        own_role_url = f"{USERS}/{(await admin.get(f'{AUTH}/me')).json()['data']['user']['id']}"

        forbidden = await member.get(USERS)
        granted = await admin.put(role_url, json={"role": "platform_admin"}, headers=headers)
        allowed = await member.get(USERS)
        removed = await admin.put(role_url, json={"role": "user"}, headers=headers)
        null_role = await admin.put(role_url, json={"role": None}, headers=headers)
        last_admin = await admin.put(
            f"{own_role_url}/platform-role", json={"role": "user"}, headers=headers
        )

    assert forbidden.status_code == 403
    assert granted.json()["data"]["platform_role"] == "platform_admin"
    assert allowed.status_code == 200
    assert removed.json()["data"]["platform_role"] == "user"
    assert null_role.status_code == 422
    assert last_admin.status_code == 409
    assert last_admin.json()["error"]["code"] == "LAST_PLATFORM_ADMIN"
