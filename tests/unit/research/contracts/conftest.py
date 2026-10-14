import pytest


@pytest.fixture
def request_data():
    return {
        "contract_version": "1.0",
        "platform_run_id": "p1",
        "stage_run_id": "s1",
        "stage_key": "hypothesis",
        "interaction_mode": "light",
        "models": {
            "stage": {"provider": "openai_compatible", "model": "draft", "api_key": "sk-test"},
            "review": {"provider": "openai_compatible", "model": "review", "api_key": "sk-test"},
        },
        "integrations": {"search": "integration-secret"},
        "budget_usd": "10",
        "callback_url": "http://platform.test/internal/engines/fake/runs/s1",
    }
