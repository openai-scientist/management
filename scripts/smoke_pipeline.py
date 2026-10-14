"""Exercise a configured Platform and both engines through the public API."""

import argparse
import asyncio
import os
import sys
from time import monotonic
from uuid import UUID

import httpx


class SmokeError(Exception):
    pass


def data(response):
    if response.is_error:
        # Do not print response bodies: login and engine errors may contain sensitive data.
        raise SmokeError(f"HTTP {response.status_code} from {response.request.url.path}")
    return response.json()["data"]


async def smoke(client, project, csrf_token, *, timeout=600, poll_interval=1, topic=None):
    base = f"/api/v1/projects/{project}/runs"
    headers = {"X-CSRF-Token": csrf_token, "Origin": str(client.base_url).rstrip("/")}
    run = data(
        await client.post(
            base,
            json={
                "topic": topic or "Compare sparse and dense retrieval on a public ML benchmark",
                "domains": ["ml"],
                "interaction_mode": "light",
            },
            headers=headers,
        )
    )
    run_url = f"{base}/{run['id']}"

    async def wait(label, predicate):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            current = data(await client.get(run_url))
            if current["status"] in {"failed", "stopped", "budget_exceeded"}:
                raise SmokeError(f"Run {current['id']} ended with {current['status']}")
            if predicate(current):
                return current
            await asyncio.sleep(poll_interval)
        raise SmokeError(f"Timed out waiting for {label}; run {run['id']}")

    for key in ("hypothesis", "experiment_plan"):

        def review_ready(current):
            stage = current.get("current_stage") or {}
            gate = stage.get("open_gate") or {}
            return stage.get("stage_key") == key and gate.get("kind") == "artifact_review"

        current = await wait(f"{key} review gate", review_ready)
        stage = current["current_stage"]
        stage_url = f"{run_url}/stages/{stage['stage_run_id']}"
        data(
            await client.post(
                f"{stage_url}/gates/{stage['open_gate']['gate_id']}",
                json={"action": "approve"},
                headers=headers,
            )
        )
        await wait(
            f"{key} COMPLETED",
            lambda item: (
                item["current_stage"]["stage_run_id"] == stage["stage_run_id"]
                and item["current_stage"]["status"] == "COMPLETED"
            ),
        )
        selection = {}
        if key == "hypothesis":
            artifacts = data(
                await client.get(
                    f"{run_url}/artifacts",
                    params={"stage_run_id": stage["stage_run_id"], "schema": "i2h.hypotheses/v1"},
                )
            )
            published = [item for item in artifacts if item["primary"]]
            if not published:
                raise SmokeError("No published hypothesis artifact")
            artifact = max(published, key=lambda item: item["version"])
            response = await client.get(f"{run_url}/artifacts/{artifact['id']}/download")
            if response.is_error:
                raise SmokeError("Cannot download the hypothesis artifact")
            ids = [item["id"] for item in response.json()["hypotheses"][:2]]
            if len(ids) != 2:
                raise SmokeError("Smoke requires at least two hypotheses")
            selection = {"hypothesis_ids": ids}
        data(
            await client.post(f"{stage_url}/accept", json={"selection": selection}, headers=headers)
        )
    return await wait("completed run", lambda item: item["status"] == "completed")


async def main(args):
    email, password = os.environ.get("SMOKE_EMAIL"), os.environ.get("SMOKE_PASSWORD")
    if not email or not password:
        raise SmokeError("Set SMOKE_EMAIL and SMOKE_PASSWORD in the process environment")
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=30) as client:
        session = data(
            await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        )
        result = await smoke(
            client,
            args.project,
            session["csrf_token"],
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            topic=args.topic,
        )
        print(f"run {result['id']}: {result['status']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--timeout", type=float, default=600, help="Seconds per wait step")
    parser.add_argument("--poll-interval", type=float, default=1)
    parser.add_argument("--topic")
    args = parser.parse_args()
    if args.timeout <= 0 or args.poll_interval < 0:
        parser.error("timeout must be positive; poll-interval must be nonnegative")
    try:
        asyncio.run(main(args))
    except (SmokeError, httpx.HTTPError, ValueError, KeyError, TypeError):
        # Keep errors useful without dumping credentials, body content or transport internals.
        print(
            "Smoke failed; check service readiness, run state and configuration.", file=sys.stderr
        )
        sys.exit(1)
