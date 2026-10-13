import pytest

from management.contracts import (
    TERMINAL_PHASES,
    ContractModel,
    Phase,
    is_compatible,
    parse_schema_id,
)


def test_is_compatible_same_major():
    assert is_compatible("1.0") and is_compatible("1.7")
    assert not is_compatible("2.0")


@pytest.mark.parametrize("version", ["", "one", "1", "1.x", "1.0.extra", "01.0"])
def test_malformed_version_is_incompatible(version):
    assert not is_compatible(version)


def test_parse_schema_id():
    assert parse_schema_id("i2h.hypotheses/v1") == ("i2h.hypotheses", 1)


@pytest.mark.parametrize("schema_id", ["hypotheses", "/v1", "i2h.hypotheses/v0", "x/v1/more"])
def test_parse_schema_id_rejects_invalid(schema_id):
    with pytest.raises(ValueError):
        parse_schema_id(schema_id)


def test_contract_receiver_ignores_additive_fields():
    class M(ContractModel):
        a: int

    assert M(a=1, b=2).model_dump() == {"a": 1}


def test_compatible_semver_patch():
    assert is_compatible("1.0.0")
    assert not is_compatible("2.0.0")


def test_terminal_phases():
    assert TERMINAL_PHASES == {Phase.published, Phase.failed, Phase.cancelled}
