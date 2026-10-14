from pathlib import Path

import management.research.contracts as contracts


def test_contract_source_has_no_platform_dependency():
    source = Path(contracts.__file__).parent
    offenders = [
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if ".venv" not in path.parts and "management" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
