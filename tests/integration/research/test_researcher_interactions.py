from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from management.projects.models import Notification
from management.research.contracts import Phase
from management.research.engines.client import EngineUnavailable
from management.research.models import ResearchRun, RunArtifact, RunEvent, StageDecision, StageRun
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import add_member
from tests.helpers.research import open_pipeline_api


@pytest.fixture
async def interaction(tmp_path):
    async with open_pipeline_api(tmp_path) as state:
        yield state


def base(state):
    return (
        f"/api/v1/projects/{state[3]['id']}/runs/{state[4]['id']}/stages/"
        f"{state[4]['current_stage']['stage_run_id']}"
    )


async def stage(state):
    async with state[0].factory() as db:
        return await db.get(StageRun, UUID(state[4]["current_stage"]["stage_run_id"]))


async def question(state):
    row = await stage(state)
    return await state[5]["i2h"].open_question_gate(
        row.engine_run_id, step_key="scope", options=["a", "b"], default="a"
    )


async def answer(state, gate, payload):
    return await state[1].post(
        base(state) + f"/gates/{gate.gate_id}",
        json=payload,
        headers=mutation_headers(state[2]["csrf_token"]),
    )


async def test_copilot_answers_question_gate(interaction):
    gate = await question(interaction)
    response = await answer(interaction, gate, {"action": "answer", "option_id": "b"})
    assert response.status_code == 200, response.text
    assert (await stage(interaction)).open_gate is None
    async with interaction[0].factory() as db:
        decision = (await db.scalars(select(StageDecision))).one()
        assert decision.action == "answer_gate" and decision.payload["option_id"] == "b"


async def test_invalid_option_422(interaction):
    gate = await question(interaction)
    interaction[5]["i2h"].set_unreachable(True)
    response = await answer(interaction, gate, {"action": "answer", "option_id": "other"})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "GATE_OPTION_INVALID"
    async with interaction[0].factory() as db:
        assert (await db.scalars(select(StageDecision))).all() == []


