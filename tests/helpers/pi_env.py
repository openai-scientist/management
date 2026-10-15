"""Harness for the AI PI tests: a project, fake engines, a fake PI and result builders."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from management.research.contracts import Anchor, ConsultationResult, Finding, ReviewFindings
from management.research.models import PiConsultation, RunEvent
from tests.fakes.engines import engine_overrides, install_fake_engines, seed_models
from tests.fakes.pi import FakePI, pi_overrides
from tests.helpers.harness import open_harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, create_project

CHARTER = {
    "question": "Does retrieval method A beat B?",
    "objectives": ["Compare A and B"],
    "success_criteria": ["A wins by 3 points"],
}


def decided(request, decision: dict, **fields) -> ConsultationResult:
    return ConsultationResult.model_validate(
        {
            "consultation_id": request.consultation_id,
            "status": "decided",
            "decision": decision,
            "rationale": "Because the evidence says so.",
            "confidence": 0.8,
            "brief": "The PI decided.",
            **fields,
        }
    )


class Env(SimpleNamespace):
    def url(self, tail=""):
        return f"{PROJECTS}/{self.project['id']}/runs/{self.run['id']}{tail}"

    def csrf(self):
        return mutation_headers(self.session["csrf_token"])

    async def post(self, tail, **body):
        return await self.client.post(self.url(tail), json=body, headers=self.csrf())

    async def current(self):
        return (await self.client.get(self.url())).json()["data"]["current_stage"]

    async def rows(self, model, *where):
        async with self.harness.factory() as db:
            return list(await db.scalars(select(model).where(*where)))

    async def consultations(self):
        return await self.rows(PiConsultation, PiConsultation.research_run_id == self.run_id)

    async def consultation(self, request):
        async with self.harness.factory() as db:
            return await db.get(PiConsultation, UUID(request.consultation_id))

    async def events(self, event_type):
        return await self.rows(
            RunEvent, RunEvent.run_id == self.run_id, RunEvent.type == event_type
        )

    @property
    def run_id(self):
        return UUID(self.run["id"])

    async def endorse(self, request, **body):
        return await self.client.post(
            self.url(f"/pi/consultations/{request.consultation_id}/endorse"),
            json=body,
            headers=self.csrf(),
        )

    async def gate(self, stage, **body):
        """The researcher answers the open gate of `stage` (an item of `current_stage`)."""
        tail = f"/stages/{stage['stage_run_id']}/gates/{stage['open_gate']['gate_id']}"
        return await self.client.post(self.url(tail), json=body, headers=self.csrf())

    async def start_run(self, mode="light"):
        response = await self.client.post(
            f"{PROJECTS}/{self.project['id']}/runs",
            json={
                "topic": "Compare two retrieval methods",
                "domains": ["ml"],
                "interaction_mode": mode,
            },
            headers=self.csrf(),
        )
        assert response.status_code == 201, response.text
        self.run = response.json()["data"]

    async def settle_charter(self):
        request = self.pi.of_kind("charter")[0]
        response = await self.pi.respond(
            request, decided(request, {"action": "charter", "charter": CHARTER})
        )
        assert response.status_code == 200, response.text
        if request.authority == "advise":
            assert (await self.endorse(request)).status_code == 200
        return request


@asynccontextmanager
async def pi_env(tmp_path, mode="light", *, scripted=True, start=True, enabled=True, **settings):
    overrides = engine_overrides()
    if settings.pop("exd_free", False):
        overrides["pipelines"]["default"][1]["checkpoint"] = False
    async with open_harness(
        tmp_path, **overrides, **(pi_overrides() if enabled else {}), **settings
    ) as harness:
        fakes = install_fake_engines(harness)
        if scripted:
            for fake in fakes.values():
                fake.scripted()
        pi = FakePI(harness).install()
        async with harness.client() as client:
            session = await login(harness, client, uid="owner", email="owner@example.com")
            project = await create_project(client, session)
            await seed_models(harness, session["user"]["id"])
            env = Env(
                harness=harness,
                client=client,
                session=session,
                project=project,
                fakes=fakes,
                pi=pi,
                run=None,
            )
            if start:
                response = await client.post(
                    f"{PROJECTS}/{project['id']}/runs",
                    json={
                        "topic": "Compare two retrieval methods",
                        "domains": ["ml"],
                        "interaction_mode": mode,
                    },
                    headers=mutation_headers(session["csrf_token"]),
                )
                assert response.status_code == 201, response.text
                env.run = response.json()["data"]
            yield env


def critic_result(
    request, verdict="sound", points=(), obligations=(), summary="Fits the charter.", **fields
):
    """A `stage_review` result: the Critic report and the `report` decision."""
    return ConsultationResult.model_validate(
        {
            "consultation_id": request.consultation_id,
            "status": "decided",
            "decision": {"action": "report", "note": summary},
            "rationale": summary,
            "brief": "The Critic reviewed the stage.",
            "report": {
                "verdict": verdict,
                "summary": summary,
                "points": list(points),
                "obligations": list(obligations),
                "model": "openai_compatible/review",
            },
            **fields,
        }
    )


def serious_point(point_id="C1", **fields):
    return {
        "point_id": point_id,
        "severity": "serious",
        "lens": "charter",
        "ref": "charter:objectives[0]",
        "point": "The result does not address the objective",
    } | fields


async def critic_reply(env, **fields):
    """Answer the latest `stage_review` request; returns it."""
    request = env.pi.of_kind("stage_review")[-1]
    response = await env.pi.respond(request, critic_result(request, **fields))
    assert response.status_code == 200, response.text
    return request


async def next_outcome(env, **fields):
    """Let the Critic answer the finished stage; returns the `stage_outcome` request."""
    await critic_reply(env, **fields)
    return env.pi.of_kind("stage_outcome")[-1]


def blocking(locator, finding_id="F1-1", issue="No falsification criterion") -> ReviewFindings:
    return ReviewFindings(
        round=1,
        reviewed_artifact_id="x",
        reviewer_model="review",
        findings=[
            Finding(
                finding_id=finding_id,
                anchor=Anchor(locator=locator),
                severity="blocking",
                issue=issue,
                author="reviewer",
            )
        ],
        passed=False,
    )


async def blocked_outcome(env, findings):
    """The engine's review blocks twice with the same findings: the rule comments once, then
    approves with the findings still open. Returns the `stage_outcome` request."""
    await env.settle_charter()
    (engine_run_id,) = env.fakes["i2h"].states
    for _ in range(2):
        await env.fakes["i2h"].open_review_gate(engine_run_id, findings=findings)
    return await next_outcome(env)
