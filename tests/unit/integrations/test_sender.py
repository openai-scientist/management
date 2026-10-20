import logging

import httpx
import pytest

from management.integrations.sender import MAX_RETRY_AFTER_SECONDS, WebhookSender

pytestmark = pytest.mark.unit

URL = "https://hooks.slack.com/services/T000/B000/secretTOKEN9876"


async def public(_host: str, _port: int) -> list[str]:
    return ["34.1.2.3"]


def sender_answering(handler, resolver=public) -> WebhookSender:
    return WebhookSender(transport=httpx.MockTransport(handler), resolver=resolver)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "outcome", "code", "disables"),
    [
        (200, "sent", None, False),
        (204, "sent", None, False),
        (302, "permanent", "PROVIDER_REJECTED", False),
        (400, "permanent", "PROVIDER_REJECTED", False),
        (401, "permanent", "WEBHOOK_UNAUTHORIZED", True),
        (403, "permanent", "WEBHOOK_UNAUTHORIZED", True),
        (404, "permanent", "WEBHOOK_NOT_FOUND", True),
        (410, "permanent", "WEBHOOK_NOT_FOUND", True),
        (500, "retryable", "PROVIDER_ERROR", False),
        (503, "retryable", "PROVIDER_ERROR", False),
    ],
)
async def test_maps_each_reply(status: int, outcome: str, code: str | None, disables: bool) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, headers={"location": "https://evil.example"})

    result = await sender_answering(handler).send("slack", URL, {"text": "hi"})
    assert (result.outcome, result.status, result.error_code) == (outcome, status, code)
    assert result.disables is disables
    assert result.ok is (outcome == "sent")
    # No redirect is followed.
    assert [str(request.url) for request in sent] == [URL]
    assert sent[0].content == b'{"text":"hi"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "seconds"),
    [("12", 12), ("0.5", 1), ("99999", MAX_RETRY_AFTER_SECONDS), ("soon", 60), (None, 60)],
)
async def test_rate_limit_waits_as_asked_up_to_a_cap(header: str | None, seconds: int) -> None:
    headers = {"retry-after": header} if header else {}
    result = await sender_answering(lambda _r: httpx.Response(429, headers=headers)).send(
        "discord", URL, {}
    )
    assert (result.outcome, result.error_code, result.retry_after) == (
        "rate_limited",
        "PROVIDER_RATE_LIMITED",
        seconds,
    )
    assert not result.disables


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ConnectTimeout("timed out " + URL), "PROVIDER_TIMEOUT"),
        (httpx.ReadTimeout("timed out " + URL), "PROVIDER_TIMEOUT"),
        (httpx.ConnectError("refused " + URL), "PROVIDER_ERROR"),
    ],
)
async def test_network_failures_are_retried(
    error: Exception, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    with caplog.at_level(logging.DEBUG):
        result = await sender_answering(handler).send("slack", URL, {})
    assert (result.outcome, result.status, result.error_code) == ("retryable", None, code)
    assert "secretTOKEN" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses", [["10.0.0.5"], ["34.1.2.3", "127.0.0.1"], ["::ffff:169.254.169.254"], []]
)
async def test_refuses_a_host_that_resolves_inward(addresses: list[str]) -> None:
    async def resolver(_host: str, _port: int) -> list[str]:
        return addresses

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not connect")

    result = await sender_answering(handler, resolver).send("slack", URL, {})
    assert (result.outcome, result.error_code, result.disables) == (
        "permanent",
        "WEBHOOK_HOST_BLOCKED",
        True,
    )


@pytest.mark.asyncio
async def test_failed_lookup_is_retried() -> None:
    async def resolver(_host: str, _port: int) -> list[str]:
        raise OSError("no such host")

    result = await sender_answering(lambda _r: httpx.Response(200), resolver).send("slack", URL, {})
    assert (result.outcome, result.error_code) == ("retryable", "PROVIDER_ERROR")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404, 500])
async def test_request_log_keeps_only_the_host(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await sender_answering(lambda _r: httpx.Response(status, text="secretTOKEN9876")).send(
            "slack", URL, {}
        )
    assert "hooks.slack.com" in caplog.text
    assert "secretTOKEN" not in caplog.text
    assert "T000" not in caplog.text