async def test_question_rejects_approve_without_calling_engine(interaction):
    gate = await question(interaction)
    interaction[5]["i2h"].set_unreachable(True)
    response = await answer(interaction, gate, {"action": "approve"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "GATE_ACTION_INVALID"


async def test_pending_gate_retry_keeps_original_decision_maker(interaction, monkeypatch):
    harness, client, manager, project, _, fakes = interaction
    row = await stage(interaction)
    gate = await fakes["i2h"].open_review_gate(row.engine_run_id)
    engine = harness.app.state.engines.get("i2h")
    original = engine.answer_gate
    first = True

    async def lose_ack(*args, **kwargs):
        nonlocal first
        result = await original(*args, **kwargs)
        if first:
            first = False
            raise EngineUnavailable("Lost acknowledgement after the engine applied the answer")
        return result

    monkeypatch.setattr(engine, "answer_gate", lose_ack)
    payload = {"action": "comment", "findings": [finding(gate.artifact_ids[0])]}
    assert (await answer(interaction, gate, payload)).status_code == 503
    async with harness.client() as researcher_client:
        researcher = await login(
            harness, researcher_client, uid="researcher", email="researcher@example.com"
        )
        await add_member(client, manager, project["id"], "researcher@example.com", "researcher")
        retried = await researcher_client.post(
            base(interaction) + f"/gates/{gate.gate_id}",
            json=payload,
            headers=mutation_headers(researcher["csrf_token"]),
        )
    assert retried.status_code == 200, retried.text
    async with harness.factory() as db:
        decision = (await db.scalars(select(StageDecision))).one()
        assert decision.status == "applied"
        assert decision.decided_by_user_id == UUID(manager["user"]["id"])


def finding(artifact_id):
    return {
        "finding_id": "researcher-1",
        "anchor": {"artifact_id": artifact_id, "locator": "H1"},
        "severity": "major",
        "issue": "Clarify the baseline",
        "author": "reviewer",
    }


async def test_comment_forwards_findings_to_engine(interaction):
    row = await stage(interaction)
    gate = await interaction[5]["i2h"].open_review_gate(row.engine_run_id)
    response = await answer(
        interaction, gate, {"action": "comment", "findings": [finding(gate.artifact_ids[0])]}
    )
    assert response.status_code == 200, response.text
    async with interaction[0].factory() as db:
        decision = (await db.scalars(select(StageDecision))).one()
        assert decision.payload["findings"][0]["author"] == "researcher"
        assert decision.decided_by_user_id == UUID(interaction[2]["user"]["id"])
    assert (await stage(interaction)).phase == "revising"
    retried = await answer(
        interaction, gate, {"action": "comment", "findings": [finding(gate.artifact_ids[0])]}
    )
    assert retried.status_code == 200


@pytest.mark.parametrize("artifact_id", [None, "invalid", str(uuid4())])
async def test_comment_rejects_artifact_outside_stage(interaction, artifact_id):
    row = await stage(interaction)
    gate = await interaction[5]["i2h"].open_review_gate(row.engine_run_id)
    interaction[5]["i2h"].set_unreachable(True)
    body = finding(artifact_id) | {"author": "researcher"}
    response = await answer(interaction, gate, {"action": "comment", "findings": [body]})
    assert response.status_code == 422, response.text
    async with interaction[0].factory() as db:
        assert (await db.scalars(select(StageDecision))).all() == []


async def test_comment_rejects_existing_artifact_not_owned_by_stage(interaction):
    row = await stage(interaction)
    gate = await interaction[5]["i2h"].open_review_gate(row.engine_run_id)
    async with interaction[0].factory() as db:
        artifact = RunArtifact(
            run_id=row.research_run_id,
            stage_run_id=None,
            kind="other",
            filename="legacy.json",
            content_type="application/json",
            storage_key="legacy",
            size_bytes=2,
            sha256="0" * 64,
        )
        db.add(artifact)
        await db.commit()
        artifact_id = str(artifact.id)
    response = await answer(
        interaction, gate, {"action": "comment", "findings": [finding(artifact_id)]}
    )
    assert response.status_code == 422, response.text


async def test_review_history_lists_rounds(interaction):
    row = await stage(interaction)
    fake = interaction[5]["i2h"]
    gate = await fake.open_review_gate(row.engine_run_id)
    fake.scripted()
    body = finding(gate.artifact_ids[0]) | {"author": "researcher"}
    assert (
        await answer(interaction, gate, {"action": "comment", "findings": [body]})
    ).status_code == 200
    await fake._upload(
        row.engine_run_id,
        "revision.response/v1",
        b'{"round":2,"artifact_before":"a1","artifact_after":"a2","items":[]}',
        review_round=2,
    )
    await fake._upload(
        row.engine_run_id,
        "revision.diff/v1",
        b'{"round":2,"artifact_before":"a1","artifact_after":"a2","format":"unified","patch":""}',
        review_round=2,
    )
    response = await interaction[1].get(base(interaction) + "/reviews")
    assert response.status_code == 200, response.text
    rounds = response.json()["data"]
    assert [item["review_round"] for item in rounds] == [1, 2]
    assert [a["schema_id"] for a in rounds[0]["artifacts"]] == ["review.findings/v1"]
    assert {a["schema_id"] for a in rounds[1]["artifacts"]} == {
        "review.findings/v1",
        "revision.response/v1",
        "revision.diff/v1",
    }
    ids = {a["artifact_id"] for item in rounds for a in item["artifacts"]}
    assert len(ids) == 4


async def test_attention_notification_on_gate(interaction):
    harness, client, manager, project, _, fakes = interaction
    members = {}
    for role in ("researcher", "reviewer"):
        async with harness.client() as member_client:
            members[role] = await login(
                harness, member_client, uid=role, email=f"{role}@example.com"
            )
        await add_member(client, manager, project["id"], f"{role}@example.com", role)
    row = await stage(interaction)
    # A phase change alone must not ask for attention (i2h continues work in this phase).
    await fakes["i2h"].to_phase(row.engine_run_id, Phase.awaiting_researcher)
    async with harness.factory() as db:
        assert (
            await db.scalars(select(RunEvent).where(RunEvent.type == "attention.required"))
        ).all() == []
    await question(interaction)
    # A callback retry must not create a second attention event or duplicate notifications.
    event = fakes["i2h"].events[row.engine_run_id][-1]
    retry = await client.post(
        f"/api/v1/internal/engines/i2h/runs/{row.id}/events",
        headers={"X-Service-Key": "test-service-key"},
        json={"events": [event.model_dump(mode="json", by_alias=True)]},
    )
    assert retry.json() == {"accepted": 0, "skipped": 1}
    async with harness.factory() as db:
        notices = (
            await db.scalars(select(Notification).where(Notification.run_id == row.research_run_id))
        ).all()
        assert {notice.recipient_user_id for notice in notices} == {
            UUID(manager["user"]["id"]),
            UUID(members["researcher"]["user"]["id"]),
        }
        assert len(notices) == 2 and all(notice.kind == "run_awaiting_review" for notice in notices)
        events = (
            await db.scalars(select(RunEvent).where(RunEvent.type == "attention.required"))
        ).all()
        assert len(events) == 1 and events[0].event_class == "platform"


async def test_auto_does_not_send_manual_attention_in_s2(interaction):
    row = await stage(interaction)
    async with interaction[0].factory() as db:
        run = await db.get(ResearchRun, row.research_run_id)
        run.interaction_mode = "auto"
        await db.commit()
    await interaction[5]["i2h"].open_review_gate(row.engine_run_id)
    async with interaction[0].factory() as db:
        assert (
            await db.scalars(select(RunEvent).where(RunEvent.type == "attention.required"))
        ).all() == []


@pytest.mark.parametrize("terminal", ["publish", "fail"])
async def test_terminal_stage_asks_for_attention(interaction, terminal):
    row = await stage(interaction)
    fake = interaction[5]["i2h"]
    if terminal == "publish":
        await fake.publish(row.engine_run_id)
    else:
        await fake.fail(row.engine_run_id, "internal_error", "Failed")
    async with interaction[0].factory() as db:
        events = (
            await db.scalars(select(RunEvent).where(RunEvent.type == "attention.required"))
        ).all()
        assert len(events) == 1
        notices = (
            await db.scalars(select(Notification).where(Notification.run_id == row.research_run_id))
        ).all()
        assert len(notices) == 1
