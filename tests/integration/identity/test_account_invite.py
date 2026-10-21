import pytest
from sqlalchemy import select

from management.audit.models import AuditEvent
from management.identity.bootstrap_admin import bootstrap_admin
from management.identity.models import User
from management.infra.email_templates import account_invite
from tests.helpers.harness import ORIGIN, Harness
from tests.helpers.identity import login, mutation_headers, sign_in, start

USERS = "/api/v1/users"
AUTH = "/api/v1/auth"


async def _admin(harness: Harness, client) -> dict[str, str]:
    session = await login(harness, client, uid="pa", email="pa@example.com")
    await bootstrap_admin(
        "pa@example.com", settings=harness.settings, session_factory=harness.factory
    )
    return mutation_headers(session["csrf_token"])


@pytest.mark.asyncio
async def test_creation_emails_the_sign_in_details_only_when_asked(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    async with harness.client() as admin, harness.client() as client:
        headers = await _admin(harness, admin)
        mailed = await admin.post(USERS, json={"email": "Dat.Ngo@Example.com"}, headers=headers)
        silent = await admin.post(
            USERS, json={"email": "quiet@example.com", "send_email": False}, headers=headers
        )
        assert mailed.status_code == 201, mailed.text
        assert silent.status_code == 201, silent.text
        assert mailed.json()["data"]["invite_email_sent"] is True
        assert mailed.json()["data"]["invite_sent_at"] is not None
        assert silent.json()["data"]["invite_email_sent"] is False
        assert silent.json()["data"]["invite_sent_at"] is None

        (message,) = harness.emails.sent
        # The address is kept as typed, apart from the domain, which has no letter case.
        assert message["to"] == "Dat.Ngo@example.com"
        for body in (message["text"], message["html"]):
            assert "Dat.Ngo@example.com" in body
            assert "http://localhost:3000" in body
            assert "password" not in body.lower()
        harness.emails.sent.clear()
        # Both sign in with an emailed code, mailed or not.
        assert (await sign_in(harness, client, "dat.ngo@example.com")).status_code == 200
        assert (await sign_in(harness, client, "quiet@example.com")).status_code == 200


@pytest.mark.asyncio
async def test_a_failed_email_still_creates_the_account(harness: Harness) -> None:
    async with harness.client() as admin:
        headers = await _admin(harness, admin)
        harness.emails.accept = False
        created = await admin.post(USERS, json={"email": "dat@example.com"}, headers=headers)
        assert created.status_code == 201, created.text
        assert created.json()["data"]["invite_email_sent"] is False
        assert harness.emails.sent == []

        harness.emails.accept = True
        harness.settings.invite_resend_cooldown_seconds = 0
        again = await admin.post(f"{USERS}/{created.json()['data']['id']}/invite", headers=headers)
        assert again.json()["data"]["invite_email_sent"] is True
        assert len(harness.emails.sent) == 1


@pytest.mark.asyncio
async def test_admin_sends_the_same_details_again(harness: Harness) -> None:
    async with harness.client() as admin:
        headers = await _admin(harness, admin)
        quiet = await admin.post(
            USERS, json={"email": "dat@example.com", "send_email": False}, headers=headers
        )
        user_id = quiet.json()["data"]["id"]
        assert harness.emails.sent == []

        # The first mail of an account created without one; no cooldown applies yet.
        first = await admin.post(f"{USERS}/{user_id}/invite", headers=headers)
        assert first.status_code == 200, first.text
        data = first.json()["data"]
        assert data["invite_email_sent"] is True
        assert data["invite_sent_at"] is not None
        (message,) = harness.emails.sent
        assert message["to"] == "dat@example.com"
        assert "Email: dat@example.com\n" in message["text"]
        # No link is put in the mail when the frontend address is not configured.
        assert "Sign-in page:" not in message["text"]

        too_soon = await admin.post(f"{USERS}/{user_id}/invite", headers=headers)
        assert too_soon.status_code == 429
        assert too_soon.json()["error"]["code"] == "INVITE_COOLDOWN"
        assert 1 <= int(too_soon.headers["retry-after"]) <= 60
        assert len(harness.emails.sent) == 1

        harness.settings.invite_resend_cooldown_seconds = 0
        second = await admin.post(f"{USERS}/{user_id}/invite", headers=headers)
        assert second.status_code == 200
        assert len(harness.emails.sent) == 2
        assert harness.emails.sent[1]["text"] == message["text"]

    async with harness.factory() as db:
        events = (
            await db.scalars(select(AuditEvent).where(AuditEvent.action == "user.invite_sent"))
        ).all()
    assert len(events) == 2
    assert {event.resource_id for event in events} == {user_id}


@pytest.mark.asyncio
async def test_details_are_not_sent_again_when_they_no_longer_apply(harness: Harness) -> None:
    harness.settings.invite_resend_cooldown_seconds = 0
    async with harness.client() as admin, harness.client() as client:
        headers = await _admin(harness, admin)
        created = await admin.post(
            USERS, json={"email": "dat@example.com", "send_email": False}, headers=headers
        )
        user_id = created.json()["data"]["id"]
        invite = f"{USERS}/{user_id}/invite"

        # Only a Platform Admin sends them, and only with the CSRF token.
        session = (await sign_in(harness, client, "dat@example.com")).json()["data"]
        own = await client.post(invite, headers=mutation_headers(session["csrf_token"]))
        assert own.status_code == 403
        assert own.json()["error"]["code"] == "ROLE_REQUIRED"
        assert (await admin.post(invite, headers={"Origin": ORIGIN})).status_code == 403

        # The user has signed in: there is nothing left to invite them to.
        signed_in = await admin.post(invite, headers=headers)
        assert signed_in.status_code == 409
        assert signed_in.json()["error"]["code"] == "INVITE_NOT_PENDING"
        me = (await admin.get(f"{AUTH}/me")).json()["data"]["user"]["id"]
        assert (await admin.post(f"{USERS}/{me}/invite", headers=headers)).status_code == 409

        pending = await admin.post(
            USERS, json={"email": "gone@example.com", "send_email": False}, headers=headers
        )
        pending_id = pending.json()["data"]["id"]
        suspended = await admin.patch(
            f"{USERS}/{pending_id}/status", json={"status": "suspended"}, headers=headers
        )
        assert suspended.status_code == 200, suspended.text
        refused = await admin.post(f"{USERS}/{pending_id}/invite", headers=headers)
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "USER_SUSPENDED"

        missing = "00000000-0000-0000-0000-000000000000"
        assert (await admin.post(f"{USERS}/{missing}/invite", headers=headers)).status_code == 404
        assert harness.emails.sent == []

    async with harness.factory() as db:
        user = await db.scalar(select(User).where(User.email_normalized == "dat@example.com"))
    assert user.invite_sent_at is None


def test_the_message_escapes_what_it_quotes() -> None:
    subject, text, html = account_invite(
        '"<b>x</b>"@example.com', "<i>Dat</i>", 'https://app.example.com/?a=1&b="2"'
    )
    assert subject == "Your AI Research Platform account: sign-in details"
    assert text.startswith("Hello <i>Dat</i>,\n")
    assert 'Sign-in page: https://app.example.com/?a=1&b="2"\n' in text
    assert "<b>x</b>" not in html and "<i>Dat</i>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "&lt;i&gt;Dat&lt;/i&gt;" in html
    assert 'href="https://app.example.com/?a=1&amp;b=&quot;2&quot;"' in html


@pytest.mark.asyncio
async def test_a_development_stack_starts_with_an_admin(harness: Harness) -> None:
    from pydantic import ValidationError
    from sqlalchemy import func

    from management.core.config import Settings
    from management.identity.default_admin import ensure_default_admin

    settings = harness.settings.model_copy(update={"default_admin_email": "admin@example.com"})
    # Nothing is configured in the harness itself, so nothing was created.
    await ensure_default_admin(harness.factory, harness.settings)
    async with harness.factory() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 0
    async with harness.client() as client:
        # Someone who started a sign-in for the address holds no session and no role.
        await start(client, "admin@example.com")
        harness.emails.sent.clear()
        # Starting twice leaves one account.
        for _ in range(2):
            await ensure_default_admin(harness.factory, settings)
        signed_in = await sign_in(harness, client, "admin@example.com")
        assert signed_in.status_code == 200, signed_in.text
        user = signed_in.json()["data"]["user"]
        assert user["platform_role"] == "platform_admin"
        assert user["email_verified"] is True
    async with harness.factory() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 1

    # The admin a development stack makes for itself never reaches a deployed system.
    deployed = {
        "cookie_secure": True,
        "session_signing_secret": "a-real-session-signing-secret-of-40-chars",
        "cors_allowed_origins": "https://app.example.com",
        "resend_api_key": "re_key",
        "app_url": "https://app.example.com",
    }
    for env in ("staging", "production"):
        with pytest.raises(ValidationError, match="local development only"):
            Settings(
                _env_file=None, app_env=env, default_admin_email="admin@example.com", **deployed
            )
