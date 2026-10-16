import json
import subprocess

import pytest

from scripts import affected_tests as affected


@pytest.mark.parametrize(
    ("paths", "lanes", "full"),
    [
        (["tests/unit/connections/test_connector_values.py"], ["unit-connections"], False),
        (["tests/integration/research/deleted.py"], ["integration-research"], False),
        (
            ["tests/unit/connections/old.py", "tests/unit/infra/new.py"],
            ["unit-connections", "unit-infra"],
            False,
        ),
        (["src/management/connections/connectors/values.py"], None, True),
        (["tests/helpers/identity.py"], None, True),
        (["uv.lock"], None, True),
        ([".github/workflows/ci.yml"], None, True),
        (["scripts/affected-lanes.json"], None, True),
        (["docs/contract/schema/Capabilities.json"], None, True),
        (["new_package/unknown.py"], None, True),
        (["README.md", "AGENTS.md", "docs/testing.md"], [], False),
    ],
)
def test_selection_preserves_coverage(paths, lanes, full):
    plan = affected.select_paths(paths)
    assert plan["full"] is full
    assert plan["lanes"] == (sorted(affected.load_map()["lanes"]) if full else lanes)


def test_git_base_includes_whole_diff_staged_unstaged_and_untracked(monkeypatch):
    calls = []

    def git(*args):
        calls.append(args)
        if args[0] == "merge-base":
            return "abc123\n"
        if args[0] == "ls-files":
            return "src/new.py\0"
        if "--cached" in args:
            return "tests/unit/infra/staged.py\0"
        if "abc123" in args:
            return "tests/unit/connections/old.py\0tests/unit/infra/renamed.py\0"
        return "tests/unit/research/deleted.py\0"

    monkeypatch.setattr(affected, "git", git)
    paths = affected.changed_paths("origin/main")
    assert set(paths) == {
        "src/new.py",
        "tests/unit/infra/staged.py",
        "tests/unit/connections/old.py",
        "tests/unit/infra/renamed.py",
        "tests/unit/research/deleted.py",
    }
    assert all("--no-renames" in call for call in calls if call[0] == "diff")
    assert any("abc123" in call and "HEAD" in call for call in calls)


@pytest.mark.parametrize("error", [OSError("git missing"), subprocess.CalledProcessError(1, "git")])
def test_missing_git_or_base_runs_full(monkeypatch, error):
    def broken(*args):
        raise error

    monkeypatch.setattr(affected, "git", broken)
    assert affected.plan_changes("missing-base")["full"] is True


@pytest.mark.parametrize("result", ["success", "skipped", "failure", "cancelled", "", None])
def test_gate_accepts_only_success_or_skipped(result):
    assert affected.gate_passes({"test": {"result": result}}) is (result in {"success", "skipped"})


def test_gate_rejects_missing_and_partial_results():
    assert not affected.gate_passes({})
    assert not affected.gate_passes({"test": {}})
    assert not affected.gate_passes({"test": {"result": "success"}, "other": {"result": "failure"}})


def test_saved_plan_runs_identical_paths_and_propagates_failure(monkeypatch, tmp_path):
    plan = affected.select_paths(["tests/unit/connections/test_connector_values.py"])
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(affected.subprocess, "run", run)
    assert affected.main(["--run-plan", str(path)]) == 1
    assert commands[0][-1] == "tests/unit/connections"


def test_docs_plan_does_not_start_pytest(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("pytest started for docs-only plan")

    monkeypatch.setattr(affected.subprocess, "run", unexpected)
    assert affected.run_plan(affected.select_paths(["README.md"])) == 0


def test_invalid_saved_selection_fails_instead_of_skipping(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text('{"full": false, "lanes": [], "paths": ["src/unknown.py"]}', encoding="utf-8")
    assert affected.main(["--run-plan", str(path)]) == 1


def test_every_test_file_has_an_owner_lane():
    for path in (affected.ROOT / "tests").rglob("test_*.py"):
        plan = affected.select_paths([path.relative_to(affected.ROOT).as_posix()])
        assert not plan["full"], path
        assert len(plan["lanes"]) == 1, path


def test_real_git_local_diff_keeps_staged_deleted_renamed_and_untracked(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "old.py").write_text("original", encoding="utf-8")
    (tmp_path / "deleted.py").write_text("delete", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "old.py", "deleted.py"], check=True)
    (tmp_path / "old.py").rename(tmp_path / "new.py")
    (tmp_path / "deleted.py").unlink()
    (tmp_path / "untracked.py").write_text("new", encoding="utf-8")
    monkeypatch.setattr(affected, "ROOT", tmp_path)
    assert set(affected.changed_paths()) == {"old.py", "new.py", "deleted.py", "untracked.py"}


def test_deleted_owner_runs_full_instead_of_skipping(monkeypatch, tmp_path):
    plan = affected.select_paths(["tests/unit/connections/deleted.py"])
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(affected, "ROOT", tmp_path)
    monkeypatch.setattr(affected.subprocess, "run", run)
    assert affected.run_plan(plan) == 0
    assert commands == [[affected.sys.executable, "-m", "pytest", "-q"]]


def test_ignored_executable_source_is_included_but_caches_and_secrets_are_excluded(
    monkeypatch, tmp_path
):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("src/\n", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "generated.py").write_text("value = 1", encoding="utf-8")
    (source / ".env.local").write_text("dummy-only", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "generated.pyc").write_bytes(b"cache")
    monkeypatch.setattr(affected, "ROOT", tmp_path)
    paths = affected.changed_paths()
    assert "src/generated.py" in paths
    assert "src/.env.local" not in paths
    assert "src/__pycache__/generated.pyc" not in paths
    assert affected.select_paths([path for path in paths if path.startswith("src/")])["full"]
