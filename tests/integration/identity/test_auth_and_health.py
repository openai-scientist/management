from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from management.audit.models import AuditEvent
from management.core.errors import APIError
from management.core.sessions import normalize_email
from management.identity.bootstrap_admin import bootstrap_admin
from management.identity.models import AuthSession, User, UserPlatformRole
from tests.helpers.harness import ORIGIN, Harness
from tests.helpers.identity import emailed_code, login, mutation_headers, post, sign_in, start


@pytest.mark.asyncio
async def test_health_probes_answer_and_openapi_keeps_its_guards(harness: Harness) -> None:
    async with harness.client() as client:
        live = await client.get("/api/v1/health/live")
        ready = await client.get("/api/v1/health/ready")
    assert live.status_code == 200
    assert live.json() == {
        "success": True,
        "message": "OK",
        "data": {"status": "ok"},
        "meta": {"request_id": live.headers["x-request-id"], "pagination": None},
    }
    assert ready.status_code == 200
    assert ready.json()["data"] == {"status": "ready"}
    schema = harness.app.openapi()
    # Listed even while the feature is off.
    assert {
        "/api/v1/projects/{project_id}/connections/google/start",
        "/api/v1/connections/google/callback",
        "/api/v1/projects/{project_id}/connections/{connection_id}/reauthorize",
    } <= set(schema["paths"])
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "/api/v1/research" not in schema["paths"]


