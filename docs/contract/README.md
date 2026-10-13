# Research engine HTTP contract

Contract version: **1.0.0** (semantic versioning). This directory is the public
contract; Python implementations belong to each service. Engines copy `schema/`,
`examples/`, and this README into `tests/contract/vendor/`, with the management
commit in `vendor/SOURCE`. No service imports code from another repository.

Regenerate with `uv run python scripts/export_contract.py`. Verify with
`uv run python scripts/export_contract.py --check` and
`uv run pytest tests/contracts -q`. Every wire model has a standalone JSON Schema
and an example. Every lifecycle event and one activity event has an example.
Examples contain placeholder credentials only. Replace them when calling a service.

Receivers ignore unknown fields, except secret-bearing models (`ModelConfig`,
`Models`, `RunCreateRequest`) and telemetry (`LlmCall`, `TelemetryRecord`,
`TelemetryBatch`), which reject them. Senders include all required fields.
Different minor versions remain compatible; a different major version is rejected.
Platform reports `ENGINE_CONTRACT_MISMATCH` for an incompatible engine.
Additive contract changes increment the minor version; breaking changes require
user approval and a major-version decision.

## Names and constants

`stage_key` is a pipeline stage: initially `hypothesis` or `experiment_plan`.
`phase` is a shared engine lifecycle state. `step_key` is an engine-specific step
declared in capabilities; Platform displays it without interpreting its science.

Phases: `drafting`, `reviewing`, `revising`, `awaiting_researcher`, `published`,
`failed`, `cancelled`, `paused`. Terminal phases are `published`, `failed`, `cancelled`.
Modes: `auto`, `light`, `copilot`, `full`. Model roles: `stage`, `review`.
Gate kinds: `question`, `artifact_review`. Event classes: `lifecycle`, `activity`.
Finding severity: `blocking`, `major`, `minor`; kind: `fix`, `needs_evidence`;
categories: `leakage`, `baseline`, `control`, `statistics`, `reproducibility`,
`evidence`, `other`.

Schema IDs: `i2h.hypotheses/v1`, `exd.exp_plan/v1`, `platform.dataset/v1`,
`exd.resources/v1`, `exd.experiment_index/v1`, `review.findings/v1`,
`revision.response/v1`, `revision.diff/v1`, `pipeline.feedback/v1`,
`pipeline.recommendation/v1`. Artifact versions are positive integers, distinct
from the major version in a schema ID. Artifacts are immutable.
Selection of hypotheses uses `{"hypothesis_ids": ["H1", "H2"]}`; values are each
hypothesis's `id`. The `hypothesis` stage receives
`inputs.params = {"topic": "12–2000 characters", "domains": []}` (at most six domains).
Engine restart failure code: `engine_restarted`.

## HTTP

An engine base URL includes its prefix (for example `http://i2h:8000/v1`). Append
these relative paths; never discard the base prefix. Return the model body directly,
without a Platform public-API response wrapper. All routes except public Health
require `X-Service-Key`. Requests also carry `X-Request-ID` and `traceparent`.

| Method | Relative path | Request / response |
|---|---|---|
| GET | `/health` | `Health`; without key omit `checks` and `last_error`; with valid key include available details |
| GET | `/capabilities` | `Capabilities` |
| POST | `/runs` | `RunCreateRequest` → `RunCreateResponse`; idempotent by `stage_run_id` |
| GET | `/runs/{engine_run_id}` | `RunState` |
| GET | `/runs/{engine_run_id}/events?after=0&limit=500` | `EventPage`; `after` ≥ 0, `limit` 1–500; empty page allowed |
| POST | `/runs/{engine_run_id}/gates/{gate_id}` | `GateAnswer` → current `RunState` |
| POST | `/runs/{engine_run_id}/pause` | current `RunState` |
| POST | `/runs/{engine_run_id}/resume` | current `RunState` |
| POST | `/runs/{engine_run_id}/cancel` | current `RunState` |

