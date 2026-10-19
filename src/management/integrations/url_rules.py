"""Checking a pasted webhook URL against what its provider issues.

The URL is parsed with httpx, the same parser that later sends to it, so a URL cannot pass the
check under one reading and reach another host under a different one.
"""

import ipaddress
import re

import httpx

from management.core.errors import APIError
from management.integrations.catalog import PROVIDERS

MAX_URL_LENGTH = 2048


def _invalid(provider_label: str) -> APIError:
    return APIError(
        422,
        "INTEGRATION_URL_INVALID",
        f"This is not a {provider_label} webhook URL. Copy the URL again from {provider_label}.",
    )


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in allowed)


def validate_webhook_url(provider_key: str, raw: str) -> str:
    """Return the URL to store and send to, or raise 422 INTEGRATION_URL_INVALID."""
    provider = PROVIDERS[provider_key]
    error = _invalid(provider.label)
    text = raw.strip()
    if not text or len(text) > MAX_URL_LENGTH or "\\" in text:
        raise error
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise error
    try:
        url = httpx.URL(text)
        host = url.raw_host.decode("ascii").lower()
    except (httpx.InvalidURL, UnicodeError):
        raise error from None
    if host.endswith("."):
        host = host[:-1]
    if (
        url.scheme != "https"
        or url.userinfo
        or url.port not in (None, 443)
        or not host
        or _is_ip(host)
        or not _host_allowed(host, provider.hosts)
        or url.fragment
        or (url.query and not provider.allows_query)
    ):
        raise error
    path = url.raw_path.decode("ascii").split("?", 1)[0]
    if provider.path_pattern is not None and not re.fullmatch(provider.path_pattern, path):
        raise error
    if provider.path_pattern is None and path in ("", "/"):
        raise error
    return f"https://{host}{url.raw_path.decode('ascii')}"


def url_hint(url: str) -> str:
    """Enough to tell two webhooks apart, never enough to post with: host and 4 last path chars."""
    parsed = httpx.URL(url)
    path = parsed.raw_path.decode("ascii").split("?", 1)[0].rstrip("/")
    return f"{parsed.host}/…{path[-4:]}"
