import hashlib
import hmac
import secrets


def new_session_secret() -> str:
    return secrets.token_urlsafe(48)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_otp_code() -> str:
    """Six random digits, leading zeros kept."""
    return f"{secrets.randbelow(10**6):06d}"


def otp_digest(signing_secret: str, purpose: str, user_id: object, code: str) -> str:
    """What is stored for a one-time code: bound to its user and purpose, useless if leaked."""
    message = f"{purpose}:{user_id}:{code}".encode()
    return hmac.new(signing_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def csrf_token(session_secret: str, signing_secret: str) -> str:
    return hmac.new(
        signing_secret.encode("utf-8"), session_secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def service_key_matches(expected: str | None, candidate: str | None) -> bool:
    """Compare a shared service key in constant time. No configured key matches nothing."""
    if not expected or not candidate:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), candidate.encode("utf-8"))


def csrf_matches(session_secret: str, signing_secret: str, candidate: str | None) -> bool:
    if not candidate:
        return False
    expected = csrf_token(session_secret, signing_secret)
    return hmac.compare_digest(expected, candidate)
