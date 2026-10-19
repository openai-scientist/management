"""What a chat message says, before a provider's formatter lays it out.

`headline`, `quote` and fact values are plain text and may hold what a user typed (a run
topic, a comment, a name). Formatters escape them with `escape_for`, so they render as
written and cannot mention a whole channel or hide a link. Titles and fact labels are the
Platform's own words. `link_url` is markup-free by construction: the Platform builds it from
its own app URL and record ids.

Every message has the same shape so a channel reads at a glance: a short title in the
event's colour, one bold line saying what or who, an optional quote, then label/value facts.
"""

import re
from dataclasses import dataclass
from typing import Literal

Tone = Literal["success", "danger", "warning", "info", "neutral"]

FOOTER = "AI Research Platform"

# Slack reads <...> as links and mentions (<!channel>, <url|text>) and & as an entity.
_SLACK = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
# Markdown characters, plus @ so that @everyone and @here stay plain text.
_DISCORD = re.compile(r"([\\*_~`|>\[\]()#@])")
_TEAMS = re.compile(r"([\\\[\]()*_#])")


@dataclass(frozen=True, slots=True)
class Message:
    title: str
    headline: str
    tone: Tone = "info"
    facts: tuple[tuple[str, str], ...] = ()
    quote: str | None = None
    link_label: str | None = None
    link_url: str | None = None


def escape_for(provider: str, text: str) -> str:
    if provider == "slack":
        return "".join(_SLACK.get(char, char) for char in text)
    if provider == "discord":
        return _DISCORD.sub(r"\\\1", text)
    return _TEAMS.sub(r"\\\1", text)


def sample_message(project_name: str | None, integration_name: str | None) -> Message:
    """The fixed text of a test send. A URL not yet saved learns nothing about the project."""
    if project_name is None or integration_name is None:
        return Message(
            "Webhook connected",
            "This channel can receive updates. Save the integration to turn them on.",
            "success",
        )
    return Message(
        "Integration connected",
        f"Updates from {project_name} will appear in this channel.",
        "success",
        (("Project", project_name), ("Integration", integration_name)),
    )