`callback_url` is Platform's full base for this stage run:
`{platform_base}{api_prefix}/internal/engines/{engine_key}/runs/{stage_run_id}`.
Append `/events` (`EventBatch` → `{accepted, skipped}`), `/artifacts`
(multipart `meta` JSON `ArtifactMeta` plus `file` bytes → 201 `{artifact_id}`), or
`/telemetry` (`TelemetryBatch`). Artifact input `download_url` is opaque; download
it using the service key. Platform's current route is
`/internal/engines/{engine_key}/artifacts/{artifact_id}`.

Error bodies contain `code` and `message`, without wrappers. Required codes:
401 `SERVICE_KEY_INVALID`; 404 `RUN_NOT_FOUND`; 409 `GATE_NOT_OPEN`, `RUN_CLOSED`,
`EVENT_GAP`, `STAGE_CLOSED`; 422 `VALIDATION_ERROR`, `GATE_OPTION_INVALID`,
`GATE_ACTION_INVALID`, `SHA256_MISMATCH`; 503 `ENGINE_UNAVAILABLE`.
`EVENT_GAP` additionally includes `delivered_seq`. Validation errors may include
`details`, but each entry contains only `loc`, `msg`, `type`: never echo submitted
values or validator contexts. Other service-specific codes must be documented
when added. Public Platform APIs retain their existing wrappers.

## Lifecycle and delivery

Draft → independent review → researcher review → publish. Researcher comments
always cause `revising` → `reviewing` → `awaiting_researcher`. Automatic revisions
are optional and declared by `auto_revision`. Review passes exactly when no finding
is `blocking`. Exhausted revisions or a reviewer failure leave a researcher gate;
they never publish automatically. A reviewer error retries once before that gate.
Finding IDs are engine-assigned and unique within a run (`F<round>-<n>`).

In `auto` and `light`, engines handle optional questions and artifact reviews
themselves, using declared defaults and recording an activity with `actor=system`.
They do not emit `run.gate_opened` for those optional gates. Every gate sent to
Platform requires a Platform/researcher answer. Platform decides primary-artifact
review in `auto`, and decides pipeline accept/send-back. Approve and accept are
separate actions. Send-back is only from a `COMPLETED` stage.

Lifecycle types and payloads:

| Type | Required envelope fields | Payload model |
|---|---|---|
| `run.phase_changed` | new `phase` | `PhaseChanged` (`from`: prior phase or null) |
| `run.step_changed` | new `step_key` | `StepChanged` (`from`: prior step or null) |
| `run.gate_opened` | `phase` | `Gate` |
| `run.gate_closed` | | `GateClosed` |
| `review.completed` | | `ReviewCompleted` |
| `artifact.delivered` | | `ArtifactDelivered` |
| `run.published` | `phase=published` | `RunPublished` (exact primary `artifact_id`) |
| `run.failed` | `phase=failed` | `FailureInfo` |
| `run.cancelled` | `phase=cancelled` | `{}` |
| `run.cost` | | `RunCost` (cumulative for one engine run) |

`source_seq` starts at 1 and increases continuously per engine run. Duplicate
callbacks are harmless. On `EVENT_GAP`, resend from `delivered_seq + 1`; do not
cancel. On `STAGE_CLOSED`, stop delivery and cancel local work. Upload the artifact
successfully before emitting any event that references its Platform ID, including
`review.completed`, `run.gate_opened`, and `run.published`; retry failed delivery.
Non-primary artifacts may be uploaded in any phase before publication.

Activity types use the engine prefix and must be declared in capabilities. Each
activity requires a nonempty `step_key`, `actor`, and one-sentence `summary`.
Do not include prompts, complete model responses, or credentials in activity or
telemetry. Telemetry uses `platform_run_id`, not `research_run_id`.

Only phase `reviewing` must use `models.review`. Other phases use `models.stage`,
although i2h's drafting debate judge may use the review model. The two models must
differ in `(provider, model)`. Credentials remain in memory; snapshots contain
provider/model metadata only. Engine restart fails every unfinished run with
`engine_restarted`, discards credentials, and requires researcher rerun. Platform
does not automatically rerun; `prior_engine_run_id` can reuse valid earlier work.

