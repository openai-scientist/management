import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.models import AuditEvent
from management.core.errors import APIError
from management.identity.bootstrap_admin import bootstrap_admin
from management.identity.models import EmailOtp, User
from tests.helpers.harness import Harness
from tests.helpers.identity import (
    AUTH,
    EMAIL,
    USERS,
    emailed_code,
    emailed_link_token,
    invalid,
    login,
    mutation_headers,
    post,
    sign_in,
    start,
    user_row,
    wrong,
)
from tests.helpers.projects import PROJECTS, create_project

PENDING = {"email": EMAIL, "expires_in_seconds": 600, "resend_after_seconds": 0}


@pytest.mark.asyncio
async def test_a_new_address_gets_an_account_and_signs_in_with_the_code(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    async with harness.client() as client:
        started = await start(client)
        assert started.json()["data"] == PENDING
        assert "set-cookie" not in started.headers
        assert (await client.get(f"{AUTH}/me")).status_code == 401

        (message,) = harness.emails.sent
        code = emailed_code(harness, EMAIL, keep=True)
        token = emailed_link_token(harness, EMAIL, keep=True)
        assert message["subject"] == "Your AI Research Platform sign-in code"
        assert code not in message["subject"] and token not in message["subject"]
        assert f"\nSign-in code: {code}\n" in message["text"]
        assert (
            f"\nSign-in link: http://localhost:3000/auth/verify#token={token}\n"
            in (message["text"])
        )
        assert "(Vietnam time, GMT+7), in 10 minutes\n" in message["text"]

        signed_in = await post(client, "email/verify", email=EMAIL, code=code)
        assert signed_in.status_code == 200, signed_in.text
        user = signed_in.json()["data"]["user"]
        assert (user["email_verified"], user["onboarded"], user["display_name"]) == (
            True,
            False,
            "new",
        )
        assert (await client.get(f"{AUTH}/me")).status_code == 200

        named = await client.patch(
            f"{AUTH}/me",
            json={"display_name": "New Person"},
            headers=mutation_headers(signed_in.json()["data"]["csrf_token"]),
        )
        assert named.json()["data"]["onboarded"] is True

    async with harness.client() as other:
        # The code and the link sent with it worked once, together.
        assert invalid(await post(other, "email/verify", email=EMAIL, code=code))
        assert invalid(await post(other, "email/verify-link", token=token))
        again = await sign_in(harness, other, EMAIL)
        assert again.json()["data"]["user"]["onboarded"] is True
    async with harness.factory() as db:
        actions = (await db.scalars(select(AuditEvent.action))).all()
        assert await db.scalar(select(func.count()).select_from(User)) == 1
    assert sorted(actions) == ["user.email_verified", "user.profile_updated", "user.registered"]


@pytest.mark.asyncio
async def test_the_link_shows_whom_it_signs_in_and_then_signs_in(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    async with harness.client() as client:
        await login(harness, client, uid="dat", email=EMAIL)
    async with harness.client() as client:
        await start(client)
        token = emailed_link_token(harness, EMAIL, keep=True)
        code = emailed_code(harness, EMAIL)
        for _ in range(2):
            # Looking changes nothing.
            info = await post(client, "email/link-info", token=token)
            assert info.json()["data"] == {"email": "n***@example.com"}
        assert "set-cookie" not in info.headers

        signed_in = await post(client, "email/verify-link", token=token)
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["data"]["user"]["onboarded"] is True
        assert invalid(await post(client, "email/verify-link", token=token))
        assert invalid(await post(client, "email/link-info", token=token))
        assert invalid(await post(client, "email/verify", email=EMAIL, code=code))
        malformed = await post(client, "email/verify-link", token="short")
        assert malformed.status_code == 422


@pytest.mark.asyncio
async def test_every_address_gets_the_same_answer(harness: Harness) -> None:
    async with harness.client() as client:
        await login(harness, client, uid="known", email="known@example.com")
        harness.emails.sent.clear()
        async with harness.factory() as db:
            (
                await db.scalar(select(User).where(User.email == "known@example.com"))
            ).status = "suspended"
            await db.commit()
        answers = [
            (await post(client, "email/start", email=email)).json()
            for email in ("known@example.com", "fresh@example.com")
        ]
        for answer in answers:
            del answer["meta"]
        assert answers[0] | {"data": None} == answers[1] | {"data": None}
        # The suspended account is sent nothing and cannot use an old code.
        assert [message["to"] for message in harness.emails.sent] == ["fresh@example.com"]
        assert invalid(await post(client, "email/verify", email="known@example.com", code="123456"))
        assert invalid(await post(client, "email/verify", email="nobody@example.com", code="1" * 6))


@pytest.mark.asyncio
async def test_a_start_within_the_cooldown_keeps_the_code_and_link(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    harness.settings.otp_resend_cooldown_seconds = 60
    async with harness.client() as client:
        await start(client)
        code = emailed_code(harness, EMAIL, keep=True)
        token = emailed_link_token(harness, EMAIL)
        await start(client)
        assert harness.emails.sent == []
        signed_in = await post(client, "email/verify-link", token=token)
        assert signed_in.status_code == 200, signed_in.text
        assert invalid(await post(client, "email/verify", email=EMAIL, code=code))


@pytest.mark.asyncio
async def test_a_new_start_replaces_the_code_and_link(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    async with harness.client() as client:
        await start(client)
        old_code = emailed_code(harness, EMAIL, keep=True)
        old_token = emailed_link_token(harness, EMAIL)
        await start(client)
        assert invalid(await post(client, "email/verify-link", token=old_token))
        assert invalid(await post(client, "email/verify", email=EMAIL, code=old_code))
        code = emailed_code(harness, EMAIL)
        assert (await post(client, "email/verify", email=EMAIL, code=code)).status_code == 200


@pytest.mark.asyncio
async def test_guessing_codes_locks_the_code_but_not_the_link(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    harness.settings.otp_lock_after_failures = 3
    async with harness.client() as client:
        await start(client)
        code = emailed_code(harness, EMAIL, keep=True)
        token = emailed_link_token(harness, EMAIL)
        for _ in range(3):
            assert invalid(await post(client, "email/verify", email=EMAIL, code=wrong(code)))
        async with harness.factory() as db:
            row = await db.scalar(select(EmailOtp))
        assert row.locked_until is not None and row.code_digest is None
        assert invalid(await post(client, "email/verify", email=EMAIL, code=code))
        # Whoever guessed cannot keep the inbox's owner out.
        assert (await post(client, "email/verify-link", token=token)).status_code == 200


@pytest.mark.asyncio
async def test_wrong_codes_are_counted_even_though_the_request_fails(harness: Harness) -> None:
    async with harness.client() as client:
        await start(client)
        code = emailed_code(harness, EMAIL)
        for _ in range(5):
            assert invalid(await post(client, "email/verify", email=EMAIL, code=wrong(code)))
        async with harness.factory() as db:
            row = await db.scalar(select(EmailOtp))
        assert (row.attempts, row.failed_attempts) == (5, 5)
        assert invalid(await post(client, "email/verify", email=EMAIL, code=code))
        assert (await user_row(harness)).email_verified_at is None
        malformed = await post(client, "email/verify", email=EMAIL, code="12345")
        assert malformed.status_code == 422


@pytest.mark.asyncio
async def test_an_expired_link_does_not_work(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    async with harness.client() as client:
        await start(client)
        token = emailed_link_token(harness, EMAIL)
        async with harness.factory() as db:
            row = await db.scalar(select(EmailOtp))
            row.expires_at = row.sent_at
            await db.commit()
        assert invalid(await post(client, "email/link-info", token=token))
        assert invalid(await post(client, "email/verify-link", token=token))


@pytest.mark.asyncio
async def test_without_an_app_url_the_email_has_the_code_only(harness: Harness) -> None:
    harness.settings.app_url = None
    async with harness.client() as client:
        await start(client)
        (message,) = harness.emails.sent
        assert "#token=" not in message["text"]
        assert "Sign-in code: " in message["text"]


@pytest.mark.asyncio
async def test_the_code_is_committed_before_the_email_goes(harness: Harness, monkeypatch) -> None:
    order: list[str] = []
    commit = AsyncSession.commit

    async def recording_commit(session):
        await commit(session)
        order.append("commit")

    async def recording_send(**message):
        order.append("send")
        return True

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)
    monkeypatch.setattr(harness.emails, "send", recording_send)
    async with harness.client() as client:
        await start(client)
    assert order[:2] == ["commit", "send"] and order.count("send") == 1


@pytest.mark.asyncio
async def test_a_refused_email_is_logged(harness: Harness, monkeypatch, caplog) -> None:
    async def refusing_send(**message):
        return False

    monkeypatch.setattr(harness.emails, "send", refusing_send)
    async with harness.client() as client:
        await start(client)
    assert "was not accepted by the provider" in caplog.text


@pytest.mark.asyncio
async def test_sign_in_endpoints_need_an_allowed_origin(harness: Harness) -> None:
    async with harness.client() as client:
        for name in ("email/start", "email/verify", "email/link-info", "email/verify-link"):
            for headers in ({}, {"Origin": "https://evil.example"}):
                refused = await client.post(
                    f"{AUTH}/{name}",
                    json={"email": EMAIL, "code": "123456", "token": "t" * 64},
                    headers=headers,
                )
                assert refused.status_code == 403, name
                assert refused.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_sending_and_checking_have_limits_of_their_own(harness: Harness) -> None:
    harness.settings.auth_session_rate_limit = 1
    harness.settings.auth_code_rate_limit = 2
    async with harness.client() as client:
        assert (await post(client, "email/start", email=EMAIL)).status_code == 200
        limited = await post(client, "email/start", email=EMAIL)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        code = emailed_code(harness, EMAIL)
        assert invalid(await post(client, "email/link-info", token="t" * 64))
        assert invalid(await post(client, "email/verify", email=EMAIL, code=wrong(code)))
        for name in ("email/verify", "email/link-info", "email/verify-link"):
            limited = await post(client, name, email=EMAIL, code=code, token="t" * 64)
            assert limited.status_code == 429, name


@pytest.mark.asyncio
async def test_an_unverified_account_gets_no_project_and_no_admin_role(harness: Harness) -> None:
    async with harness.client() as manager_client, harness.client() as client:
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        await start(client)
        project = await create_project(manager_client, manager)
        invited = await manager_client.post(
            f"{PROJECTS}/{project['id']}/members",
            json={"email": EMAIL, "role": "researcher"},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert invited.status_code == 404
        with pytest.raises(APIError, match="Sign in with an emailed code first"):
            await bootstrap_admin(EMAIL, settings=harness.settings, session_factory=harness.factory)

        code = emailed_code(harness, EMAIL)
        assert (await post(client, "email/verify", email=EMAIL, code=code)).status_code == 200
        invited = await manager_client.post(
            f"{PROJECTS}/{project['id']}/members",
            json={"email": EMAIL, "role": "researcher"},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert invited.status_code == 201, invited.text


@pytest.mark.asyncio
async def test_an_admin_created_user_verifies_at_the_first_sign_in(harness: Harness) -> None:
    async with harness.client() as admin, harness.client() as client:
        session = await login(harness, admin, uid="pa", email="pa@example.com")
        await bootstrap_admin(
            "pa@example.com", settings=harness.settings, session_factory=harness.factory
        )
        headers = mutation_headers(session["csrf_token"])
        created = await admin.post(
            USERS,
            json={"email": "hire@example.com", "display_name": "New Hire"},
            headers=headers,
        )
        assert created.json()["data"]["email_verified"] is False
        (invitation,) = harness.emails.sent
        assert "code:" not in invitation["text"].lower()
        harness.emails.sent.clear()

        signed_in = await sign_in(harness, client, "hire@example.com")
        assert signed_in.status_code == 200, signed_in.text
        # The admin named the user, so the app asks for no name.
        assert signed_in.json()["data"]["user"]["onboarded"] is True
        assert (await client.get(PROJECTS)).status_code == 200
        listed = await admin.get(USERS, params={"email": "hire@example.com"})
        assert listed.json()["data"][0]["email_verified"] is True


@pytest.mark.asyncio
async def test_an_admin_takes_over_an_address_that_never_verified(harness: Harness) -> None:
    harness.settings.app_url = "http://localhost:3000"
    async with harness.client() as admin, harness.client() as client:
        session = await login(harness, admin, uid="pa", email="pa@example.com")
        await bootstrap_admin(
            "pa@example.com", settings=harness.settings, session_factory=harness.factory
        )
        await start(client, "dat@example.com")
        squatter_code = emailed_code(harness, "dat@example.com", keep=True)
        squatter_token = emailed_link_token(harness, "dat@example.com")

        created = await admin.post(
            USERS,
            json={"email": "dat@example.com", "send_email": False},
            headers=mutation_headers(session["csrf_token"]),
        )
        assert created.status_code == 201, created.text
        assert created.json()["data"]["email_verified"] is False
        # The code and link sent before are dead.
        assert invalid(
            await post(client, "email/verify", email="dat@example.com", code=squatter_code)
        )
        assert invalid(await post(client, "email/verify-link", token=squatter_token))
