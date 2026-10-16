# AGENTS.md

Conventions for working in this repo. `README.md` explains what the Platform does; this file says how to change it safely.

## Verify

Run from the repo root. Use `uv run` for everything.

| Check | Command |
|---|---|
| Fast tests (no app, DB or network) | `uv run pytest tests/unit -q` |
| Affected working changes | `uv run python scripts/affected_tests.py --local --run` |
| Pre-push / whole PR diff | `uv run python scripts/affected_tests.py --base origin/main --run` |
| Full tests (parallel, ~2 min) | `uv run --with pytest-xdist pytest -q -p no:cacheprovider -n 8` |
| Lint | `uv run ruff check src alembic tests scripts` |
| Format | `uv run ruff format --check src alembic tests scripts` |
| Import rules | `uv run lint-imports` |
| Migrations | `uv run python -m alembic <command>` |

While editing, run the relevant file or feature owner, for example
`uv run pytest tests/unit/connections -q` or `uv run pytest tests/integration/research -q`.
These are scoped checks. Use the affected command for conservative dependency coverage;
source changes currently select the full suite because the harness imports the whole app.
Run the full suite once, in parallel, with lint, format and import rules before final handoff
or commit; do not run it serially.
The shared lane map, timings and CI gate are documented in `docs/testing.md`.
Tests run on SQLite in memory. Tests marked `live` need a real service and are skipped unless a `PLATFORM_*` variable is set (see `README.md`).

## Feature layout and layers

Each feature is a package: `src/management/<feature>/` with `router`, `schemas`, `service` and `models`. A feature may split these into sub-packages (for example `connections/router/`).

Layers, high to low. A module imports only from lower layers. `lint-imports` enforces this.

1. `research`, `monitoring`
2. `datasets`
3. `connections`
4. `projects`
5. `identity`, `notifications`
6. `audit`, `access`
7. `core`, `db`, `infra`

Rules:

- No module imports the router of another feature, or of its own.
- Features on the same layer (joined by `|` above) do not import each other's services.
- Model modules are shared data. Any feature may import `management.*.models`.
- `management/main.py` mounts the routers. It is in no layer.
- Alembic owns the schema. App startup never creates tables.

## Tests

- `tests/unit/`: no app, no database, no network. Marker `unit`.
- `tests/integration/<feature>/`: one folder per feature. Use the harness from `tests/helpers/harness.py` and the fixtures in `tests/conftest.py`.
- `tests/e2e/`: flows that cross features, over HTTP or against real servers.
- Shared helpers go in `tests/helpers/`. Fakes go in `tests/fakes/`.
- No test file imports another test file. Move shared code to `tests/helpers/` or `tests/fakes/`.
- A test that needs a real external service carries the `live` marker and skips itself without its `PLATFORM_*` variable.
- Assert behavior. Do not skip or weaken a test to make it pass.

## Engine contract

The Platform owns the HTTP contract between itself and the research engines.

- Wire models live in `src/management/research/contracts/`.
- After any change to them, run `uv run python scripts/export_contract.py` and commit the result in `docs/contract/`.
- `tests/unit/research/contracts/test_export_is_current.py` fails when `docs/contract/` is stale.
- An additive change raises the minor `contract_version`. A breaking change needs the user's decision first.
- Cross-repo rules: `../docs/superpowers/handoff/CONTRACT-RULES.md`.

## Settings and secrets

- Settings are in `src/management/core/config.py`. A new setting needs an entry in `.env.example` with a one-line comment and its code default.
- Never read or commit `.env`, `.env.local` or `docker/prod.env.local`. Use `.env.example` and `docker/prod.env.example`.
- Secrets stored in the database go through `SecretBox` (`src/management/infra`). Never log or return them.

## Git

Work on a feature branch. Commit there. Do not push unless the user asks.
