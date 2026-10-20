"""What the Platform can post to, and about what.

The one source for the catalog endpoint, for checking a saved integration's events, and for
the plain-English text the web app shows next to a delivery error.
"""

from dataclasses import dataclass

MAX_INTEGRATIONS_PER_PROJECT = 10
# Run statuses that post `run.finished`. Running out of budget ends a run too.
INTEGRATION_FINISHED_STATUSES = frozenset({"completed", "failed", "stopped", "budget_exceeded"})


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    label: str
    url_placeholder: str
    setup_steps: tuple[str, ...]
    docs_url: str
    # Hosts a webhook URL may point at. A URL host matches one exactly or is a subdomain of it.
    hosts: tuple[str, ...]
    # The URL path must match this; None accepts any path.
    path_pattern: str | None
    # Slack and Discord carry the secret in the path and never use a query string.
    allows_query: bool


PROVIDERS: dict[str, Provider] = {
    provider.key: provider
    for provider in (
        Provider(
            key="slack",
            label="Slack",
            url_placeholder="https://hooks.slack.com/services/…",
            setup_steps=(
                "In Slack, create an app for your workspace at api.slack.com/apps.",
                "Turn on Incoming Webhooks and click Add New Webhook to Workspace.",
                "Pick the channel to post to, then copy the webhook URL.",
            ),
            docs_url="https://api.slack.com/messaging/webhooks",
            hosts=("hooks.slack.com",),
            path_pattern=r"/(services|triggers)/[A-Za-z0-9_\-/]+",
            allows_query=False,
        ),
        Provider(
            key="teams",
            label="Microsoft Teams",
            url_placeholder="https://….logic.azure.com/workflows/…",
            setup_steps=(
                "In the Teams channel, open the … menu and choose Workflows.",
                "Pick the template 'Post to a channel when a webhook request is received'.",
                "Finish the steps, then copy the URL the workflow gives you.",
            ),
            docs_url="https://support.microsoft.com/office/create-incoming-webhooks-with-workflows-for-microsoft-teams-8ae491c7-0394-4861-ba59-055e33f75498",
            hosts=("logic.azure.com", "powerplatform.com"),
            path_pattern=None,
            allows_query=True,
        ),
        Provider(
            key="discord",
            label="Discord",
            url_placeholder="https://discord.com/api/webhooks/…",
            setup_steps=(
                "Right-click a text channel and choose Edit Channel.",
                "Open Integrations, then Webhooks, and click New Webhook.",
                "Name the webhook, then click Copy Webhook URL.",
            ),
            docs_url="https://support.discord.com/hc/en-us/articles/228383668",
            hosts=("discord.com", "discordapp.com"),
            path_pattern=r"/api(/v\d+)?/webhooks/\d+/[A-Za-z0-9_\-]+",
            allows_query=False,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class Event:
    key: str
    label: str
    description: str


EVENTS: dict[str, Event] = {
    event.key: event
    for event in (
        Event(
            "run.awaiting_review",
            "Run awaiting review",
            "A run stopped and is waiting for someone to review it.",
        ),
        Event(
            "run.finished",
            "Run finished",
            "A run completed, failed, was stopped or ran out of budget.",
        ),
        Event(
            "run.commented",
            "New discussion on a run",
            "The first comment on a run, or on one of its results.",
        ),
        Event("member.joined", "Member joined", "Someone accepted an invitation to the project."),
    )
}

# What a stored delivery error means, worded for the person who has to fix it.
ERROR_MESSAGES: dict[str, str] = {
    "WEBHOOK_NOT_FOUND": (
        "The channel's webhook no longer exists. Create a new one and paste its URL here."
    ),
    "WEBHOOK_UNAUTHORIZED": (
        "The channel refused the message. Create a new webhook and paste its URL here."
    ),
    "PROVIDER_RATE_LIMITED": "The service asked us to slow down. Messages will resume shortly.",
    "PROVIDER_TIMEOUT": "The service did not answer in time. We will try again.",
    "PROVIDER_ERROR": "The service could not take the message. We will try again.",
    "PROVIDER_REJECTED": "The service did not accept the message format. Contact support.",
    "WEBHOOK_HOST_BLOCKED": "The webhook address is not reachable from the Platform.",
    "INTEGRATION_INACTIVE": "Not sent: the integration was paused or had stopped.",
    "INTEGRATION_URL_UNREADABLE": "The saved webhook URL can't be read. Paste the URL again.",
    "INTEGRATIONS_NOT_CONFIGURED": "Integrations are not configured on this server.",
    "EVENT_GONE": "Not sent: what the message was about has been deleted.",
    "CLAIM_LIMIT": "The message could not be sent. Contact support if this repeats.",
}
DEFAULT_ERROR_MESSAGE = "The last message could not be delivered."


def error_message(code: str) -> str:
    return ERROR_MESSAGES.get(code, DEFAULT_ERROR_MESSAGE)
