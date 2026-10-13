import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_import_contracts_hold() -> None:
    """The layering and router contracts in pyproject.toml hold for the current imports."""
    result = subprocess.run(
        [sys.executable, "-c", "from importlinter.cli import lint_imports_command as c; c()"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
