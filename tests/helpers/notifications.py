import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import AsyncClient

from tests.helpers.harness import ORIGIN, Harness
from tests.helpers.identity import login
from tests.helpers.projects import add_member
from tests.helpers.research import ready_project

NOTIFICATIONS = "/api/v1/notifications"


async def kinds(client: AsyncClient, **params: object) -> list[str]:
    response = await client.get(NOTIFICATIONS, params=params)
    assert response.status_code == 200, response.text
    return [item["kind"] for item in response.json()["data"]]


async def unread(client: AsyncClient) -> int:
    return (await client.get(f"{NOTIFICATIONS}/unread-count")).json()["data"]["unread_count"]


async def team(harness: Harness, manager_client, researcher_client, reviewer_client):
    """A ready project with a manager, a researcher and a reviewer signed in."""
    manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
    researcher = await login(
        harness, researcher_client, uid="researcher", email="researcher@example.com"
    )
    reviewer = await login(harness, reviewer_client, uid="reviewer", email="reviewer@example.com")
    project, version = await ready_project(manager_client, manager)
    members = {
        role: await add_member(manager_client, manager, project["id"], f"{role}@example.com", role)
        for role in ("researcher", "reviewer")
    }
    return manager, researcher, reviewer, project, version, members


STREAM = f"{NOTIFICATIONS}/stream"


class EventStream:
    def __init__(self, messages: asyncio.Queue) -> None:
        self.messages = messages
        self.buffer = b""

    async def frame(self) -> str:
        while b"\n\n" not in self.buffer:
            message = await asyncio.wait_for(self.messages.get(), timeout=2)
            assert message["type"] == "http.response.body"
            self.buffer += message.get("body", b"")
            if not message.get("more_body", False) and b"\n\n" not in self.buffer:
                raise AssertionError("Stream ended before another event")
        frame, self.buffer = self.buffer.split(b"\n\n", 1)
        return frame.decode()

    async def snapshot(self, event_name: str = "notifications") -> dict:
        frame = await self.frame()
        assert frame.startswith(f"event: {event_name}\n"), frame
        return json.loads(frame.split("data: ", 1)[1])


@asynccontextmanager
async def open_stream(
    harness: Harness, client: AsyncClient, path: str = STREAM
) -> AsyncIterator[tuple[dict, EventStream]]:
    """Exercise the real ASGI stream; HTTPX's in-process transport buffers forever."""
    request = client.build_request("GET", path, headers={"Origin": ORIGIN})
    messages = asyncio.Queue()
    disconnected = asyncio.Event()
    request_received = False

    async def receive() -> dict:
        nonlocal request_received
        if not request_received:
            request_received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        await messages.put(message)

    task = asyncio.create_task(
        harness.app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": request.url.path,
                "raw_path": request.url.path.encode(),
                "root_path": "",
                "query_string": request.url.query,
                "headers": [(name.lower(), value) for name, value in request.headers.raw],
                "client": ("127.0.0.1", 12345),
                "server": ("test", 80),
            },
            receive,
            send,
        )
    )
    try:
        started = await asyncio.wait_for(messages.get(), timeout=2)
        assert started["type"] == "http.response.start"
        yield started, EventStream(messages)
    finally:
        disconnected.set()
        await asyncio.wait_for(task, timeout=2)
