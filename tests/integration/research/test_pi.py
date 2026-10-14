"""The AI PI in the Platform: charter, routing by authority, applying, overriding."""

from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from management.research.contracts import (
    Anchor,
    ConsultationResult,
    Finding,
    ReviewFindings,
    TokenUsage,
)
from management.research.models import (
    PiConsultation,
    PiNote,
    ResearchRun,
    RunEvent,
    StageDecision,
    StageRun,
)
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


async def approve_gate(env):
    request = env.pi.of_kind("gate_review")[-1]
    response = await env.pi.respond(request, decided(request, {"action": "approve"}))
    assert response.status_code == 200, response.text
    return request


async def test_charter_auto_starts_first_stage(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        request = env.pi.of_kind("charter")[0]
        assert request.authority == "decide" and request.charter is None
        assert env.fakes["i2h"].requests == []
        assert env.run["current_stage"] is None
        await env.settle_charter()
        sent = env.fakes["i2h"].requests[0]
        assert sent.interaction_mode == "copilot"
        assert sent.inputs.params["guidance"][0] == f"Research question: {CHARTER['question']}"
        assert "Success criterion: A wins by 3 points" in sent.inputs.params["guidance"]
        async with env.harness.factory() as db:
            run = await db.get(ResearchRun, env.run_id)
        assert run.charter_status == "settled"
        assert run.charter["author"] == "pi" and run.charter["version"] == 1
        assert run.charter["post_hoc"] is False
        assert env.pi.of_kind("gate_review")[0].charter.question == CHARTER["question"]


async def test_charter_light_waits_for_researcher_then_endorse(tmp_path):
    async with pi_env(tmp_path, "light") as env:
        request = env.pi.of_kind("charter")[0]
        assert request.authority == "advise"
        assert len(await env.events("attention.required")) == 1
        response = await env.pi.respond(
            request, decided(request, {"action": "charter", "charter": CHARTER})
        )
        assert response.status_code == 200
        (row,) = await env.consultations()
        assert row.status == "awaiting_researcher"
        assert env.fakes["i2h"].requests == []
        assert len(await env.events("pi.recommendation")) == 1
        assert (await env.endorse(request)).status_code == 200
        (row,) = [c for c in await env.consultations() if c.kind == "charter"]
        assert row.status == "endorsed"
        assert len(env.fakes["i2h"].requests) == 1
        state = (await env.client.get(env.url("/pi"))).json()["data"]
        assert state["charter_status"] == "settled" and state["charter"]["version"] == 1


async def test_researcher_writes_charter_instead(tmp_path):
    async with pi_env(tmp_path, "copilot") as env:
        request = env.pi.of_kind("charter")[0]
        response = await env.client.put(
            env.url("/pi/charter"),
            json={"charter": CHARTER, "reason": "My own question"},
            headers=env.csrf(),
        )
        assert response.status_code == 200, response.text
        (row,) = [c for c in await env.consultations() if c.kind == "charter"]
        assert row.status == "overridden"
        assert len(env.fakes["i2h"].requests) == 1
        # A late answer from the PI changes nothing.
        late = await env.pi.respond(
            request,
            decided(request, {"action": "charter", "charter": CHARTER | {"question": "Other"}}),
        )
        assert late.status_code == 200
        async with env.harness.factory() as db:
            assert (await db.get(ResearchRun, env.run_id)).charter["question"] == CHARTER[
                "question"
            ]


async def test_pi_answers_question_gate_in_auto(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        gate = await fake.open_question_gate(
            engine_run_id, step_key="topic", options=["narrow", "broad"], default="narrow"
        )
        request = env.pi.of_kind("gate_question")[0]
        assert request.authority == "decide"
        assert request.allowed[0].option_ids == ["narrow", "broad"]
        response = await env.pi.respond(
            request, decided(request, {"action": "answer", "option_id": "broad", "note": "Wider"})
        )
        assert response.status_code == 200, response.text
        answer = fake._answers[(engine_run_id, gate.gate_id)]
        assert (answer.action, answer.option_id) == ("answer", "broad")
        assert answer.actor == "system" and answer.decided_by == f"pi:{request.consultation_id}"
        (decision,) = await env.rows(StageDecision, StageDecision.gate_id == gate.gate_id)
        assert decision.actor == "pi" and decision.decided_by_user_id is None
        assert decision.payload["consultation_id"] == request.consultation_id
        assert (await env.consultation(request)).status == "applied"


async def test_light_gate_review_by_pi(tmp_path):
    async with pi_env(tmp_path, "light") as env:
        await env.settle_charter()
        request = env.pi.of_kind("gate_review")[0]
        assert request.authority == "decide"
        await approve_gate(env)
        stage = await env.current()
        assert stage["status"] == "COMPLETED"
        decisions = await env.rows(StageDecision, StageDecision.action == "approve")
        assert [d.actor for d in decisions] == ["pi"]
        # The hypothesis stage is a checkpoint: the researcher decides, the PI advises.
        outcome = env.pi.of_kind("stage_outcome")[0]
        assert outcome.authority == "advise"


async def test_light_non_checkpoint_stage_outcome_is_the_pis(tmp_path):
    async with pi_env(tmp_path, "light", exd_free=True) as env:
        await env.settle_charter()
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        response = await env.pi.respond(
            outcome, decided(outcome, {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}})
        )
        assert response.status_code == 200
        assert (await env.endorse(outcome)).status_code == 200
        await approve_gate(env)
        second = env.pi.of_kind("stage_outcome")[1]
        assert second.situation.stage_key == "experiment_plan"
        assert second.authority == "decide"


async def test_copilot_gate_goes_to_researcher_with_recommendation(tmp_path):
    async with pi_env(tmp_path, "copilot") as env:
        await env.settle_charter()
        request = env.pi.of_kind("gate_review")[0]
        assert request.authority == "advise"
        attention = len(await env.events("attention.required"))
        await env.pi.respond(request, decided(request, {"action": "approve"}))
        stage = await env.current()
        assert stage["status"] == "RUNNING" and stage["open_gate"] is not None
        assert (await env.consultation(request)).status == "awaiting_researcher"
        assert len(await env.events("pi.recommendation")) >= 1
        assert (await env.endorse(request)).status_code == 200
        assert (await env.current())["status"] == "COMPLETED"
        (decision,) = await env.rows(StageDecision, StageDecision.action == "approve")
        assert decision.actor == "user" and decision.decided_by_user_id is not None
        assert decision.payload["consultation_id"] == request.consultation_id
        assert (await env.consultation(request)).status == "endorsed"
        assert attention >= 1


async def test_pi_cannot_approve_blocking(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        await fake.open_review_gate(
            engine_run_id,
            findings=ReviewFindings(
                round=1,
                reviewed_artifact_id="x",
                reviewer_model="review",
                findings=[
                    Finding(
                        finding_id="F1-1",
                        anchor=Anchor(locator="hypothesis:H1"),
                        severity="blocking",
                        issue="No falsification criterion",
                        author="reviewer",
                    )
                ],
                passed=False,
            ),
        )
        request = env.pi.of_kind("gate_review")[0]
        await env.pi.respond(request, decided(request, {"action": "approve"}))
        row = await env.consultation(request)
        assert row.status == "awaiting_researcher"
        assert row.result["escalate"]["reason_code"] == "integrity"
        assert (await env.current())["open_gate"] is not None
        assert await env.rows(StageDecision, StageDecision.action == "approve") == []


async def test_invalid_pi_action_goes_to_researcher(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        await fake.open_question_gate(
            engine_run_id, step_key="topic", options=["narrow", "broad"], default="narrow"
        )
        request = env.pi.of_kind("gate_question")[0]
        await env.pi.respond(request, decided(request, {"action": "answer", "option_id": "huge"}))
        row = await env.consultation(request)
        assert row.status == "awaiting_researcher"
        assert row.result["escalate"]["reason_code"] == "cannot_decide"


async def test_stage_outcome_accept_by_pi_passes_handoff_note(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        assert outcome.authority == "decide"
        accept = next(a for a in outcome.allowed if a.action == "accept")
        assert accept.selection_key == "hypothesis_ids"
        assert any(a.role == "primary" for a in outcome.artifacts)
        response = await env.pi.respond(
            outcome,
            decided(
                outcome,
                {
                    "action": "accept",
                    "selection": {"hypothesis_ids": ["H1"]},
                    "note": "Focus on H1",
                },
                handoff=["Check the baselines first"],
            ),
        )
        assert response.status_code == 200, response.text
        sent = env.fakes["exd"].requests[0]
        assert sent.inputs.decisions.note == "Focus on H1"
        assert sent.inputs.decisions.actor == "system"
        assert sent.inputs.decisions.decided_by == f"pi:{outcome.consultation_id}"
        assert "Handoff: Check the baselines first" in sent.inputs.params["guidance"]
        (decision,) = await env.rows(StageDecision, StageDecision.action == "accept")
        assert (
            decision.actor == "pi"
            and decision.payload["consultation_id"] == outcome.consultation_id
        )
        assert (await env.consultation(outcome)).applied_decision_id == decision.id


async def test_stage_outcome_send_back_by_pi(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        send_back = next(a for a in outcome.allowed if a.action == "send_back")
        assert send_back.targets == ["hypothesis"]
        response = await env.pi.respond(
            outcome,
            decided(
                outcome,
                {"action": "send_back", "target_stage_key": "hypothesis", "note": "Too broad"},
                handoff=["Narrow to one domain"],
            ),
        )
        assert response.status_code == 200, response.text
        second = env.fakes["i2h"].requests[1]
        assert second.iteration == 2
        assert second.inputs.feedback[0].note == "Too broad\nNarrow to one domain"
        assert second.inputs.params["guidance"].count("Handoff: Narrow to one domain") == 1
        (decision,) = await env.rows(StageDecision, StageDecision.action == "send_back")
        assert decision.actor == "pi" and decision.reason == "Because the evidence says so."


async def test_guidance_is_replaced_on_every_stage_start(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        await approve_gate(env)
        first = env.pi.of_kind("stage_outcome")[0]
        await env.pi.respond(
            first,
            decided(
                first,
                {"action": "send_back", "target_stage_key": "hypothesis"},
                handoff=["First lesson"],
            ),
        )
        await approve_gate(env)
        second = env.pi.of_kind("stage_outcome")[1]
        await env.pi.respond(
            second,
            decided(
                second,
                {"action": "send_back", "target_stage_key": "hypothesis"},
                handoff=["Second lesson"],
            ),
        )
        guidance = env.fakes["i2h"].requests[2].inputs.params["guidance"]
        assert "Handoff: Second lesson" in guidance
        assert "Handoff: First lesson" not in guidance


async def test_stale_result_ignored(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        request = env.pi.of_kind("gate_review")[0]
        # The engine closed the gate by itself while the PI was thinking.
        async with env.harness.factory() as db:
            stage = (await db.scalars(select(StageRun))).one()
            stage.open_gate = None
            await db.commit()
        response = await env.pi.respond(request, decided(request, {"action": "approve"}))
        assert response.status_code == 200
        row = await env.consultation(request)
        assert row.status == "stale" and row.result is not None
        assert await env.rows(StageDecision) == []
        retry = await env.client.post(
            env.url(f"/pi/consultations/{request.consultation_id}/retry"), headers=env.csrf()
        )
        assert retry.status_code == 409


async def test_escalation_stops_in_auto_only_for_hard_reasons(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        gate = env.pi.of_kind("gate_review")[0]
        await env.pi.respond(
            gate,
            decided(
                gate,
                {"action": "approve"},
                escalate={"reason_code": "low_confidence", "message": "Unsure"},
            ),
        )
        assert (await env.consultation(gate)).status == "applied"
        notes = await env.rows(PiNote, PiNote.kind == "observation")
        assert "low_confidence" in notes[0].text
        outcome = env.pi.of_kind("stage_outcome")[0]
        await env.pi.respond(
            outcome,
            decided(
                outcome,
                {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}},
                escalate={"reason_code": "charter_conflict", "message": "Conflicts"},
            ),
        )
        assert (await env.consultation(outcome)).status == "awaiting_researcher"
        assert (await env.current())["status"] == "COMPLETED"
        assert len(await env.events("attention.required")) >= 1


async def test_escalation_always_stops_outside_auto(tmp_path):
    async with pi_env(tmp_path, "light") as env:
        await env.settle_charter()
        gate = env.pi.of_kind("gate_review")[0]
        await env.pi.respond(
            gate,
            decided(
                gate,
                {"action": "approve"},
                escalate={"reason_code": "low_confidence", "message": "Unsure"},
            ),
        )
        assert (await env.consultation(gate)).status == "awaiting_researcher"
        assert (await env.current())["open_gate"] is not None


async def test_pi_unavailable_escalates(tmp_path):
    async with pi_env(tmp_path, "light", start=False) as env:
        env.pi.unreachable = True
        response = await env.client.post(
            f"{PROJECTS}/{env.project['id']}/runs",
            json={
                "topic": "Compare two retrieval methods",
                "domains": ["ml"],
                "interaction_mode": "light",
            },
            headers=env.csrf(),
        )
        assert response.status_code == 201, response.text
        env.run = response.json()["data"]
        (row,) = await env.consultations()
        assert row.status == "failed" and row.failure["code"] == "pi_unavailable"
        reasons = [e.payload.get("reason") for e in await env.events("attention.required")]
        assert "pi_failed" in reasons
        env.pi.unreachable = False
        retry = await env.client.post(
            env.url(f"/pi/consultations/{row.id}/retry"), headers=env.csrf()
        )
        assert retry.status_code == 200, retry.text
        new_id = retry.json()["data"]["consultation_id"]
        assert new_id != str(row.id)
        assert env.pi.last.consultation_id == new_id
        rows = {str(c.id): c.status for c in await env.consultations()}
        assert rows[str(row.id)] == "failed" and rows[new_id] == "sent"


async def test_override_recorded_in_notebook(tmp_path):
    async with pi_env(tmp_path, "copilot", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        gate = await fake.open_question_gate(
            engine_run_id, step_key="topic", options=["narrow", "broad"], default="narrow"
        )
        request = env.pi.of_kind("gate_question")[0]
        await env.pi.respond(request, decided(request, {"action": "answer", "option_id": "broad"}))
        stage = await env.current()
        response = await env.client.post(
            env.url(f"/stages/{stage['stage_run_id']}/gates/{gate.gate_id}"),
            json={"action": "answer", "option_id": "narrow", "reason": "Budget is tight"},
            headers=env.csrf(),
        )
        assert response.status_code == 200, response.text
        assert (await env.consultation(request)).status == "overridden"
        (note,) = await env.rows(PiNote, PiNote.kind == "override")
        assert "Budget is tight" in note.text and "answer" in note.text and note.actor == "user"
        notebook = (await env.client.get(env.url("/pi/notebook"))).json()["data"]
        assert any(n["kind"] == "override" for n in notebook)
        # The next question carries the override.
        await fake.open_question_gate(
            engine_run_id, step_key="scope", options=["a", "b"], default="a"
        )
        assert any(n.kind == "override" for n in env.pi.last.notebook)


async def test_directive_in_next_request(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        response = await env.post("/pi/directives", text="Prefer small models")
        assert response.status_code == 201, response.text
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        assert any(
            n.kind == "directive" and n.actor == "user" and n.text == "Prefer small models"
            for n in outcome.notebook
        )
        await env.pi.respond(
            outcome, decided(outcome, {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}})
        )
        assert (
            "Directive: Prefer small models"
            in env.fakes["exd"].requests[0].inputs.params["guidance"]
        )


async def test_result_idempotent(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        result = decided(
            outcome,
            {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}},
            usage={"lead": TokenUsage(input_tokens=1_000_000, output_tokens=0)},
        )
        first = await env.pi.respond(outcome, result)
        second = await env.pi.respond(outcome, result)
        assert first.status_code == second.status_code == 200
        assert second.json()["processed"] is False
        assert len(await env.rows(StageDecision, StageDecision.action == "accept")) == 1
        assert len(env.fakes["exd"].requests) == 1
        assert (await env.consultation(outcome)).cost_usd == Decimal("1")


async def test_result_needs_pi_service_key(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        request = env.pi.of_kind("charter")[0]
        response = await env.pi.respond(
            request, decided(request, {"action": "charter", "charter": CHARTER}), key="wrong"
        )
        assert response.status_code == 401
        assert response.json()["code"] == "SERVICE_KEY_INVALID"


async def test_pi_disabled_keeps_old_behavior(tmp_path):
    async with pi_env(tmp_path, "copilot", enabled=False) as env:
        assert env.run["current_stage"]["stage_key"] == "hypothesis"
        sent = env.fakes["i2h"].requests[0]
        assert sent.interaction_mode == "copilot"
        assert "guidance" not in sent.inputs.params
        assert env.pi.requests == [] and await env.consultations() == []
        state = (await env.client.get(env.url("/pi"))).json()["data"]
        assert state["enabled"] is False and state["charter_status"] == "none"
        refused = await env.post("/pi/directives", text="hello")
        assert refused.status_code == 409
        assert len(await env.events("attention.required")) >= 1


async def test_secret_not_stored_in_consultation_request(tmp_path):
    async with pi_env(tmp_path, "light") as env:
        request = env.pi.of_kind("charter")[0]
        assert request.models.lead.api_key.get_secret_value() == "model-test-secret"
        assert env.pi.wire[0]["models"]["critic"]["api_key"] == "model-test-secret"
        (row,) = await env.consultations()
        assert "model-test-secret" not in str(row.request)
        assert row.request["models"]["lead"]["api_key"] is None
        listing = await env.client.get(env.url("/pi/consultations"))
        assert "model-test-secret" not in listing.text


async def test_pi_cost_added_to_run(tmp_path):
    async with pi_env(tmp_path, "light") as env:
        request = env.pi.of_kind("charter")[0]
        await env.pi.respond(
            request,
            decided(
                request,
                {"action": "charter", "charter": CHARTER},
                usage={
                    "lead": TokenUsage(input_tokens=1_000_000, output_tokens=500_000),
                    "critic": TokenUsage(input_tokens=2_000_000, output_tokens=0),
                },
            ),
        )
        # Review model: $1 in, $2 out per million. Stage model has the same prices here.
        (row,) = await env.consultations()
        assert row.cost_usd == Decimal("4")
        async with env.harness.factory() as db:
            assert (await db.get(ResearchRun, env.run_id)).cost_usd == Decimal("4")


async def test_consultation_list_exposes_trace_and_one_decision_event(tmp_path):
    async with pi_env(tmp_path, "light") as env:
        request = env.pi.of_kind("charter")[0]
        await env.pi.respond(
            request,
            decided(
                request,
                {"action": "charter", "charter": CHARTER},
                trace=[
                    {"actor": "pi", "tool": "read_artifact", "summary": "Read H1"},
                    {"actor": "critic", "tool": "search_literature", "summary": "Checked novelty"},
                ],
            ),
        )
        (item,) = (await env.client.get(env.url("/pi/consultations"))).json()["data"]
        assert [step["tool"] for step in item["trace"]] == ["read_artifact", "search_literature"]
        assert "trace" not in item["result"]
        (event,) = await env.events("pi.decision")
        assert event.actor == "pi" and event.event_class == "activity"
        assert event.summary == "The PI decided."
        assert len(await env.events("pi.decision")) == 1


async def test_failure_is_always_advice_and_endorse_reruns(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        await fake.fail(engine_run_id, "llm_error", "Model call failed")
        request = env.pi.of_kind("failure")[0]
        assert request.authority == "advise"
        assert request.situation.failure.code == "llm_error"
        assert [a.action for a in request.allowed] == ["rerun", "stop"]
        await env.pi.respond(request, decided(request, {"action": "rerun"}))
        assert (await env.consultation(request)).status == "awaiting_researcher"
        assert len(fake.requests) == 1
        assert (await env.endorse(request)).status_code == 200
        assert len(fake.requests) == 2
        (decision,) = await env.rows(StageDecision, StageDecision.action == "rerun")
        assert (
            decision.actor == "user"
            and decision.payload["consultation_id"] == request.consultation_id
        )


async def test_close_report_is_only_a_note(tmp_path):
    async with pi_env(tmp_path, "auto", exd_free=True) as env:
        await env.settle_charter()
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        await env.pi.respond(
            outcome, decided(outcome, {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}})
        )
        await approve_gate(env)
        last = env.pi.of_kind("stage_outcome")[1]
        await env.pi.respond(last, decided(last, {"action": "accept"}))
        close = env.pi.of_kind("close")[0]
        assert [a.action for a in close.allowed] == ["report"]
        await env.pi.respond(
            close, decided(close, {"action": "report"}, brief="Run finished well.")
        )
        row = await env.consultation(close)
        assert row.status == "applied" and row.applied_decision_id is None
        briefs = await env.rows(PiNote, PiNote.kind == "brief")
        assert "Run finished well." in [n.text for n in briefs]
        assert await env.rows(PiNote, PiNote.kind == "decision", PiNote.text.like("report%")) == []


async def test_artifact_catalogue_lists_history_and_inputs(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        await approve_gate(env)
        outcome = env.pi.of_kind("stage_outcome")[0]
        await env.pi.respond(
            outcome, decided(outcome, {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}})
        )
        await approve_gate(env)
        second = env.pi.of_kind("stage_outcome")[1]
        roles = {(a.stage_key, a.role) for a in second.artifacts}
        assert ("experiment_plan", "primary") in roles
        assert ("hypothesis", "input") in roles
        assert all(a.size_bytes is not None for a in second.artifacts)
        await env.pi.respond(
            second,
            decided(second, {"action": "send_back", "target_stage_key": "hypothesis"}),
        )
        await approve_gate(env)
        third = env.pi.of_kind("stage_outcome")[2]
        assert ("hypothesis", "history") in {(a.stage_key, a.role) for a in third.artifacts}


@pytest.mark.parametrize("path", ["/pi", "/pi/notebook", "/pi/consultations"])
async def test_pi_reads_need_project_access(tmp_path, path):
    async with pi_env(tmp_path, "light") as env:
        async with env.harness.client() as other:
            await login(env.harness, other, uid="stranger", email="stranger@example.com")
            assert (await other.get(env.url(path))).status_code in {403, 404}
