# Management test feedback audit

Measured on 2026-10-15 at baseline `a9fce9d68e95b08cbf97eb331269cad39d0f8f56`.
The working tree was initially clean. No existing tests or production behavior were removed or changed.

## Commands and coverage

Use `uv run` from the repository root:

| Stage | Command | Coverage |
|---|---|---|
| One edit | `uv run pytest tests/unit/connections/test_connector_values.py -q` | Explicit file only |
| Feature edit | `uv run pytest tests/integration/research -q` | Explicit owner only |
| Fast local lane | `uv run pytest tests/unit -q` | All unit owners; scoped evidence |
| Safe working changes | `uv run python scripts/affected_tests.py --local --run` | Staged, unstaged, untracked relevant paths (including ignored local source) |
| Pre-push / PR | `uv run python scripts/affected_tests.py --base origin/main --run` | Whole merge-base diff plus working changes |
| Preview selection | `uv run python scripts/affected_tests.py --base origin/main` | JSON, no pytest |
| Full handoff | `uv run pytest -q` | Entire suite, followed by lint, format, import rules |

Choose the real PR target instead of `origin/main` when different. Fetch that target before pre-push;
missing history selects everything. Git `--no-renames` includes both old and new paths; deleted
paths still select their owners. Ignored untracked files under `src`, `tests`, `scripts`, `alembic` and `docs/contract` also participate. Explicit exclusions are cache/generated directories (`.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `dist`, `build`, `.worktrees`, `.kilo`, `runs`, `outputs`), `.env*`, `.pyc` and `.log` files; secrets are never read. Unknown paths,
missing Git/base information and deleted owner directories run the full suite. An invalid saved
plan fails, rather than silently skipping tests. Pytest's exit code propagates.

These commands do not make a source PR automatically faster: **every `src/` change currently
selects the full suite**. Scoped edit commands deliberately trade breadth for fast iteration;
use the conservative affected command and full final handoff checks before treating work as complete.

## Shared selection and CI

`scripts/affected-lanes.json` is the only lane map. `scripts/affected_tests.py` reads it for local,
pre-push and CI. Fifteen lanes cover unit connections/infra/research, architecture, selector tooling,
integration audit/connections/datasets/db/identity/monitoring/notifications/projects/research,
and e2e. The map includes each lane's tests and its owner-local support files.

All source is shared because `tests/conftest.py:9` imports `tests.helpers.harness`, which imports
`management.main`; main imports and mounts every feature router. `create_app` also initializes
all infrastructure. `db/models_registry.py` imports every feature's models, and feature imports
form cycles (including projects, datasets and research). Dropping these edges to claim narrower
source coverage would miss app composition and schema failures. No new graph dependency is needed
for this conservative map. Shared helpers/fakes/fixtures remain shared even when only some tests
explicitly reference them.

Shared prefixes: `src/`, `alembic/`, `docker/`, `.github/`, `scripts/`, `tests/helpers/`,
`tests/fakes/`, `docs/contract/`. Shared files: `uv.lock`, `pyproject.toml`, `alembic.ini`,
`.python-version`, `tests/conftest.py`, `tests/__init__.py`, `tests/unit/__init__.py`,
`tests/integration/__init__.py`. Everything else unknown also selects all lanes.
The explicit skip list is only `README.md`, `AGENTS.md`, `docs/test-audit.md`;
generated contract documents are test inputs and never skipped.

CI keeps the existing `test` job and all PostgreSQL environment/service settings, migrations,
format/lint/import rules and Docker build. Selection writes an immutable plan in runner temp;
execution reads exactly that plan, without a second Git diff. PR selection uses the merge base
with `origin/$TARGET_BRANCH`, fetched with full history. Main/release branch pushes, tags,
merge groups, published releases, daily schedule and manual runs always select the full suite.
Docs-only plans skip pytest while the other checks still run. There are no duplicate service
setups or per-owner runner installations.

The new always-running `gate` depends on `test`, failing failed/cancelled/missing/invalid results,
including selection errors; success and skipped results are accepted. The existing `test` job ID
is retained. If branch protection is later changed, require `gate`; hosted protection was not
changed by this audit. No live coverage was moved off PRs.

## Baseline and measured feedback

These local timings were measured while three repository audits shared the Windows machine;
contention makes before/after full timings unsuitable as an isolated speed benchmark.

| Measurement | Result |
|---|---|
| Full baseline | 1,088 passed, 92 skipped; 1,180 collected; 455.08 seconds |
| Full final run | 1,115 passed, 92 skipped; 1,207 collected (27 new tooling cases); 392.22 seconds |
| Unit baseline | 266 passed, 5 skipped; 13.72 seconds |
| Collection only | 1,180 collected in 5.32 seconds |
| Selected unit-connections run | 146 passed, 5 skipped; 2.72 seconds |
| Initial selector regression checks | 27 passed; 0.61 seconds |
| Final selector regression checks | 28 passed; 1.01 seconds (includes ignored source safety) |
| Baseline test inventory | 79 files; 632 declarations; 22,978 lines |
| Baseline test support | 32 files; 2,772 lines |
| Current test inventory | 80 files; 644 declarations; 23,140 lines |
| Current test support | 32 files; 2,772 lines (unchanged) |
| Tests retired / consolidated / weakened | 0 / 0 / 0 |

The measured useful win is the feedback loop: a known connections unit edit can get scoped
feedback in 2.72 seconds instead of repeatedly waiting 455.08 seconds for every suite. This is
less coverage and is explicitly labeled as such. Test-only PRs can safely select their mapped
owners. Source PRs retain full coverage and have no claimed automatic suite-time reduction.

Historical CI evidence from [run 38591226782](https://github.com/openai-scientist/management/actions/runs/38591226782)
on 2026-10-14: test job 231 seconds, pytest 185, container initialization 14, install 2,
migrations 4, Docker build 15 (measured by parent via `gh run view 38591226782 --json jobs`).
This is historical Linux CI evidence, separate from the current local measurements. This changed
workflow has not been run on hosted Actions, so no new CI wall-time claim is made.

Largest baseline call: `test_following_a_metric_is_on_record_once_in_ten_minutes` (11.49 seconds).
BigQuery timeout proof was 3.08 seconds; other API calls and app setup repeatedly cost 1-3 seconds.
Harness creates all tables for every test (`tests/helpers/harness.py:97`); that isolation is
retained. Unit R2 emulator setup totaled about 4.2 seconds and contract export proofs 3.55 seconds.
Intentional timeout/stream waits protect cancellation, readiness and no-event contracts; reducing
them without equivalent proof risks flaky coverage. No schema fixture scope or timeout tuning
was made. Further source PR acceleration needs a separate, behavior-proven harness isolation design.

## Retention ledger and prior pruning

Read prior audit history at `../docs/superpowers/reports/2026-10-14-management-test-audit.md`.
That audit already pruned duplicates and fake-computed assertions. This audit concentrates on
feedback selection, not an exhaustive second assertion-by-assertion pruning campaign. Doubtful
cases remain. History for the retained candidates includes `fc48ebc` (test layout migration).
No seam became unused and no production code was removed.

| Candidate / mark | Actual contract and evidence | Keeper / callers / risk / validation |
|---|---|---|
| `unit/test_architecture.py::test_import_contracts_hold` R | Independent import-layer boundary; catches forbidden service/router imports | Existing import-linter contracts are used by CI and local command; test keeps full pytest useful; retain despite repeated CI check; `uv run pytest tests/unit/test_architecture.py -q` |
| `unit/research/contracts/test_isolation.py::test_contract_source_has_no_platform_dependency` R | Standalone engine wire models must not import platform package | Cross-repo contract consumers; no stronger standalone guard established; static test retained; `uv run pytest tests/unit/research/contracts -q` |
| `unit/research/contracts/test_export_is_current.py::test_export_is_current` R | Checked-in JSON schema/examples match wire models | Export script is used by developers and cross-repo vendoring; generated files are runtime test inputs; `uv run pytest tests/unit/research/contracts -q` |
| `unit/research/contracts/test_export_is_current.py::test_every_wire_model_and_event_has_valid_example` R | Every example validates against model and JSON schema | Export-current alone does not establish schema validity; retain distinct proof and avoid weakening wire contract; same command |
| `unit/research/contracts/test_service.py::test_paths_format` R | External engine gate URL spelling and formatting | Wire path consumed by engines; literal expected path is independent interface proof; same command |
| `unit/connections/test_connector_values.py::test_a_value_becomes_the_text_stored_for_it` R | External driver values preserve stored wire text, booleans and decimals | Connector preview/import share `to_text`; boolean mutation caught two rows; `uv run pytest tests/unit/connections -q` |
| `unit/infra/test_r2_file_store.py` R | Immutability, checksum, key guards and backend error behavior over fake S3 | Public file-store seam is used by app and datasets; emulator setup cost is not duplication evidence; `uv run pytest tests/unit/infra -q` |

## Selection verification and mutation

Automated checks in `tests/unit/test_affected_tests.py` prove owner-only selection, two rename ends,
deletions, shared source/helpers/lock/CI/map/contracts, unknown paths, docs-only skips, Git failures,
merge-base whole diff plus staged/unstaged/untracked paths, saved-plan execution and failure
propagation, invalid-plan rejection, deleted owner fallback, complete test-file owner mapping and
success/skipped/failure/cancelled/missing gate results, plus ignored executable source discovery with cache/secret exclusions. A real temporary Git index checks staged,
renamed, deleted and untracked paths without any commit. Remaining Git protocol cases are mocked
at the command boundary, checking `--no-renames` and merge-base arguments.

Observed CLI cases:

- `uv run python scripts/affected_tests.py --local`: ALL (this change modifies CI/shared scripts).
- `uv run python scripts/affected_tests.py --base HEAD`: ALL, including working changes.
- `uv run python scripts/affected_tests.py --base missing-audit-target`: ALL, explicit unavailable-diff reason.
- `uv run python scripts/affected_tests.py --paths README.md --run`: no lanes, pytest skipped, exit 0.
- `uv run python scripts/affected_tests.py --paths tests/unit/connections/test_connector_values.py --run`: unit-connections only, 146 passed / 5 skipped in 2.72 seconds.

Mutation: swapped boolean `true`/`false` in `src/management/connections/connectors/values.py`.
`uv run python scripts/affected_tests.py --paths src/management/connections/connectors/values.py --run -- -k test_a_value_becomes_the_text_stored_for_it`
selected ALL, with an explicit targeted pytest filter for the mutation proof: 2 failed, 31 passed,
1,174 deselected in 5.70 seconds; selector propagated exit 1. The mutation was restored byte for
byte in `finally`; `git diff --exit-code -- src/management/connections/connectors/values.py` passed.
No claim that this filtered mutation command was a full-suite run.

## Exact verification and limitations

Baseline commands:

```powershell
uv run pytest -q --durations=30 --junitxml=$env:TEMP/management-audit-baseline.xml
uv run pytest tests/unit -q --durations=15 --junitxml=$env:TEMP/management-audit-unit.xml
uv run pytest --collect-only -q
```

Final commands:

```powershell
uv run pytest tests/unit/test_affected_tests.py -q
uv run pytest -q --durations=30 --junitxml=$env:TEMP/management-audit-final.xml
uv run ruff check src alembic tests scripts
uv run ruff format --check src alembic tests scripts
uv run lint-imports
```

Focused current selector: 28 passed. The full final run collected the original 27 tooling cases; a subsequent isolated ignored-source discovery addition was verified by the focused 28-case rerun. No production behavior changed during that run. Lint passed; format: 286 files formatted; import rules: 2 kept,
0 broken (142 modules / 577 dependencies). Full final: 1,115 passed, 92 skipped in 392.22 seconds; all 80 test files collected at run start passed or skipped as expected. The current tree has one additional selector regression case (1,208 cases total), checked by the focused 28-case rerun. The observed 455.08-to-392.22-second full-run change is timing variation under contention, not an attributed suite speedup.
Baseline had no failures. The 92 local skips are 47 external-service cases (27 PostgreSQL, 9 MySQL, 2 BigQuery and 9 other external service cases) and 45 uvloop cases
unsupported on Windows, measured directly from current JUnit skip reasons (the prior report described older counts); no real services were started or contacted by this
audit. PostgreSQL invariant coverage remains enabled in Linux CI. Hosted workflow, Docker build,
real PostgreSQL migrations, real-service tests and uvloop were not run locally.

One initial mutation command failed at Python parsing due to PowerShell quoting, before changing
source; the corrected command produced the caught failures above. Initial selector TDD collection
failed because the new script did not yet exist, then passed after implementation. Initial focused
lint found two long argument declarations; formatting corrected them and the final checks passed.
Raw JUnit/log output stays in the OS temporary directory, outside the repository.

Search scope: `E:/Working/capstone/management`, relevant Python source/tests/scripts/migrations,
JSON lane/contract inputs, CI YAML and instruction/documentation files. Discovery used `rg` and
`rg --files --hidden --no-ignore-vcs`, including ignored/hidden relevant sources while respecting
`.ignore`/`.rgignore`, with explicit exclusions `.git`, `node_modules`, `.venv`, `__pycache__`,
`dist`, `.worktrees`, `.kilo`, pytest/ruff caches, unrelated runs/outputs and `.env*` secret files.
A Python-only manifest over `src tests scripts alembic` verified Python coverage. The initial broad
manifest included cache paths because later include globs overrode exclusions; the scoped Python
manifest corrected that coverage issue. Sibling repositories were not changed; only parent-provided
handoff/history files were read. No secrets were read. Searches and retained-candidate review are
not represented as an exhaustive junk audit.

## Owner lane baseline execution

These sums include JUnit setup/call/teardown and exclude shared process/collection overhead; they are not hosted CI wall times. Each lane command is `uv run pytest -q <test path>` using the exact path in the shared JSON map. Tooling was added after baseline.

| Lane | Test path | Cases | Baseline seconds |
|---|---|---:|---:|
| architecture | `tests/unit/test_architecture.py` | 1 | 0.824 |
| e2e | `tests/e2e` | 14 | 7.283 |
| integration-audit | `tests/integration/audit` | 1 | 0.154 |
| integration-connections | `tests/integration/connections` | 596 | 191.006 |
| integration-datasets | `tests/integration/datasets` | 28 | 26.344 |
| integration-db | `tests/integration/db` | 20 | 0.182 |
| integration-identity | `tests/integration/identity` | 103 | 44.767 |
| integration-monitoring | `tests/integration/monitoring` | 19 | 13.810 |
| integration-notifications | `tests/integration/notifications` | 18 | 22.068 |
| integration-projects | `tests/integration/projects` | 31 | 32.636 |
| integration-research | `tests/integration/research` | 79 | 97.554 |
| tooling | `tests/unit/test_affected_tests.py` | added | new |
| unit-connections | `tests/unit/connections` | 151 | 2.496 |
| unit-infra | `tests/unit/infra` | 23 | 6.585 |
| unit-research | `tests/unit/research` | 96 | 2.034 |

## Per-file baseline

Measured JUnit totals include call/setup/teardown. Rows cover all 79 baseline test files.

| Test file | Cases | Skipped | Failed | Seconds |
|---|---:|---:|---:|---:|
| `tests/e2e/test_live_connectors.py` | 10 | 10 | 0 | 0.010 |
| `tests/e2e/test_notification_coverage.py` | 3 | 0 | 0 | 4.863 |
| `tests/e2e/test_notification_stream_http.py` | 1 | 0 | 0 | 2.410 |
| `tests/integration/audit/test_audit_order.py` | 1 | 0 | 0 | 0.154 |
| `tests/integration/connections/test_bigquery_connector.py` | 61 | 6 | 0 | 9.910 |
| `tests/integration/connections/test_connection_browse_api.py` | 9 | 0 | 0 | 8.605 |
| `tests/integration/connections/test_connection_live_api.py` | 26 | 0 | 0 | 44.582 |
| `tests/integration/connections/test_connections_api.py` | 16 | 0 | 0 | 13.127 |
| `tests/integration/connections/test_google_connection_grant.py` | 37 | 0 | 0 | 10.789 |
| `tests/integration/connections/test_google_drive_connection_api.py` | 5 | 0 | 0 | 6.244 |
| `tests/integration/connections/test_google_drive_connector.py` | 53 | 0 | 0 | 1.299 |
| `tests/integration/connections/test_google_picker_api.py` | 4 | 0 | 0 | 3.194 |
| `tests/integration/connections/test_google_sheets_connection_api.py` | 7 | 0 | 0 | 7.824 |
| `tests/integration/connections/test_google_sheets_connector.py` | 45 | 0 | 0 | 0.399 |
| `tests/integration/connections/test_http_source.py` | 42 | 0 | 0 | 4.624 |
| `tests/integration/connections/test_influxdb_connection_api.py` | 28 | 0 | 0 | 24.503 |
| `tests/integration/connections/test_influxdb_connector.py` | 88 | 0 | 0 | 10.310 |
| `tests/integration/connections/test_mysql_connector.py` | 46 | 31 | 0 | 6.787 |
| `tests/integration/connections/test_postgres_connector.py` | 26 | 20 | 0 | 4.119 |
| `tests/integration/connections/test_prometheus_connection_api.py` | 31 | 0 | 0 | 25.089 |
| `tests/integration/connections/test_prometheus_connector.py` | 72 | 0 | 0 | 9.601 |
| `tests/integration/datasets/test_dataset_imports_api.py` | 22 | 0 | 0 | 20.459 |
| `tests/integration/datasets/test_datasets_api.py` | 6 | 0 | 0 | 5.885 |
| `tests/integration/db/test_postgres_invariants.py` | 20 | 20 | 0 | 0.182 |
| `tests/integration/identity/test_account_invite.py` | 6 | 0 | 0 | 2.916 |
| `tests/integration/identity/test_auth_and_health.py` | 13 | 0 | 0 | 9.612 |
| `tests/integration/identity/test_avatar_api.py` | 5 | 0 | 0 | 4.232 |
| `tests/integration/identity/test_email_sender.py` | 4 | 0 | 0 | 0.097 |
| `tests/integration/identity/test_email_verification.py` | 11 | 0 | 0 | 5.483 |
| `tests/integration/identity/test_google_sign_in.py` | 42 | 0 | 0 | 11.092 |
| `tests/integration/identity/test_one_time_codes.py` | 5 | 0 | 0 | 1.156 |
| `tests/integration/identity/test_password_reset.py` | 10 | 0 | 0 | 5.110 |
| `tests/integration/identity/test_user_profile_api.py` | 7 | 0 | 0 | 5.069 |
| `tests/integration/monitoring/test_admin_log_monitoring_api.py` | 4 | 0 | 0 | 4.245 |
| `tests/integration/monitoring/test_admin_log_monitoring_stream.py` | 2 | 0 | 0 | 0.374 |
| `tests/integration/monitoring/test_admin_overview_api.py` | 3 | 0 | 0 | 2.460 |
| `tests/integration/monitoring/test_admin_usage_api.py` | 2 | 0 | 0 | 2.859 |
| `tests/integration/monitoring/test_monitoring_alerts.py` | 1 | 0 | 0 | 0.271 |
| `tests/integration/monitoring/test_monitoring_event_writer.py` | 7 | 0 | 0 | 3.601 |
| `tests/integration/notifications/test_comments_and_notifications_api.py` | 3 | 0 | 0 | 5.984 |
| `tests/integration/notifications/test_notification_stream.py` | 15 | 0 | 0 | 16.084 |
| `tests/integration/projects/test_invite_candidates_stream.py` | 9 | 0 | 0 | 5.758 |
| `tests/integration/projects/test_list_search_and_filters.py` | 1 | 0 | 0 | 2.998 |
| `tests/integration/projects/test_project_files_api.py` | 4 | 0 | 0 | 4.412 |
| `tests/integration/projects/test_project_invitations.py` | 8 | 0 | 0 | 9.826 |
| `tests/integration/projects/test_project_invite_candidates.py` | 2 | 0 | 0 | 2.455 |
| `tests/integration/projects/test_projects_api.py` | 7 | 0 | 0 | 7.187 |
| `tests/integration/research/test_admin_models_api.py` | 6 | 0 | 0 | 6.222 |
| `tests/integration/research/test_internal_engines_api.py` | 6 | 0 | 0 | 6.878 |
| `tests/integration/research/test_orchestrator_flow.py` | 5 | 0 | 0 | 3.573 |
| `tests/integration/research/test_researcher_interactions.py` | 15 | 0 | 0 | 22.338 |
| `tests/integration/research/test_run_events_api.py` | 4 | 0 | 0 | 5.775 |
| `tests/integration/research/test_run_stages_api.py` | 5 | 0 | 0 | 8.185 |
| `tests/integration/research/test_run_timeline.py` | 7 | 0 | 0 | 9.262 |
| `tests/integration/research/test_runs_api.py` | 10 | 0 | 0 | 16.382 |
| `tests/integration/research/test_smoke_pipeline.py` | 3 | 0 | 0 | 5.617 |
| `tests/integration/research/test_stage_models.py` | 13 | 0 | 0 | 8.321 |
| `tests/integration/research/test_start_stage_callback_race.py` | 1 | 0 | 0 | 1.219 |
| `tests/integration/research/test_terminal_events.py` | 3 | 0 | 0 | 3.749 |
| `tests/integration/research/test_timeline_migration.py` | 1 | 0 | 0 | 0.033 |
| `tests/unit/connections/test_connection_gate.py` | 9 | 0 | 0 | 0.044 |
| `tests/unit/connections/test_connector_values.py` | 34 | 0 | 0 | 0.067 |
| `tests/unit/connections/test_csv_export.py` | 7 | 0 | 0 | 0.345 |
| `tests/unit/connections/test_network_guard.py` | 32 | 0 | 0 | 0.179 |
| `tests/unit/connections/test_threaded_call.py` | 10 | 5 | 0 | 1.744 |
| `tests/unit/connections/test_timeseries_source.py` | 59 | 0 | 0 | 0.117 |
| `tests/unit/infra/test_file_store.py` | 14 | 0 | 0 | 0.202 |
| `tests/unit/infra/test_r2_file_store.py` | 7 | 0 | 0 | 6.310 |
| `tests/unit/infra/test_secret_box.py` | 2 | 0 | 0 | 0.073 |
| `tests/unit/research/contracts/test_base.py` | 13 | 0 | 0 | 0.021 |
| `tests/unit/research/contracts/test_export_is_current.py` | 2 | 0 | 0 | 1.594 |
| `tests/unit/research/contracts/test_isolation.py` | 1 | 0 | 0 | 0.005 |
| `tests/unit/research/contracts/test_review_events.py` | 32 | 0 | 0 | 0.063 |
| `tests/unit/research/contracts/test_runs.py` | 23 | 0 | 0 | 0.053 |
| `tests/unit/research/contracts/test_service.py` | 7 | 0 | 0 | 0.018 |
| `tests/unit/research/test_engine_client.py` | 6 | 0 | 0 | 0.147 |
| `tests/unit/research/test_engine_settings.py` | 8 | 0 | 0 | 0.091 |
| `tests/unit/research/test_popper_client.py` | 4 | 0 | 0 | 0.042 |
| `tests/unit/test_architecture.py` | 1 | 0 | 0 | 0.824 |
