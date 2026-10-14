import pytest

from scripts.smoke_pipeline import SmokeError, smoke
from tests.fakes.engines import engine_overrides, install_fake_engines, seed_models
from tests.helpers.harness import open_harness
from tests.helpers.identity import login
from tests.helpers.projects import create_project


async def test_smoke_finishes_two_stage_pipeline(tmp_path):
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        fakes = install_fake_engines(harness)
        for fake in fakes.values():
            fake.scripted()
        async with harness.client() as client:
            session = await login(harness, client, uid="owner", email="owner@example.com")
            project = await create_project(client, session)
            await seed_models(harness, session["user"]["id"])
            result = await smoke(
                client, project["id"], session["csrf_token"], timeout=5, poll_interval=0
            )
        assert result["status"] == "completed"
        assert fakes["exd"].requests[0].inputs.decisions.selection == {
            "hypothesis_ids": ["H1", "H2"]
        }


async def test_smoke_times_out_when_engine_never_opens_gate(tmp_path):
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        install_fake_engines(harness)
        async with harness.client() as client:
            session = await login(harness, client, uid="owner", email="owner@example.com")
            project = await create_project(client, session)
            await seed_models(harness, session["user"]["id"])
            with pytest.raises(SmokeError, match="Timed out"):
                await smoke(
                    client, project["id"], session["csrf_token"], timeout=0.01, poll_interval=0
                )
