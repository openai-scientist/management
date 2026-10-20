"""Lay a `Message` out as the JSON body each provider's incoming webhook accepts."""

from datetime import UTC, datetime
from typing import Any

from management.integrations.messages import FOOTER, Message, Tone, escape_for

_COLOURS: dict[Tone, str] = {
    "success": "2EB67D",
    "danger": "E01E5A",
    "warning": "ECB22E",
    "info": "5865F2",
    "neutral": "99AAB5",
}
# Adaptive Card container styles closest to each tone.
_TEAMS_STYLES: dict[Tone, str] = {
    "success": "good",
    "danger": "attention",
    "warning": "warning",
    "info": "accent",
    "neutral": "emphasis",
}


def _slack(message: Message) -> dict[str, Any]:
    def esc(text: str) -> str:
        return escape_for("slack", text)

    body = f"*{esc(message.headline)}*"
    if message.quote:
        body += f"\n>{esc(message.quote)}"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": message.title}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    if message.facts:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{label}*\n{esc(value)}"}
                    for label, value in message.facts
                ],
            }
        )
    if message.link_url and message.link_label:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": message.link_label},
                        "url": message.link_url,
                    }
                ],
            }
        )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": FOOTER}]})
    # An attachment carries the coloured bar; `text` is what notifications show.
    return {
        "text": f"{esc(message.title)}: {esc(message.headline)}",
        "attachments": [{"color": f"#{_COLOURS[message.tone]}", "blocks": blocks}],
    }


def _teams(message: Message) -> dict[str, Any]:
    def esc(text: str) -> str:
        return escape_for("teams", text)

    body: list[dict[str, Any]] = [
        {
            "type": "Container",
            "style": _TEAMS_STYLES[message.tone],
            "bleed": True,
            "items": [
                {"type": "TextBlock", "text": message.title, "weight": "Bolder", "wrap": True}
            ],
        },
        {
            "type": "TextBlock",
            "text": esc(message.headline),
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
    ]
    if message.quote:
        body.append(
            {"type": "TextBlock", "text": esc(message.quote), "isSubtle": True, "wrap": True}
        )
    if message.facts:
        body.append(
            {
                "type": "FactSet",
                "facts": [{"title": label, "value": esc(value)} for label, value in message.facts],
            }
        )
    body.append(
        {"type": "TextBlock", "text": FOOTER, "size": "Small", "isSubtle": True, "wrap": True}
    )
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if message.link_url and message.link_label:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": message.link_label, "url": message.link_url}
        ]
    # The envelope the Workflows template "Post to a channel when a webhook request is
    # received" expects.
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


def _discord(message: Message) -> dict[str, Any]:
    def esc(text: str) -> str:
        return escape_for("discord", text)

    lines = [f"**{esc(message.headline)}**"]
    if message.quote:
        lines.append(f"> {esc(message.quote)}")
    if message.link_url and message.link_label:
        lines.append(f"\n[{esc(message.link_label)} →]({message.link_url})")
    embed: dict[str, Any] = {
        "title": message.title,
        "description": "\n".join(lines),
        "color": int(_COLOURS[message.tone], 16),
        "footer": {"text": FOOTER},
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if message.facts:
        embed["fields"] = [
            {"name": label, "value": esc(value), "inline": True} for label, value in message.facts
        ]
    if message.link_url:
        embed["url"] = message.link_url
    # No mention of any kind is resolved, whatever the text holds.
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


_FORMATTERS = {"slack": _slack, "teams": _teams, "discord": _discord}


def payload_for(provider: str, message: Message) -> dict[str, Any]:
    return _FORMATTERS[provider](message)
