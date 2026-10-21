import base64
import json
import re
import time

from httpx import AsyncClient
from sqlalchemy import select

from management.identity.google_oauth import GoogleIdentity
from management.identity.models import User
from tests.helpers.harness import GOOGLE_CLIENT_ID, ORIGIN, Harness

AUTH = "/api/v1/auth"


def emailed_code(harness: Harness, email: str, *, keep: bool = False) -> str:
    """The 6-digit code in the latest code email to this address.

    The email is taken out of the captured list unless `keep` is set, so tests that count
    messages see only the ones they are about.
    """
    return _emailed(harness, email, r"code: (\d{6})$", keep=keep)


def emailed_link_token(harness: Harness, email: str, *, keep: bool = False) -> str:
    """The token of the sign-in link in the latest code email to this address."""
    return _emailed(harness, email, r"#token=([\w-]{64})$", keep=keep)


def _emailed(harness: Harness, email: str, pattern: str, *, keep: bool) -> str:
    for message in reversed(harness.emails.sent):
        found = re.search(pattern, message["text"], re.MULTILINE)
        if message["to"].casefold() == email.casefold() and found:
            if not keep:
                harness.emails.sent.remove(message)
            return found.group(1)
    raise AssertionError(f"nothing matching {pattern!r} was emailed to {email}")


async def sign_in(harness: Harness, client: AsyncClient, email: str):
    """Ask for a code for this address and enter it. Returns the verify response."""
    headers = {"Origin": ORIGIN}
    started = await client.post(f"{AUTH}/email/start", json={"email": email}, headers=headers)
    assert started.status_code == 200, started.text
    return await client.post(
        f"{AUTH}/email/verify",
        json={"email": email, "code": emailed_code(harness, email)},
        headers=headers,
    )


async def login(
    harness: Harness, client: AsyncClient, *, uid: str, email: str
) -> dict[str, object]:
    """Sign in as the user with this email, creating it and naming it after `uid` at first."""
    response = await sign_in(harness, client, email)
    assert response.status_code == 200, response.text
    session = response.json()["data"]
    if not session["user"]["onboarded"]:
        named = await client.patch(
            f"{AUTH}/me",
            json={"display_name": uid.replace("-", " ").title()},
            headers=mutation_headers(session["csrf_token"]),
        )
        assert named.status_code == 200, named.text
        session = {**session, "user": named.json()["data"]}
    return session


def mutation_headers(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


USERS = "/api/v1/users"
EMAIL = "new@example.com"


async def post(client: AsyncClient, name: str, **body: object):
    return await client.post(f"{AUTH}/{name}", json=body, headers={"Origin": ORIGIN})


async def start(client: AsyncClient, email: str = EMAIL):
    response = await post(client, "email/start", email=email)
    assert response.status_code == 200, response.text
    return response


def invalid(response) -> bool:
    return response.status_code == 400 and response.json()["error"]["code"] == "OTP_INVALID"


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


async def user_row(harness: Harness, email: str = EMAIL) -> User:
    async with harness.factory() as db:
        return await db.scalar(select(User).where(User.email_normalized == email))


async def upload(client: AsyncClient, session: dict, content: bytes, *, url: str | None = None):
    return await client.post(
        url or f"{AUTH}/me/avatar",
        # The name and content type lie on purpose: the server decides from the bytes.
        files={"file": ("picture.txt", content, "text/plain")},
        headers=mutation_headers(session["csrf_token"]),
    )


CLIENT_SECRET = "google-secret-that-must-never-be-logged"
GOOGLE_EMAIL = "dat@gmail.com"
DAT = GoogleIdentity(
    subject="google-sub-1",
    email=GOOGLE_EMAIL,
    email_verified=True,
    hosted_domain=None,
    name="Dat Ngo",
)


def id_token(**changes: object) -> str:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": GOOGLE_CLIENT_ID,
        "exp": int(time.time()) + 3600,
        "sub": DAT.subject,
        "email": GOOGLE_EMAIL,
        "email_verified": True,
        "name": DAT.name,
    } | changes
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.signature"


def cookies_set(response) -> dict[str, str]:
    """Each cookie the response sets, by name, with its whole Set-Cookie line."""
    return {line.split("=", 1)[0]: line for line in response.headers.get_list("set-cookie")}


OWNER_EMAIL = "owner@example.com"