@pytest.mark.asyncio
async def test_sign_in_requires_an_allowed_origin(harness: Harness) -> None:
    async with harness.client() as client:
        response = await client.post(
            "/api/v1/auth/email/start",
            json={"email": "origin@example.com"},
            headers={"Origin": "https://untrusted.example"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"

    async with harness.factory() as db:
        assert await db.scalar(select(User.id)) is None


@pytest.mark.asyncio
async def test_sign_in_rate_limit_and_request_body_limit(harness: Harness) -> None:
    harness.settings.auth_session_rate_limit = 2
    async with harness.client() as client:
        email = "rate-limit@example.com"
        await start(client, email)
        # Entering the code is counted elsewhere, so it does not use up a sign-in.
        code = emailed_code(harness, email)
        signed_in = await post(client, "email/verify", email=email, code=code)
        assert signed_in.status_code == 200, signed_in.text
        await start(client, email)

        limited = await post(client, "email/start", email=email)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.headers.get("x-request-id")

        harness.settings.auth_session_rate_limit = 100
        harness.settings.request_max_body_bytes = 1024
        oversized = await post(client, "email/verify", email=email, code="1" * 1100)
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
        assert oversized.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_session_token_is_stored_as_digest_and_admin_bootstrap_is_one_time(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        unauthenticated_profile = await client.get("/api/v1/auth/me")
        assert unauthenticated_profile.status_code == 401

        session = await login(harness, client, uid="first-admin", email="Admin@Example.com")
        raw_cookie = client.cookies.get(harness.settings.session_cookie_name)
        assert raw_cookie
        assert session["user"]["status"] == "active"

        await bootstrap_admin(
            "admin@example.com",
            settings=harness.settings,
            session_factory=harness.factory,
        )
        async with harness.factory() as db:
            user = await db.scalar(
                select(User).where(User.email_normalized == normalize_email("admin@example.com"))
            )
            stored_session = await db.scalar(
                select(AuthSession).where(AuthSession.user_id == user.id)
            )
            role = await db.get(UserPlatformRole, user.id)
        assert user.status == "active"
        assert role.role_code == "platform_admin"
        assert stored_session.token_digest != raw_cookie
        assert len(stored_session.token_digest) == 64

        profile = await client.get("/api/v1/auth/me")
        assert profile.status_code == 200
        current = profile.json()["data"]
        assert current["user"]["email"] == "Admin@example.com"
        assert current["user"]["status"] == "active"
        assert current["user"]["platform_role"] == "platform_admin"
        assert current["session"]["absolute_expires_at"]
        assert current["session"]["idle_expires_at"]
        csrf_response = await client.get("/api/v1/auth/csrf-token")
        assert csrf_response.status_code == 200
        assert csrf_response.json()["data"]["csrf_token"] == session["csrf_token"]
        with pytest.raises(APIError, match="already bootstrapped"):
            await bootstrap_admin(
                "admin@example.com",
                settings=harness.settings,
                session_factory=harness.factory,
            )


@pytest.mark.asyncio
async def test_mutation_requires_csrf_and_the_last_admin_cannot_be_suspended(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="pa", email="pa@example.com")
        await bootstrap_admin(
            "pa@example.com", settings=harness.settings, session_factory=harness.factory
        )
        user_id = session["user"]["id"]
        response = await client.patch(
            f"/api/v1/users/{user_id}/status",
            json={"status": "suspended"},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"
        csrf = session["csrf_token"]
        response = await client.patch(
            f"/api/v1/users/{user_id}/status",
            json={"status": "suspended"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        # A Platform Admin cannot suspend the final active Platform Admin.
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LAST_PLATFORM_ADMIN"


@pytest.mark.asyncio
async def test_logout_requires_csrf_and_revokes_cookie_session(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="logout", email="logout@example.com")
        missing_csrf = await client.post("/api/v1/auth/logout", headers={"Origin": ORIGIN})
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["error"]["code"] == "CSRF_INVALID"

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]},
        )
        assert logout.status_code == 200
        assert logout.json()["success"] is True
        assert logout.json()["data"] is None
        assert logout.json()["message"] == "Signed out"
        assert client.cookies.get(harness.settings.session_cookie_name) is None
        profile = await client.get("/api/v1/auth/me")
        assert profile.status_code == 401
        assert profile.json()["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.asyncio
async def test_a_suspended_account_cannot_use_a_code_it_was_sent(harness: Harness) -> None:
    email = "suspended@example.com"
    async with harness.client() as client:
        await login(harness, client, uid="suspended", email=email)
        await start(client, email)
        code = emailed_code(harness, email)
        async with harness.factory() as db, db.begin():
            (await db.scalar(select(User))).status = "suspended"
        refused = await post(client, "email/verify", email=email, code=code)

    # The same answer as a wrong code: the response does not tell who is suspended.
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "OTP_INVALID"


@pytest.mark.asyncio
async def test_errors_share_one_envelope(harness: Harness) -> None:
    async with harness.client() as client:
        unknown = await client.get("/api/v1/does-not-exist")
        wrong_method = await client.put("/api/v1/auth/email/start")
        invalid = await client.post(
            "/api/v1/auth/email/start", json={"email": "not-an-email"}, headers={"Origin": ORIGIN}
        )
        harness.app.state.settings = None
        crashed = await client.get("/api/v1/auth/me", headers={"Origin": ORIGIN})

    for response, status, code in (
        (unknown, 404, "NOT_FOUND"),
        (wrong_method, 405, "METHOD_NOT_ALLOWED"),
        (invalid, 422, "VALIDATION_ERROR"),
        (crashed, 500, "INTERNAL_ERROR"),
    ):
        body = response.json()
        assert response.status_code == status
        assert set(body) == {"success", "message", "error", "meta"}
        assert body["success"] is False
        assert body["error"]["code"] == code
        assert body["meta"]["request_id"] == response.headers["x-request-id"]
    assert invalid.json()["error"]["details"][0]["field"] == "body.email"
    assert crashed.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.asyncio
async def test_dead_session_clears_cookie(harness: Harness) -> None:
    async with harness.client() as client:
        await login(harness, client, uid="expired", email="expired@example.com")
        async with harness.factory() as db, db.begin():
            stored = await db.scalar(select(AuthSession))
            stored.idle_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    assert harness.settings.session_cookie_name in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_logout_all_revokes_every_session_of_the_user(harness: Harness) -> None:
    async with harness.client() as laptop, harness.client() as phone, harness.client() as other:
        session = await login(harness, laptop, uid="multi", email="multi@example.com")
        await login(harness, phone, uid="multi", email="multi@example.com")
        await login(harness, other, uid="bystander", email="bystander@example.com")

        response = await laptop.post(
            "/api/v1/auth/logout-all",
            headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] is None
        assert (await phone.get("/api/v1/auth/me")).status_code == 401
        assert (await laptop.get("/api/v1/auth/me")).status_code == 401
        assert (await other.get("/api/v1/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_platform_admin_creates_a_user_who_can_then_sign_in(harness: Harness) -> None:
    new_user = {"email": "Hired@Example.com", "display_name": " New Hire "}
    async with harness.client() as admin, harness.client() as member:
        admin_session = await login(harness, admin, uid="pa", email="pa@example.com")
        await bootstrap_admin(
            "pa@example.com", settings=harness.settings, session_factory=harness.factory
        )
        member_session = await login(harness, member, uid="member", email="member@example.com")
        headers = mutation_headers(admin_session["csrf_token"])

        missing_csrf = await admin.post("/api/v1/users", json=new_user, headers={"Origin": ORIGIN})
        not_admin = await member.post(
            "/api/v1/users", json=new_user, headers=mutation_headers(member_session["csrf_token"])
        )
        created = await admin.post("/api/v1/users", json=new_user, headers=headers)
        # Creating an account must not replace the admin's own session.
        assert (await admin.get("/api/v1/auth/me")).json()["data"]["user"]["email"] == (
            "pa@example.com"
        )
        listed = await admin.get("/api/v1/users", params={"q": "hired@example.com"})
        async with harness.client() as client:
            signed_in = await sign_in(harness, client, "hired@example.com")
        again = await admin.post(
            "/api/v1/users", json={"email": "hired@example.com"}, headers=headers
        )

    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["error"]["code"] == "CSRF_INVALID"
    assert not_admin.status_code == 403
    assert not_admin.json()["error"]["code"] == "ROLE_REQUIRED"
    assert created.status_code == 201, created.text
    assert created.json()["message"] == "User created"
    user = created.json()["data"]
    assert user["email"] == "Hired@example.com"
    assert user["display_name"] == "New Hire"
    assert user["status"] == "active"
    assert user["platform_role"] == "user"
    assert user["email_verified"] is False
    assert user["created_by_user_id"] == admin_session["user"]["id"]
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert [item["id"] for item in listed.json()["data"]] == [user["id"]]
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["data"]["user"]["id"] == user["id"]

    async with harness.factory() as db:
        event = await db.scalar(select(AuditEvent).where(AuditEvent.action == "user.created"))
    assert str(event.actor_user_id) == admin_session["user"]["id"]
    assert event.resource_id == user["id"]
