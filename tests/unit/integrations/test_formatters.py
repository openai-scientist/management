import json

import pytest

from management.integrations.formatters import payload_for
from management.integrations.messages import Message, escape_for, sample_message

pytestmark = pytest.mark.unit

HOSTILE = "@everyone [x](https://evil) <!channel> <https://evil|ok> *bold* #h"
LINK = "https://app.example.com/projects/0d9b7c1e-0000-4000-8000-000000000001/runs/1"
MESSAGE = Message(
    title="New comment",
    headline="Lan <!here> commented on Study time",
    tone="info",
    facts=(("Project", "Lab"),),
    quote=HOSTILE,
    link_label="Open discussion",
    link_url=LINK,
)


def test_slack_attachment_with_colour_bar() -> None:
    assert payload_for("slack", MESSAGE) == {
        "text": "New comment: Lan &lt;!here&gt; commented on Study time",
        "attachments": [
            {
                "color": "#5865F2",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "New comment"}},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Lan &lt;!here&gt; commented on Study time*\n"
                            ">@everyone [x](https://evil) &lt;!channel&gt; "
                            "&lt;https://evil|ok&gt; *bold* #h",
                        },
                    },
                    {"type": "section", "fields": [{"type": "mrkdwn", "text": "*Project*\nLab"}]},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Open discussion"},
                                "url": LINK,
                            }
                        ],
                    },
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "AI Research Platform"}],
                    },
                ],
            }
        ],
    }


def test_teams_adaptive_card_in_workflows_envelope() -> None:
    payload = payload_for("teams", MESSAGE)
    [attachment] = payload["attachments"]
    assert payload["type"] == "message"
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["body"] == [
        {
            "type": "Container",
            "style": "accent",
            "bleed": True,
            "items": [
                {"type": "TextBlock", "text": "New comment", "weight": "Bolder", "wrap": True}
            ],
        },
        {
            "type": "TextBlock",
            "text": "Lan <!here> commented on Study time",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": r"@everyone \[x\]\(https://evil\) <!channel> <https://evil|ok> \*bold\* \#h",
            "isSubtle": True,
            "wrap": True,
        },
        {"type": "FactSet", "facts": [{"title": "Project", "value": "Lab"}]},
        {
            "type": "TextBlock",
            "text": "AI Research Platform",
            "size": "Small",
            "isSubtle": True,
            "wrap": True,
        },
    ]
    assert card["actions"] == [{"type": "Action.OpenUrl", "title": "Open discussion", "url": LINK}]


def test_discord_embed_resolves_no_mentions() -> None:
    payload = payload_for("discord", MESSAGE)
    assert payload["allowed_mentions"] == {"parse": []}
    [embed] = payload["embeds"]
    assert embed.pop("timestamp")
    assert embed == {
        "title": "New comment",
        "description": r"**Lan <!here\> commented on Study time**"
        "\n"
        r"> \@everyone \[x\]\(https://evil\) <!channel\> <https://evil\|ok\> \*bold\* \#h"
        f"\n\n[Open discussion →]({LINK})",
        "color": 0x5865F2,
        "footer": {"text": "AI Research Platform"},
        "fields": [{"name": "Project", "value": "Lab", "inline": True}],
        "url": LINK,
    }


@pytest.mark.parametrize(
    ("tone", "slack", "teams", "discord"),
    [
        ("success", "#2EB67D", "good", 0x2EB67D),
        ("danger", "#E01E5A", "attention", 0xE01E5A),
        ("warning", "#ECB22E", "warning", 0xECB22E),
        ("neutral", "#99AAB5", "emphasis", 0x99AAB5),
    ],
)
def test_each_tone_has_its_colour(tone: str, slack: str, teams: str, discord: int) -> None:
    message = Message("T", "H", tone)  # type: ignore[arg-type]
    assert payload_for("slack", message)["attachments"][0]["color"] == slack
    card = payload_for("teams", message)["attachments"][0]["content"]
    assert card["body"][0]["style"] == teams
    assert payload_for("discord", message)["embeds"][0]["color"] == discord


@pytest.mark.parametrize("provider", ["slack", "teams", "discord"])
def test_message_without_link_quote_or_facts_stays_short(provider: str) -> None:
    payload = json.dumps(payload_for(provider, Message("Hello", "World")))
    for absent in ("button", "Action.OpenUrl", "FactSet", "fields", '"url"', ">"):
        assert absent not in payload


def test_escaping_survives_its_own_escape_character() -> None:
    assert escape_for("discord", "\\*") == "\\\\\\*"
    assert escape_for("teams", "\\_") == "\\\\\\_"
    assert escape_for("slack", "a & b") == "a &amp; b"


def test_sample_message_names_the_project_only_once_saved() -> None:
    draft = sample_message(None, None)
    assert draft.facts == () and draft.tone == "success"
    saved = sample_message("Lab", "Team")
    assert saved.title == "Integration connected"
    assert saved.facts == (("Project", "Lab"), ("Integration", "Team"))
