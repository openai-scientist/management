import base64
import json
import re
import time

from httpx import AsyncClient
from sqlalchemy import select

from management.identity.google_oauth import GoogleIdentity
from management.identity.models import User
from tests.helpers.harness import GOOGLE_CLIENT_ID, ORIGIN, Harness

PASSWORD = "correct horse battery"


def emailed_code(harness: Harness, email: str, *, keep: bool = False) -> str:
    """The 6-digit code in the latest code email to this address.

    The email is taken out of the captured list unless `keep` is set, so tests that count
    messages see only the ones they are about.
    """
    for message in reversed(harness.emails.sent):
        found = re.search(r"code: (\d{6})$", message["text"], re.MULTILINE)
        if message["to"].casefold() == email.casefold() and found:
            if not keep:
                harness.emails.sent.remove(message)
            return found.group(1)
    raise AssertionError(f"no code was emailed to {email}")


async def verify(
    harness: Harness, client: AsyncClient, email: str, password: str = PASSWORD
) -> dict[str, object]:
    """Enter the emailed code for this account, which signs it in."""
    response = await client.post(
        "/api/v1/auth/verify-email",
        json={"email": email, "code": emailed_code(harness, email), "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def login(
    harness: Harness, client: AsyncClient, *, uid: str, email: str
) -> dict[str, object]:
    """Sign in as the user with this email, registering and verifying it the first time."""
    headers = {"Origin": ORIGIN}
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": uid.replace("-", " ").title(),
        },
        headers=headers,
    )
    if response.status_code == 201:
        return await verify(harness, client, email)
    assert response.status_code == 409, response.text
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def sign_in(harness: Harness, client: AsyncClient, email: str, password: str):
    """Sign in and return the response, entering an emailed code if the account needs one.

    An account an admin created is unverified until its user does this the first time.
    """
    headers = {"Origin": ORIGIN}
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=headers
    )
    if response.status_code != 403 or response.json()["error"]["code"] != "EMAIL_NOT_VERIFIED":
        return response
    await client.post("/api/v1/auth/resend-verification", json={"email": email}, headers=headers)
    return await client.post(
        "/api/v1/auth/verify-email",
        json={"email": email, "code": emailed_code(harness, email), "password": password},
        headers=headers,
    )


def mutation_headers(csrf: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf}


AUTH = "/api/v1/auth"
USERS = "/api/v1/users"
EMAIL = "new@example.com"


async def post(client: AsyncClient, name: str, **body: object):
    return await client.post(f"{AUTH}/{name}", json=body, headers={"Origin": ORIGIN})


async def register(client: AsyncClient, email: str = EMAIL, password: str = PASSWORD):
    response = await post(client, "register", email=email, password=password)
    assert response.status_code == 201, response.text
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
NEW_PASSWORD = "a brand new password"


async def reset(client, code: str, *, email: str = OWNER_EMAIL, new_password: str = NEW_PASSWORD):
    verified = await post(client, "verify-reset-password", email=email, code=code)
    if verified.status_code != 200:
        return verified
    return await post(
        client,
        "reset-password",
        email=email,
        reset_token=verified.json()["data"]["reset_token"],
        new_password=new_password,
    )


async def forgot(harness: Harness, client, email: str = OWNER_EMAIL) -> str:
    response = await post(client, "forgot-password", email=email)
    assert response.status_code == 200, response.text
    return emailed_code(harness, email)
