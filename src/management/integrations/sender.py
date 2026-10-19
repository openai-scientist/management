"""Post one payload to one webhook and say what came of it.

The webhook URL is a secret: Slack and Discord carry the token in the path, Teams in the
query. Nothing here logs it, puts it in an error, or keeps the provider's reply body.
"""

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from fastapi import Request

from management.integrations.catalog import PROVIDERS

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
MAX_RETRY_AFTER_SECONDS = 15 * 60
# Codes that mean the webhook itself is gone or refused: retrying cannot help, and the
# integration stops until someone pastes a working URL.
DISABLING_CODES = frozenset({"WEBHOOK_NOT_FOUND", "WEBHOOK_UNAUTHORIZED", "WEBHOOK_HOST_BLOCKED"})

Outcome = Literal["sent", "retryable", "rate_limited", "permanent"]
Resolver = Callable[[str, int], Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: Outcome
    status: int | None = None
    error_code: str | None = None
    # Seconds the provider asked us to wait, capped; only for `rate_limited`.
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "sent"

    @property
    def disables(self) -> bool:
        return self.error_code in DISABLING_CODES


def _retry_after(value: str | None) -> int:
    seconds = 60
    if value:
        try:
            seconds = int(float(value))
        except ValueError:
            try:
                seconds = int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                pass
    return max(1, min(seconds, MAX_RETRY_AFTER_SECONDS))


def classify(response: httpx.Response) -> SendResult:
    status = response.status_code
    if 200 <= status < 300:
        return SendResult("sent", status)
    if status == 429:
        return SendResult(
            "rate_limited",
            status,
            "PROVIDER_RATE_LIMITED",
            _retry_after(response.headers.get("retry-after")),
        )
    if status in (404, 410):
        return SendResult("permanent", status, "WEBHOOK_NOT_FOUND")
    if status in (401, 403):
        return SendResult("permanent", status, "WEBHOOK_UNAUTHORIZED")
    if status >= 500:
        return SendResult("retryable", status, "PROVIDER_ERROR")
    # Any other 4xx means our payload was refused; a 3xx is a webhook we will not follow.
    return SendResult("permanent", status, "PROVIDER_REJECTED")


def _is_public(text: str) -> bool:
    address = ipaddress.ip_address(text.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class WebhookSender:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._transport = transport
        self._resolver = resolver

    async def send(self, provider: str, url: str, payload: dict[str, Any]) -> SendResult:
        target = httpx.URL(url)
        host = target.host
        # Saved URLs only name the provider's own hosts. Checking where the name points
        # still keeps a poisoned or internal DNS answer from turning a send inward.
        try:
            addresses = await self._resolver(host, 443)
        except OSError:
            return self._log(provider, SendResult("retryable", None, "PROVIDER_ERROR"))
        if not addresses or not all(_is_public(address) for address in addresses):
            return self._log(provider, SendResult("permanent", None, "WEBHOOK_HOST_BLOCKED"))
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(target, json=payload)
        except httpx.TimeoutException:
            result = SendResult("retryable", None, "PROVIDER_TIMEOUT")
        except httpx.HTTPError:
            # The exception text holds the URL; only its type is safe to keep.
            result = SendResult("retryable", None, "PROVIDER_ERROR")
        else:
            result = classify(response)
        return self._log(provider, result)

    @staticmethod
    def _log(provider: str, result: SendResult) -> SendResult:
        if not result.ok:
            logger.info(
                "Webhook send failed",
                extra={
                    "provider": provider,
                    "status": result.status,
                    "error_code": result.error_code,
                },
            )
        return result


def get_webhook_sender(request: Request) -> WebhookSender:
    return request.app.state.webhook_sender


_WEBHOOK_HOSTS = tuple(host for provider in PROVIDERS.values() for host in provider.hosts)


class _HideWebhookPath(logging.Filter):
    """Keep only the host of a webhook URL in the line httpx logs per request.

    The Platform's own filter cuts the query string, but Slack and Discord put the token in
    the path.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 2:
            try:
                url = httpx.URL(str(args[1]))
            except (httpx.InvalidURL, TypeError, ValueError):
                return True
            host = url.host
            if any(host == known or host.endswith("." + known) for known in _WEBHOOK_HOSTS):
                record.args = (args[0], f"{url.scheme}://{host}/…", *args[2:])
        return True


def hide_webhook_paths() -> None:
    outgoing = logging.getLogger("httpx")
    if not any(isinstance(item, _HideWebhookPath) for item in outgoing.filters):
        outgoing.addFilter(_HideWebhookPath())


hide_webhook_paths()
