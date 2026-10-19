import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from management.core.config import Settings
from management.integrations.worker import secret_box

pytestmark = pytest.mark.unit


def test_secret_box_reads_the_key_from_settings() -> None:
    key = SecretStr(Fernet.generate_key().decode())
    settings = Settings.model_construct(connection_secret_key=key)
    box = secret_box(settings)
    assert box is not None
    assert box.open(box.seal({"url": "https://example.com/hook"})) == {
        "url": "https://example.com/hook"
    }


def test_no_key_means_no_box() -> None:
    assert secret_box(Settings.model_construct(connection_secret_key=None)) is None
