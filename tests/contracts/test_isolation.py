from pathlib import Path

import research_contracts


def test_contract_source_has_no_platform_dependency():
    source = Path(research_contracts.__file__).parent
    offenders = [
        str(path.relative_to(source))
        for path in source.rglob("*.py")
        if ".venv" not in path.parts and "platform_be" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
