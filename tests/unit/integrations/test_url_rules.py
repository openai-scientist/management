import pytest

from management.core.errors import APIError
from management.integrations.url_rules import url_hint, validate_webhook_url

pytestmark = pytest.mark.unit

SLACK = "https://hooks.slack.com/services/T000/B000/abcdEFGH1234"
DISCORD = "https://discord.com/api/webhooks/123456789/abc_DEF-123"
TEAMS = (
    "https://prod-01.westeurope.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke"
    "?api-version=2016-06-01&sig=secret"
)


@pytest.mark.parametrize(
    ("provider", "url", "stored"),
    [
        ("slack", SLACK, SLACK),
        ("slack", "  https://HOOKS.slack.com./triggers/T1/123/abc  ", None),
        ("discord", DISCORD, DISCORD),
        ("discord", "https://discordapp.com/api/v10/webhooks/1/tok", None),
        ("discord", "https://canary.discord.com/api/webhooks/1/tok", None),
        ("teams", TEAMS, None),
        (
            "teams",
            "https://x.environment.api.powerplatform.com/powerautomate/automations/direct/a",
            None,
        ),
    ],
)
def test_accepts_provider_urls(provider: str, url: str, stored: str | None) -> None:
    result = validate_webhook_url(provider, url)
    assert result.startswith("https://")
    if stored is not None:
        assert result == stored


def test_normalises_host_case_and_trailing_dot() -> None:
    assert (
        validate_webhook_url("slack", "https://HOOKS.slack.com./triggers/T1/123/abc")
        == "https://hooks.slack.com/triggers/T1/123/abc"
    )


def test_teams_keeps_its_signed_query() -> None:
    assert validate_webhook_url("teams", TEAMS).endswith("sig=secret")


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("slack", "http://hooks.slack.com/services/T/B/x"),
        ("slack", "https://hooks.slack.com.evil.com/services/T/B/x"),
        ("slack", "https://evilhooks.slack.com.example/services/T/B/x"),
        ("slack", "https://user:pw@hooks.slack.com/services/T/B/x"),
        ("slack", "https://hooks.slack.com:8443/services/T/B/x"),
        ("slack", "https://hooks.slack.com/services/T/B/x?redirect=1"),
        ("slack", "https://hooks.slack.com/services/T/B/x#frag"),
        ("slack", "https://hooks.slack.com/api/other"),
        ("slack", "https://hooks.slack.com\\@evil.com/services/T/B/x"),
        ("slack", "https://hooks.slack.com/services/T/B/x y"),
        ("slack", DISCORD),
        ("discord", "https://discord.com/api/webhooks/notanumber/tok"),
        ("discord", "https://discord.com/channels/1/2"),
        ("discord", "https://xn--dscord-0ta.com/api/webhooks/1/tok"),
        ("teams", "https://evillogic.azure.com/workflows/abc"),
        ("teams", "https://logic.azure.com.evil.net/workflows/abc"),
        ("teams", "https://prod.logic.azure.com/"),
        ("teams", "https://127.0.0.1/workflows"),
        ("teams", "https://[::1]/workflows"),
        ("teams", ""),
        ("teams", "not a url"),
        ("discord", "https://discord.com/api/webhooks/1/" + "a" * 2100),
    ],
)
def test_rejects_other_urls(provider: str, url: str) -> None:
    with pytest.raises(APIError) as caught:
        validate_webhook_url(provider, url)
    assert caught.value.status_code == 422
    assert caught.value.code == "INTEGRATION_URL_INVALID"


def test_hint_shows_host_and_tail_only() -> None:
    assert url_hint(SLACK) == "hooks.slack.com/…1234"
    hint = url_hint(validate_webhook_url("teams", TEAMS))
    assert "sig" not in hint and "secret" not in hint
