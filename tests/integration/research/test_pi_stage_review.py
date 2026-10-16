"""The v3 flow: review-gate rule, Critic at the stage end, overrides and obligations."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from management.research.contracts import ConsultationResult, FindingKind, GateAnswer
from management.research.engines.client import EngineUnavailable
from management.research.models import (
    ModelProfile,
    PiConsultation,
    PiNote,
    ResearchRun,
    StageDecision,
    StageRun,
)
from management.research.pi import context, review_gate, service
from management.research.pi.service import STAGE_REVIEW_TIMEOUT, flush_consultations, sweep
from tests.helpers.pi_env import (
    blocked_outcome,
    blocking,
    critic_reply,
    critic_result,
    decided,
    next_outcome,
    pi_env,
    serious_point,
)
from tests.helpers.projects import PROJECTS

ACCEPT_H1 = {"action": "accept", "selection": {"hypothesis_ids": ["H1"]}}
RISK_LINE = "Known risk on H1: F1-1, no falsification criterion yet"


def blocking_ids(*ids):
    findings = blocking("hypothesis:H1", ids[0])
    findings.findings = [findings.findings[0].model_copy(update={"finding_id": fid}) for fid in ids]
    return findings


async def stage_snapshot(env):
    return (await env.rows(StageRun))[0].config_snapshot


# --- the review-gate rule -------------------------------------------------------------------


async def test_blocking_findings_are_sent_back_once_then_the_gate_is_approved(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        findings = blocking_ids("F1-1", "F1-2")
        await fake.open_review_gate(engine_run_id, findings=findings)
        (comment,) = fake._answers.values()
        assert comment.action == "comment"
        assert [f.finding_id for f in comment.findings] == ["F1-1", "F1-2"]
        assert all(f.author == "researcher" for f in comment.findings)
        assert comment.findings[0].issue == "[F1-1] No falsification criterion"
        assert comment.actor == "system" and comment.decided_by == "policy:review_gate"
        assert (await env.current())["status"] == "RUNNING"
        # The same findings come back: the engine cannot fix them, so the gate is approved.
        await fake.open_review_gate(engine_run_id, findings=findings)
        assert (await env.current())["status"] == "COMPLETED"
        assert (await stage_snapshot(env))["open_blocking"] == ["F1-1", "F1-2"]
        events = sorted(
            (e for e in await env.events("decision.recorded") if e.payload.get("policy")),
            key=lambda e: e.seq,
        )
        assert [(e.payload["action"], e.payload["finding_ids"]) for e in events] == [
            ("comment", ["F1-1", "F1-2"]),
            ("approve", ["F1-1", "F1-2"]),
        ]
        assert env.pi.of_kind("gate_review") == []


async def test_the_rule_comments_at_most_once_per_stage_run(tmp_path):
    async with pi_env(tmp_path, "light", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        for finding_id in ("F1-1", "F2-1"):
            await fake.open_review_gate(engine_run_id, findings=blocking_ids(finding_id))
        actions = [answer.action for answer in fake._answers.values()]
        assert actions == ["comment", "approve"]
        assert (await stage_snapshot(env))["open_blocking"] == ["F2-1"]
        assert (await env.current())["status"] == "COMPLETED"


async def test_blocking_findings_that_need_evidence_are_approved_and_stay_in_the_ledger(tmp_path):
    async with pi_env(tmp_path, "light", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        findings = blocking_ids("F1-1")
        findings.findings = [
            findings.findings[0].model_copy(update={"kind": FindingKind.needs_evidence})
        ]
        await fake.open_review_gate(engine_run_id, findings=findings)
        assert [answer.action for answer in fake._answers.values()] == ["approve"]
        assert (await stage_snapshot(env))["open_blocking"] == ["F1-1"]


async def test_a_review_after_the_first_round_is_approved_at_once(tmp_path):
    async with pi_env(tmp_path, "light", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        findings = blocking_ids("F1-1").model_copy(update={"round": 2})
        await fake.open_review_gate(engine_run_id, findings=findings)
        assert [answer.action for answer in fake._answers.values()] == ["approve"]
        assert (await stage_snapshot(env))["open_blocking"] == ["F1-1"]


async def test_a_review_with_no_blocking_finding_is_approved_and_leaves_no_open_blocking(
    tmp_path,
):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        (answer,) = env.fakes["i2h"]._answers.values()
        assert answer.action == "approve"
        assert "open_blocking" not in await stage_snapshot(env)


async def test_copilot_runs_have_no_rule(tmp_path):
    async with pi_env(tmp_path, "copilot", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        await fake.open_review_gate(engine_run_id, findings=blocking_ids("F1-1"))
        assert fake._answers == {}
        assert (await env.current())["open_gate"] is not None


async def run_rule(env):
    await review_gate.answer_review_gates(env.harness.app.state, env.run_id)


async def test_the_counters_change_only_after_the_engine_accepted_the_answer(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        engine = env.harness.app.state.engines.get("i2h")
        real = engine.answer_gate

        async def down(*args, **kwargs):
            raise EngineUnavailable

        engine.answer_gate = down
        await fake.open_review_gate(engine_run_id, findings=blocking_ids("F1-1"))
        snapshot = await stage_snapshot(env)
        assert "review_gate" not in snapshot and "open_blocking" not in snapshot
        assert snapshot["review_gate_failed"]
        reasons = [e.payload.get("reason") for e in await env.events("attention.required")]
        assert "review_gate_failed" in reasons
        engine.answer_gate = real


async def test_a_stale_pending_answer_does_not_block_a_retry(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        engine = env.harness.app.state.engines.get("i2h")
        real = engine.answer_gate

        async def down(*args, **kwargs):
            raise EngineUnavailable

        engine.answer_gate = down
        gate = await fake.open_review_gate(engine_run_id)
        engine.answer_gate = real
        assert (await env.current())["status"] == "RUNNING"
        # A crash left a pending answer behind and no failure mark.
        async with env.harness.factory() as db:
            stage = (await db.scalars(select(StageRun))).one()
            stage.config_snapshot = {
                k: v for k, v in stage.config_snapshot.items() if k != "review_gate_failed"
            }
            db.add(
                StageDecision(
                    stage_run_id=stage.id,
                    gate_id=gate.gate_id,
                    action="approve",
                    payload=GateAnswer(action="approve", actor="system").model_dump(
                        mode="json", exclude={"actor", "decided_by"}
                    ),
                    actor="system",
                    status="pending",
                    decided_at=datetime.now(UTC) - timedelta(minutes=1),
                )
            )
            await db.commit()
        await run_rule(env)
        assert (await env.current())["status"] == "RUNNING"  # a fresh pending answer blocks
        async with env.harness.factory() as db:
            decision = (await db.scalars(select(StageDecision))).one()
            decision.decided_at = datetime.now(UTC) - timedelta(minutes=11)
            await db.commit()
        await run_rule(env)
        assert (await env.current())["status"] == "COMPLETED"


async def test_a_crash_in_the_background_rule_hands_the_gate_to_the_researcher(
    tmp_path, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        monkeypatch.setattr(review_gate, "answer_review_gates", boom)
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        gate = await fake.open_review_gate(engine_run_id)
        assert (await stage_snapshot(env))["review_gate_failed"] == gate.gate_id
        (event,) = [
            e
            for e in await env.events("attention.required")
            if e.payload.get("reason") == "review_gate_failed"
        ]
        assert event.payload["gate_id"] == gate.gate_id


# --- the Critic and the stage outcome -----------------------------------------------------


async def test_a_completed_stage_goes_to_the_critic_then_the_pi_sees_its_report(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        assert [r.kind for r in env.pi.requests][-1] == "stage_review"
        assert env.pi.of_kind("stage_outcome") == []
        point = {
            "point_id": "C1",
            "severity": "minor",
            "lens": "integrity",
            "ref": "integrity:spin",
            "point": "The summary reads stronger than the result",
        }
        review = await critic_reply(env, verdict="concerns", points=[point], summary="One concern.")
        (outcome,) = env.pi.of_kind("stage_outcome")
        assert outcome.critique.verdict == "concerns"
        assert outcome.critique.points[0].point_id == "C1"
        assert outcome.critique.model == "openai_compatible/review"
        row = await env.consultation(review)
        assert row.status == "applied" and row.result["report"]["verdict"] == "concerns"
        (note,) = await env.rows(PiNote, PiNote.kind == "critique")
        assert note.text == "Critic concerns: One concern." and note.actor == "pi"
        assert note.stage_key == "hypothesis"
        assert str(note.consultation_id) == review.consultation_id


async def test_the_researcher_is_told_at_once_when_the_stage_outcome_is_theirs(tmp_path):
    async with pi_env(tmp_path, "copilot") as env:
        await env.settle_charter()
        assert (await env.gate(await env.current(), action="approve")).status_code == 200
        (event,) = [
            e
            for e in await env.events("attention.required")
            if e.payload.get("reason") == "stage_completed"
        ]
        assert event.payload["kind"] == "stage_outcome"
        assert "Critic report will follow" in event.payload["text"]
        assert env.pi.of_kind("stage_outcome") == []


async def test_no_early_attention_when_the_pi_decides_the_outcome(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        reasons = [e.payload.get("reason") for e in await env.events("attention.required")]
        assert "stage_completed" not in reasons


async def test_a_critic_that_does_not_answer_in_time_is_unavailable(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[-1]
        state = env.harness.app.state
        async with env.harness.factory() as db:
            row = await db.get(PiConsultation, UUID(review.consultation_id))
            assert row.status == "sent"
            await flush_consultations(db, state, env.run_id)
            await db.refresh(row)
            assert row.status == "sent"  # not yet late
            row.created_at = datetime.now(UTC) - STAGE_REVIEW_TIMEOUT - timedelta(minutes=1)
            await db.commit()
            await flush_consultations(db, state, env.run_id)
        row = await env.consultation(review)
        assert row.status == "failed" and row.failure["code"] == "critic_timeout"
        assert env.pi.of_kind("stage_outcome")[0].critique is None
        (note,) = await env.rows(PiNote, PiNote.kind == "observation")
        assert note.text == "Critic report unavailable: The Critic did not answer in time"


async def test_an_error_while_expiring_a_critic_does_not_roll_back_the_caller(
    tmp_path, monkeypatch
):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[-1]
        state = env.harness.app.state

        async def broken(*args, **kwargs):
            raise RuntimeError("models unreadable")

        monkeypatch.setattr(service, "critic_unavailable", broken)
        async with env.harness.factory() as db:
            row = await db.get(PiConsultation, UUID(review.consultation_id))
            row.created_at = datetime.now(UTC) - STAGE_REVIEW_TIMEOUT - timedelta(minutes=1)
            await db.commit()
            run = await db.get(ResearchRun, env.run_id)
            run.obligations = [
                {
                    "obligation_id": "R1",
                    "source_stage_key": "x",
                    "text": "t",
                    "target_stage_key": "y",
                }
            ]
            await flush_consultations(db, state, env.run_id)
        assert (await env.consultation(review)).status == "sent"
        (run,) = await env.rows(ResearchRun)
        assert [o["obligation_id"] for o in run.obligations] == ["R1"]


async def test_the_sweep_expires_a_late_critic_without_any_other_event(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[-1]
        state = env.harness.app.state
        await sweep(state)
        assert (await env.consultation(review)).status == "sent"  # not yet late
        async with env.harness.factory() as db:
            row = await db.get(PiConsultation, UUID(review.consultation_id))
            row.created_at = datetime.now(UTC) - STAGE_REVIEW_TIMEOUT - timedelta(minutes=1)
            await db.commit()
        await sweep(state)
        row = await env.consultation(review)
        assert row.status == "failed" and row.failure["code"] == "critic_timeout"
        assert env.pi.of_kind("stage_outcome")


async def test_a_researcher_decision_overrides_the_open_critic_review(tmp_path):
    async with pi_env(tmp_path, "copilot") as env:
        await env.settle_charter()
        assert (await env.gate(await env.current(), action="approve")).status_code == 200
        review = env.pi.of_kind("stage_review")[-1]
        stage = await env.current()
        response = await env.post(
            f"/stages/{stage['stage_run_id']}/accept", selection={"hypothesis_ids": ["H1"]}
        )
        assert response.status_code == 200, response.text
        assert (await env.consultation(review)).status == "overridden"
        assert await env.rows(PiNote, PiNote.kind == "override") == []


async def test_a_failed_critic_leaves_the_outcome_without_a_critique(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[-1]
        failed = ConsultationResult.model_validate(
            {
                "consultation_id": review.consultation_id,
                "status": "failed",
                "failure": {"code": "llm_error", "message": "Critic model failed"},
            }
        )
        assert (await env.pi.respond(review, failed)).status_code == 200
        (outcome,) = env.pi.of_kind("stage_outcome")
        assert outcome.critique is None
        (note,) = await env.rows(PiNote, PiNote.kind == "observation")
        assert note.text == "Critic report unavailable: Critic model failed"
        reasons = [e.payload.get("reason") for e in await env.events("attention.required")]
        assert "pi_failed" not in reasons


async def test_an_unreachable_pi_still_leaves_a_note_for_the_critic(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        env.pi.unreachable = True
        await fake.open_review_gate(engine_run_id)
        kinds = {c.kind: c.status for c in await env.consultations()}
        assert kinds["stage_review"] == "failed" and kinds["stage_outcome"] == "failed"
        (note,) = await env.rows(PiNote, PiNote.kind == "observation")
        assert note.text == "Critic report unavailable: The PI could not be reached"


async def test_a_critic_result_without_a_report_is_treated_as_unavailable(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[-1]
        result = critic_result(review).model_copy(update={"report": None})
        await env.pi.respond(review, result)
        assert env.pi.of_kind("stage_outcome")[0].critique is None
        assert (await env.consultation(review)).status == "failed"


@pytest.mark.parametrize(
    "responses",
    [
        [],
        [{"point_id": "C1", "response": "rejected", "reason": "It is out of scope"}],
        [{"point_id": "C9", "response": "accepted", "reason": "Wrong point"}],
    ],
)
async def test_a_pi_that_does_not_accept_every_serious_point_hands_over_even_in_auto(
    tmp_path, responses
):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        outcome = await next_outcome(env, verdict="serious", points=[serious_point()])
        await env.pi.respond(outcome, decided(outcome, ACCEPT_H1, critic_response=responses))
        assert (await env.consultation(outcome)).status == "awaiting_researcher"
        assert (await env.current())["status"] == "COMPLETED"
        assert env.fakes["exd"].requests == []
        (event,) = await env.events("pi.recommendation")
        assert event.payload["consultation_id"] == outcome.consultation_id
        (attention,) = [
            e
            for e in await env.events("attention.required")
            if e.payload.get("reason") == "pi_overrides_critic"
        ]
        assert attention.payload["consultation_id"] == outcome.consultation_id


async def test_a_pi_that_accepts_the_serious_points_decides_as_usual(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        outcome = await next_outcome(env, verdict="serious", points=[serious_point()])
        response = {"point_id": "C1", "response": "accepted", "reason": "Handed to exd"}
        await env.pi.respond(outcome, decided(outcome, ACCEPT_H1, critic_response=[response]))
        assert (await env.consultation(outcome)).status == "applied"
        assert len(env.fakes["exd"].requests) == 1


async def test_minor_points_need_no_answer(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        point = serious_point(severity="minor")
        outcome = await next_outcome(env, verdict="concerns", points=[point])
        await env.pi.respond(outcome, decided(outcome, ACCEPT_H1))
        assert (await env.consultation(outcome)).status == "applied"


async def test_the_override_is_visible_to_the_researcher_in_copilot_too(tmp_path):
    async with pi_env(tmp_path, "copilot") as env:
        await env.settle_charter()
        assert (await env.gate(await env.current(), action="approve")).status_code == 200
        outcome = await next_outcome(env, verdict="serious", points=[serious_point()])
        await env.pi.respond(outcome, decided(outcome, ACCEPT_H1))
        row = await env.consultation(outcome)
        assert row.status == "awaiting_researcher"
        assert row.result["critic_response"] == []


# --- models and cost ----------------------------------------------------------------------


async def seed_critic(env, model="critic-model"):
    async with env.harness.factory() as db:
        db.add(
            ModelProfile(
                role="critic",
                scope="global",
                provider="openai_compatible",
                model=model,
                api_key_encrypted=env.harness.app.state.secret_box.seal({"api_key": "critic-key"}),
                extra={},
                input_usd_per_mtok=3,
                output_usd_per_mtok=0,
            )
        )
        await db.commit()


async def test_the_critic_runs_on_its_own_model_and_is_priced_by_it(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await seed_critic(env)
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[0]
        assert review.models.lead.model == "review"
        assert review.models.critic.model == "critic-model"
        assert env.pi.wire[-1]["models"]["critic"]["api_key"] == "critic-key"
        snapshot = await stage_snapshot(env)
        assert set(snapshot["pricing"]) == {"stage", "review"}
        usage = {
            "lead": {"input_tokens": 1_000_000, "output_tokens": 0},
            "critic": {"input_tokens": 1_000_000, "output_tokens": 0},
        }
        await env.pi.respond(review, critic_result(review, usage=usage))
        # Lead: review price $1/Mtok. Critic: its own profile at $3/Mtok.
        assert (await env.consultation(review)).cost_usd == Decimal("4")
        outcome = env.pi.of_kind("stage_outcome")[0]
        assert outcome.models.critic.model == "stage"
        await env.pi.respond(outcome, decided(outcome, ACCEPT_H1))


async def test_the_critic_falls_back_to_the_review_model_and_its_price(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[0]
        assert review.models.critic.model == "review"
        usage = {"critic": {"input_tokens": 1_000_000, "output_tokens": 0}}
        await env.pi.respond(review, critic_result(review, usage=usage))
        assert (await env.consultation(review)).cost_usd == Decimal("1")


# --- obligations --------------------------------------------------------------------------


@pytest.fixture
def controlled():
    return {"obligation_id": "R1", "status": "controlled", "locator": "plan:baselines"}


async def accept_over_the_risk(env):
    outcome = await blocked_outcome(env, blocking("hypothesis:H1"))
    await env.pi.respond(
        outcome,
        decided(
            outcome,
            {"action": "accept", "selection": {"hypothesis_ids": ["H1", "H2"]}},
            handoff=[RISK_LINE],
        ),
    )
    assert (await env.consultation(outcome)).status == "applied"


async def obligations(env):
    return (await env.rows(ResearchRun, ResearchRun.id == env.run_id))[0].obligations


async def test_an_accepted_blocking_finding_becomes_an_obligation_for_the_next_stage(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await accept_over_the_risk(env)
        (item,) = await obligations(env)
        assert item == {
            "obligation_id": "R1",
            "source_stage_key": "hypothesis",
            "item_id": "H1",
            "finding_id": "F1-1",
            "text": "No falsification criterion",
            "target_stage_key": "experiment_plan",
            "status": "open",
            "locator": None,
            "note": None,
            "checked_by": None,
        }
        guidance = env.fakes["exd"].requests[0].inputs.params["guidance"]
        assert (
            "Known risk R1 (from hypothesis, H1, finding F1-1): No falsification criterion. "
            "State in your output how it is controlled."
        ) in guidance
        overview = (await env.client.get(env.url("/pi"))).json()["data"]
        assert [o["obligation_id"] for o in overview["obligations"]] == ["R1"]


async def test_a_clean_accept_creates_no_obligation(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        outcome = await blocked_outcome(env, blocking("hypothesis:H1"))
        await env.pi.respond(
            outcome, decided(outcome, {"action": "accept", "selection": {"hypothesis_ids": ["H2"]}})
        )
        assert await obligations(env) == []
        guidance = env.fakes["exd"].requests[0].inputs.params["guidance"]
        assert not any(line.startswith("Known risk") for line in guidance)


async def test_the_critic_report_sets_the_obligation_status(tmp_path, controlled):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await accept_over_the_risk(env)
        fake = env.fakes["exd"]
        (engine_run_id,) = fake.states
        await fake.open_review_gate(engine_run_id)
        review = env.pi.of_kind("stage_review")[-1]
        assert review.situation.stage_key == "experiment_plan"
        assert [o.obligation_id for o in review.obligations] == ["R1"]
        await env.pi.respond(review, critic_result(review, obligations=[controlled]))
        (item,) = await obligations(env)
        assert item["status"] == "controlled" and item["locator"] == "plan:baselines"
        assert item["checked_by"] == review.consultation_id
        outcome = env.pi.of_kind("stage_outcome")[-1]
        assert [(o.obligation_id, o.status) for o in outcome.obligations] == [("R1", "controlled")]
        await env.pi.respond(outcome, decided(outcome, {"action": "accept"}))
        close = env.pi.of_kind("close")[0]
        assert [o.obligation_id for o in close.obligations] == ["R1"]


async def test_an_unresolved_obligation_keeps_its_note(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await accept_over_the_risk(env)
        fake = env.fakes["exd"]
        (engine_run_id,) = fake.states
        await fake.open_review_gate(engine_run_id)
        check = {"obligation_id": "R1", "status": "unresolved", "note": "No baseline in the plan"}
        outcome = await next_outcome(env, obligations=[check])
        (item,) = await obligations(env)
        assert item["status"] == "unresolved" and item["note"] == "No baseline in the plan"
        assert outcome.obligations[0].status == "unresolved"


async def test_obligations_for_other_stages_are_not_sent_to_the_critic(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        await env.settle_charter()
        review = env.pi.of_kind("stage_review")[0]
        assert review.obligations == []


# --- inputs of the next stage and the run API ---------------------------------------------


async def test_the_next_stage_gets_the_other_artifacts_its_engine_accepts(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False, start=False) as env:
        exd = env.fakes["exd"]
        exd.capabilities.input_schemas = [
            "i2h.hypotheses/v1",
            "review.findings/v1",
            "i2h.evidence/v1",
        ]
        await env.start_run("auto")
        await env.settle_charter()
        fake = env.fakes["i2h"]
        (engine_run_id,) = fake.states
        body = json.dumps({"schema_version": "i2h.evidence/v1", "cards": []}).encode()
        for _ in range(2):
            await fake._upload(engine_run_id, "i2h.evidence/v1", body)
        await fake._upload(engine_run_id, "pipeline.recommendation/v1", b"{}")
        await fake.open_review_gate(engine_run_id)
        outcome = await next_outcome(env)
        await env.pi.respond(outcome, decided(outcome, ACCEPT_H1))
        refs = exd.requests[0].inputs.artifacts
        assert [(r.schema_id, r.primary) for r in refs] == [
            ("i2h.hypotheses/v1", True),
            ("i2h.evidence/v1", False),
            ("review.findings/v1", False),
        ]
        assert refs[1].version == 2
        assert refs[1].sha256 == hashlib.sha256(body).hexdigest()


async def test_the_run_api_says_when_the_cost_is_unknown(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        url = f"{PROJECTS}/{env.project['id']}/runs"
        assert (await env.client.get(env.url())).json()["data"]["cost_unknown"] is False
        async with env.harness.factory() as db:
            (await db.get(ResearchRun, env.run_id)).cost_unknown = True
            await db.commit()
        assert (await env.client.get(env.url())).json()["data"]["cost_unknown"] is True
        listed = (await env.client.get(url)).json()["data"]
        assert [item["cost_unknown"] for item in listed] == [True]


async def test_every_user_directive_stays_in_the_notebook_however_old(tmp_path):
    async with pi_env(tmp_path, "auto") as env:
        base = datetime.now(UTC) - timedelta(days=1)
        async with env.harness.factory() as db:
            for n in range(context.NOTEBOOK_LIMIT + 10):
                directive = n == 0
                db.add(
                    PiNote(
                        research_run_id=env.run_id,
                        kind="directive" if directive else "observation",
                        text=f"note {n}",
                        actor="user" if directive else "pi",
                        created_at=base + timedelta(seconds=n),
                    )
                )
            db.add(
                PiNote(
                    research_run_id=env.run_id,
                    kind="directive",
                    text="pi directive",
                    actor="pi",
                    created_at=base,
                )
            )
            await db.commit()
            run = await db.get(ResearchRun, env.run_id)
            entries = await context.notebook_entries(db, run)
        texts = [entry.text for entry in entries]
        assert texts[0] == "note 0" and "pi directive" not in texts
        assert len(texts) == context.NOTEBOOK_LIMIT + 1
        assert texts[1:] == sorted(texts[1:], key=lambda t: int(t.split()[-1]))


async def test_an_accepted_serious_critic_point_becomes_an_obligation(tmp_path):
    async with pi_env(tmp_path, "auto", scripted=False) as env:
        await env.settle_charter()
        (engine_run_id,) = env.fakes["i2h"].states
        # A blocking finding on an unselected hypothesis creates no obligation.
        await env.fakes["i2h"].open_review_gate(engine_run_id)
        outcome = await next_outcome(
            env, verdict="serious", points=[serious_point(lens="integrity", ref="integrity:spin")]
        )
        await env.pi.respond(
            outcome,
            decided(
                outcome,
                ACCEPT_H1,
                critic_response=[{"point_id": "C1", "response": "accepted", "reason": "Known"}],
            ),
        )
        (item,) = await obligations(env)
        assert (item["finding_id"], item["item_id"], item["source_stage_key"]) == (
            "C1",
            None,
            "hypothesis",
        )
        assert item["target_stage_key"] == "experiment_plan"
        assert item["text"] == "[integrity] The result does not address the objective"