`RunCost.cost_usd=null` means Platform prices cumulative role-specific token usage.
If prices are unavailable, Platform marks `cost_unknown`; the USD budget cannot
be enforced. Engine `budget_usd` is informational. Platform pauses on budget
exhaustion and resumes the same engine run after a budget increase.

Platform timeline also has `class=platform`: `stage.status_changed`,
`iteration.started`, `decision.recorded`, `budget.updated`, `engine.health_changed`,
`attention.required`. Those events do not use the engine `EventEnvelope`.

## Mandatory engine behavior tests

Each engine implements these tests locally with fake model/integration calls and
a local FakePlatform. Names below are stable identifiers; each row states the
required assertions. Vendor examples must also parse with the engine's own models;
every outgoing event, artifact meta, state, capabilities, and Health must validate
against vendored JSON Schema. `test_vendor_matches_management` compares vendor
hashes with adjacent `management/docs/contract` when present, and skips only when
that directory is absent.

| Test | Required assertions |
|---|---|
| `test_capabilities_valid` | Response parses as Capabilities; engine key matches; same contract major; base profile present; exactly one primary output. |
| `test_runs_with_base_profile` | Request with empty profile reaches `awaiting_researcher` with a primary artifact review gate. |
| `test_health_liveness_without_key` | GET Health without a key returns 200 and required status/version/contract_version/active_runs; checks and last_error are omitted. A valid key exposes available details. |
| `test_runs_require_service_key` | POST runs without a service key returns 401. |
| `test_create_run_idempotent` | Two creates with the same stage_run_id return the same engine_run_id; second created is false; work is not duplicated. |
| `test_reaches_review_gate` | Poll state within harness timeout; phase is awaiting_researcher, waiting_input true, open gate kind artifact_review. |
| `test_lifecycle_events_ordered` | Callback source_seq is exactly 1..N; phase sequence starts drafting and contains reviewing before awaiting_researcher. |
| `test_activity_events_declared` | Every activity type appears in capabilities.activity_types and has nonempty step_key/actor/summary. |
| `test_review_uses_review_model` | At least one model call happens during reviewing; all such calls use models.review.model. Review model calls outside reviewing are permitted for the drafting judge. |
| `test_comment_returns_to_review_gate` | Send one researcher finding; subsequent phases include revising, then reviewing, then awaiting_researcher with a new review gate. |
| `test_approve_publishes_primary_artifact` | Approve reaches published; uploaded primary schema matches capabilities; run.published names that exact uploaded artifact ID. |
| `test_cancel` | Cancel an unfinished run; state is cancelled and run.cancelled is delivered. |
| `test_no_secret_leak` | A distinct test secret in both model keys and every integration appears in none of the callback bodies, state/events responses, captured logs, persisted checkpoints, or snapshots. |
| `test_telemetry_well_formed` | Every telemetry record validates; every reviewing LLM record has role review; prompts/responses/secret fields are rejected. |
| `test_restart_fails_open_runs` | Restart on the same persistence store: all unfinished runs fail with failure.code engine_restarted; terminal runs remain terminal; old credentials are discarded. Required once restart support lands in S4. |
| `test_pause_then_resume_continues` | Pause an unfinished run gives phase paused; resume continues the same run to the review gate with earlier progress preserved. |
| `test_error_body_shape` | Answer an unopened gate: 409 with code GATE_NOT_OPEN and message, without a wrapper. |
| `test_event_gap_triggers_resend_not_cancel` | FakePlatform returns EVENT_GAP with delivered_seq; engine retries from the next sequence, retains the run, and eventually delivers a contiguous timeline. |
| `test_repeated_gate_answer_is_idempotent` | Repeat the same normalized answer on a closed gate: 200, current RunState, no duplicated work. A different answer returns 409 GATE_NOT_OPEN. |
| `test_validation_error_does_not_echo_api_key` | Submit an invalid request containing a distinct secret: 422 body omits the secret; validation details contain only loc/msg/type. |
