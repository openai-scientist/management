import json

from sqlalchemy import select

from management.audit.models import AuditEvent
from management.identity.bootstrap_admin import bootstrap_admin
from management.research.model_profiles import load_models
from management.research.models import ModelProfile
from tests.conftest import login, mutation_headers

MODELS = "/api/v1/admin/models"
INTEGRATIONS = "/api/v1/admin/integrations"


async def admin_session(harness, client):
    session = await login(harness, client, uid="admin", email="admin@example.com")
    await bootstrap_admin(
        "admin@example.com", settings=harness.settings, session_factory=harness.factory
    )
    return session


def model_payload(model="draft", key="model-secret"):
    return dict(
        provider="openai_compatible",
        model=model,
        api_key=key,
        input_usd_per_mtok="1.5",
        output_usd_per_mtok="3",
    )


async def test_non_admin_forbidden(harness):
    async with harness.client() as client:
        await login(harness, client, uid="member", email="member@example.com")
        assert (await client.get(MODELS)).status_code == 403


async def test_put_and_get_hides_key_and_secret_not_in_audit(harness):
    async with harness.client() as client:
        session = await admin_session(harness, client)
        headers = mutation_headers(session["csrf_token"])
        put = await client.put(MODELS + "/stage", json=model_payload(), headers=headers)
        assert put.status_code == 200, put.text
        response = await client.get(MODELS)
        assert response.json()["data"]["stage"]["has_api_key"]
        assert "model-secret" not in put.text + response.text
        assert (
            await client.put(MODELS + "/review", json=model_payload("review"), headers=headers)
        ).status_code == 200
    async with harness.factory() as db:
        profiles = list((await db.scalars(select(ModelProfile))).all())
        assert all("model-secret" not in row.api_key_encrypted for row in profiles)
        models = await load_models(db, harness.app.state.secret_box)
        assert models.stage.api_key.get_secret_value() == "model-secret"
        audit = list((await db.scalars(select(AuditEvent))).all())
        assert "model-secret" not in json.dumps([a.details for a in audit])


async def test_review_same_as_stage_rejected(harness):
    async with harness.client() as client:
        session = await admin_session(harness, client)
        headers = mutation_headers(session["csrf_token"])
        await client.put(MODELS + "/stage", json=model_payload(), headers=headers)
        response = await client.put(MODELS + "/review", json=model_payload(), headers=headers)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REVIEW_MODEL_SAME_AS_STAGE"


async def test_blank_api_key_keeps_previous(harness):
    async with harness.client() as client:
        session = await admin_session(harness, client)
        headers = mutation_headers(session["csrf_token"])
        await client.put(MODELS + "/stage", json=model_payload(), headers=headers)
        await client.put(MODELS + "/stage", json=model_payload(key=""), headers=headers)
    async with harness.factory() as db:
        profile = await db.scalar(select(ModelProfile).where(ModelProfile.role == "stage"))
        assert (
            harness.app.state.secret_box.open(profile.api_key_encrypted)["api_key"]
            == "model-secret"
        )


async def test_integration_put_list_delete(harness):
    async with harness.client() as client:
        session = await admin_session(harness, client)
        headers = mutation_headers(session["csrf_token"])
        response = await client.put(
            INTEGRATIONS + "/search", json={"value": "integration-secret"}, headers=headers
        )
        assert response.status_code == 200, response.text
        listed = await client.get(INTEGRATIONS)
        assert listed.json()["data"][0]["name"] == "search"
        assert "integration-secret" not in listed.text + response.text
        assert (await client.delete(INTEGRATIONS + "/search", headers=headers)).status_code == 200
        assert (await client.get(INTEGRATIONS)).json()["data"] == []


async def test_secret_key_not_configured_and_csrf_required(harness):
    async with harness.client() as client:
        session = await admin_session(harness, client)
        assert (await client.put(MODELS + "/stage", json=model_payload())).status_code == 403
        harness.app.state.secret_box = None
        response = await client.put(
            MODELS + "/stage", json=model_payload(), headers=mutation_headers(session["csrf_token"])
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SECRET_KEY_NOT_CONFIGURED"
