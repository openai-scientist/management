"""Conservative affected tests for local work, pre-push and CI; stdlib only."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "scripts" / "affected-lanes.json"
LOCAL_EXCLUSIONS = (
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".worktrees",
    ".kilo",
    "runs",
    "outputs",
)


def load_map():
    return json.loads(MAP.read_text(encoding="utf-8"))


def full_plan(reason):
    config = load_map()
    return {"full": True, "lanes": sorted(config["lanes"]), "paths": [], "reason": reason}


def select_paths(paths):
    config = load_map()
    selected = set()
    for raw in paths:
        path = raw.replace("\\", "/")
        if path in config["ignored_files"]:
            continue
        if path in config["shared_files"] or path.startswith(tuple(config["shared_prefixes"])):
            return full_plan(f"shared dependency: {path}")
        matches = {
            lane
            for lane, prefix in config["lanes"].items()
            if path == prefix or path.startswith(prefix + "/")
        }
        if not matches:
            return full_plan(f"unmapped path: {path}")
        selected.update(matches)
    lanes = sorted(selected)
    return {
        "full": False,
        "lanes": lanes,
        "paths": [config["lanes"][lane] for lane in lanes],
        "reason": "affected test owners" if lanes else "no test changes",
    }


def git(*args):
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    ).stdout


def changed_paths(base=None):
    commands = [
        ("diff", "--name-only", "--no-renames", "-z", "--"),
        ("diff", "--cached", "--name-only", "--no-renames", "-z", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        (
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            "src",
            "tests",
            "scripts",
            "alembic",
            "docs/contract",
            *(f":(exclude)**/{name}/**" for name in LOCAL_EXCLUSIONS),
            ":(exclude)**/.env*",
            ":(exclude)**/*.pyc",
            ":(exclude)**/*.log",
        ),
    ]
    if base is not None:
        merge_base = git("merge-base", "--", base, "HEAD").strip()
        if not merge_base:
            raise ValueError("git returned no merge base")
        commands.append(("diff", "--name-only", "--no-renames", "-z", merge_base, "HEAD", "--"))
    return sorted({path for command in commands for path in git(*command).split("\0") if path})


def plan_changes(base=None):
    try:
        return select_paths(changed_paths(base))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        return full_plan(f"diff unavailable ({type(error).__name__}); running everything")


def validate_plan(plan):
    config = load_map()
    if not isinstance(plan, dict) or type(plan.get("full")) is not bool:
        raise ValueError("invalid selection plan")
    lanes = plan.get("lanes")
    if not isinstance(lanes, list) or any(lane not in config["lanes"] for lane in lanes):
        raise ValueError("unknown selection lanes")
    if lanes != sorted(set(lanes)):
        raise ValueError("selection lanes must be unique and sorted")
    expected = [] if plan["full"] else [config["lanes"][lane] for lane in lanes]
    if plan.get("paths") != expected or (plan["full"] and lanes != sorted(config["lanes"])):
        raise ValueError("selection paths do not match the lane map")
    return plan


def run_plan(plan, pytest_args=()):
    plan = validate_plan(plan)
    if not plan["lanes"]:
        print("No affected test lanes; pytest skipped.")
        return 0
    paths = plan["paths"]
    if any(not (ROOT / path).exists() for path in paths):
        print("A selected test owner was deleted; running the full suite.", file=sys.stderr)
        paths = []
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *paths, *pytest_args], cwd=ROOT, check=False
    ).returncode


def gate_passes(needs):
    return (
        isinstance(needs, dict)
        and bool(needs)
        and all(
            isinstance(job, dict) and job.get("result") in {"success", "skipped"}
            for job in needs.values()
        )
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="staged, unstaged and untracked changes")
    mode.add_argument("--base", help="target ref; whole merge-base diff plus working changes")
    mode.add_argument("--paths", nargs="*", help="explicit changed paths (including deleted paths)")
    mode.add_argument("--full", action="store_true")
    mode.add_argument("--run-plan", type=Path, help="execute an immutable saved selection")
    mode.add_argument(
        "--gate", help="GitHub needs JSON; reject failure, cancellation or missing result"
    )
    parser.add_argument("--output", type=Path, help="save a selection plan as JSON")
    parser.add_argument("--run", action="store_true", help="execute the selection")
    parser.add_argument(
        "pytest_args", nargs=argparse.REMAINDER, help="scoped pytest options after --"
    )
    args = parser.parse_args(argv)
    try:
        if args.gate is not None:
            return 0 if gate_passes(json.loads(args.gate)) else 1
        if args.run_plan:
            plan = validate_plan(json.loads(args.run_plan.read_text(encoding="utf-8")))
        elif args.full:
            plan = full_plan("full-suite event or explicit request")
        elif args.paths is not None:
            plan = select_paths(args.paths)
        else:
            plan = plan_changes(args.base)
        rendered = json.dumps(plan, ensure_ascii=True)
        print(rendered, flush=True)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        if args.run or args.run_plan:
            options = args.pytest_args
            return run_plan(plan, options[1:] if options[:1] == ["--"] else options)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Affected-test selection failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
