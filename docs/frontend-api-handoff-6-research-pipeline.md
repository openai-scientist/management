# Research pipeline API: S1–S2

Updated 2026-10-14. This handoff covers the Platform implementation through M11,
on `refactor/feature-packages`. Engine contract: [1.1.0](contract/README.md).
The three-service live smoke is pending a configured local environment; the
automated flow uses FakeEngine. S3 auto decisions and send-back are not available yet.

## Requests and access

All paths below include the default `/api/v1` prefix. IDs are UUID strings unless
otherwise stated; a gate ID is the opaque string supplied in `open_gate`.
Use cookie credentials on every request. Mutations require `Origin` from the
configured allowlist and `X-CSRF-Token` from the current session. See
[README authentication](../README.md#authentication) for sign-in and token refresh.

Project members can read. Project Managers and Researchers can start runs, answer
stage gates and accept stages. Reviewers can read review history and artifacts.
An outsider gets 404; a member without contribution rights gets 403 `ROLE_REQUIRED`.
An archived project rejects mutations with 409 `PROJECT_ARCHIVED`.

Successful public responses use this envelope:

```json
{"success":true,"message":"OK","data":{},"meta":{"request_id":"request-id","pagination":null}}
```

Errors use `success:false`, `message`, `error:{code,details}` and `meta.request_id`.
Lists with pagination put `{total,limit,offset}` in `meta.pagination`. Run events
use `after`, a sequence cursor, rather than a page offset. Money is a decimal
string. Timestamps use ISO 8601. Unknown additional response fields can be ignored.

## Run and stage routes

| Method | Path | Request / result in `data` |
|---|---|---|
| POST | `/api/v1/projects/{project_id}/runs` | Start a research run; returns RunItem, HTTP 201. |
| GET | `/api/v1/projects/{project_id}/runs` | Existing paginated run list; `current_stage` is null here. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}` | RunItem with the latest `current_stage`. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/stages` | StageItem array; optional `iteration` integer ≥ 1. |
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/stages/{stage_run_id}/gates/{gate_id}` | Answer, approve or comment; returns `{gate_id,status:"applied"}`. |
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/stages/{stage_run_id}/accept` | `{selection,note}`; returns `{stage_run_id,status:"ACCEPTED",next_stage_run_id}`. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/stages/{stage_run_id}/reviews` | ReviewRoundItem array, described below. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/artifacts` | Artifact array; filters `stage_run_id`, `schema`, `version` (≥ 1). |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/artifacts/{artifact_id}/download` | Raw file bytes as an attachment, without a JSON envelope. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/events` | Paginated event array; `after` ≥ 0, `limit` 1–1000 (default 500), optional `stage_run_id`. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/events/stream` | SSE; optional `after` and `stage_run_id`, plus `Last-Event-ID` header. |

Start example:

```json
{"topic":"Compare sparse and dense retrieval on a public ML benchmark","domains":["ml"],"interaction_mode":"copilot","budget_usd":"10.00"}
```

`topic` is 12–2000 characters; `domains` must contain at least one nonempty value,
with at most six values. `budget_usd` is optional and uses the server default.
`interaction_mode` accepts `auto`, `light`, `copilot`, `full`. With configured
engines it overrides the legacy `review_mode`; otherwise `review_mode` retains
the legacy behavior. When omitted, mode comes from `review_mode` (`copilot` by
default). Start uses the configured default pipeline; there is no client-supplied
pipeline or profile field in this request yet. An empty `ENGINES` setting uses
the legacy Popper path. Check `pipeline_key` before showing stage actions.

RunItem retains the previous fields and adds `pipeline_key`, `iteration`,
`interaction_mode`, `current_stage`. A current stage has this shape:

```json
{"stage_run_id":"stage-uuid","stage_key":"hypothesis","engine_key":"i2h","status":"RUNNING","phase":"drafting","step_key":"literature_screen","waiting_input":true,"open_gate":{},"cost_usd":"0.0420"}
```

`open_gate` is the Gate object below or null. StageItem from the stages route also
contains `iteration`, `prior_stage_run_id`, `failure`, `created_at`, `finished_at`.
Configuration snapshots are private to the Platform and are not returned.

The stage status is a pipeline decision state: `PENDING`, `RUNNING`, `COMPLETED`,
`ACCEPTED`, `FAILED`, `STOPPED`, `SUPERSEDED`. `COMPLETED` means the engine published
and the researcher must accept. Engine phase describes the work within a stage:
`drafting`, `reviewing`, `revising`, `awaiting_researcher`, `paused`, `published`,
`failed`, `cancelled`. `step_key` is engine-specific and is display information.
The run can be `queued`, `running`, `paused`, `awaiting_review`, `completed`,
`budget_exceeded`, `failed`, `stopped`. A published stage waiting for acceptance
leaves its run in `awaiting_review`. Accepting the final stage completes the run.

## Gates, comments and acceptance

A question gate looks like this:

```json
{"gate_id":"opaque-gate-id","kind":"question","step_key":"scope","prompt":"Choose a scope","options":[{"option_id":"a","label":"Narrow"},{"option_id":"b","label":"Broad"}],"default_option_id":"a","artifact_ids":[],"opened_at":"2026-10-14T00:00:00Z"}
```

Submit `{"action":"answer","option_id":"b","text":"Optional note","params":{}}`.
The option must belong to this gate. The Platform rejects invalid options before
calling the engine, with 422 `GATE_OPTION_INVALID`. A question requires `answer`;
an artifact review accepts `approve` or `comment`, otherwise 422 `GATE_ACTION_INVALID`.

An artifact review gate has `kind:"artifact_review"`, `options:[]` and one or more
`artifact_ids`. Download those artifacts to display the review. Approve with
`{"action":"approve"}`. Optional `params` are forwarded to the engine for `answer`
or `approve`; they are not allowed for comments. A comment example:

```json
{"action":"comment","findings":[{"finding_id":"researcher-1","anchor":{"artifact_id":"artifact-uuid","locator":"hypothesis:H1/falsification_criteria"},"severity":"major","kind":"fix","category":"baseline","issue":"Clarify the baseline","suggestion":"Name its configuration","author":"researcher","evidence_refs":[]}]}
```

Send at least one finding. Every `anchor.artifact_id` must belong to this stage;
missing, malformed, nonexistent and foreign artifact IDs get 422 `VALIDATION_ERROR`.
The server overwrites finding authors with `researcher` and records the current
user as the decision maker. Clients cannot supply `actor` or `decided_by` on
the gate request. Comment causes the engine to revise and review again; wait for
the next gate rather than accepting the old draft. Finding severity, category
and anchor shape are defined in the [engine contract](contract/README.md).

Repeating the same normalized answer is idempotent. A different answer to an
already answered gate gets 409 `GATE_NOT_OPEN`. A temporary engine failure gets
503 `ENGINE_UNAVAILABLE`; the pending decision can be retried with the same body.

Approve and accept are separate actions. Wait for stage status `COMPLETED`
after approval, then accept. A hypothesis acceptance selects one or more IDs
that exist in the published `i2h.hypotheses/v1` artifact:

```json
{"selection":{"hypothesis_ids":["H1","H2"]},"note":"Use both comparisons"}
```

An empty or unknown hypothesis selection gets 422 `INVALID_SELECTION`. Accepting
before `COMPLETED` gets 409 `STAGE_STATE_CONFLICT`. A missing published artifact
gets 409 `ARTIFACT_NOT_READY`; an incompatible next-stage schema gets 409
`SCHEMA_MISMATCH`. The next engine receives the published artifact and selection.
For the last stage, send `{}`; `next_stage_run_id` is null on success.

## Review history and artifacts

The reviews response groups metadata for `review.findings/v1`,
`revision.response/v1` and `revision.diff/v1` by `review_round`, ascending:

```json
[{"review_round":1,"artifacts":[{"artifact_id":"findings-uuid","schema_id":"review.findings/v1","version":1}]},{"review_round":2,"artifacts":[{"artifact_id":"response-uuid","schema_id":"revision.response/v1","version":1},{"artifact_id":"diff-uuid","schema_id":"revision.diff/v1","version":1}]}]
```

All matching artifact versions are kept. A round may be incomplete while uploads
are in progress. No matching artifacts returns `[]`. Use each ID with the public
artifact download route to read the JSON or diff. The Platform indexes these
files; it does not reinterpret scientific findings. Artifacts without a
`review_round` are not part of this index.

Artifact list items retain `id`, `run_id`, `kind`, `filename`, `content_type`,
`size_bytes`, `sha256`, `created_at`, and include `stage_run_id`, `schema_id`,
`version`, `primary`, `review_round`. The `schema` query parameter filters
`schema_id`. Downloads are scoped to the run and project, returning 404 when absent.

## Timeline and attention

GET and SSE return the same event fields. An SSE frame is:

```text
event: run-event
id: 17
data: {"seq":17,"run_id":"run-uuid","ts":"2026-10-14T00:00:00Z","type":"i2h.screen.scored","stage_key":"hypothesis","stage_run_id":"stage-uuid","step_key":"literature_screen","iteration":1,"phase":"drafting","class":"activity","summary":"Scored two papers","refs":["paper-1"],"actor":"reader","payload":{}}

```

`class` is `lifecycle`, `activity` or `platform`. Activity events supply a summary,
actor and engine step. `refs` is a list of opaque references. Legacy events can
have no stage run or phase. Migrated legacy UI stage names live in `step_key`;
new pipeline events use `stage_key` for the pipeline stage only.

Sequence IDs belong to the entire research run. Filtering by stage creates gaps;
do not renumber or expect adjacent IDs. Use the largest received `seq` as `after`.
`Last-Event-ID` takes precedence over `after` on SSE reconnect. The stream first
replays history, then delivers live events; it emits `retry: 3000` and keep-alive
comments. Deduplicate replayed events by `(run_id,seq)`.
`run-ended` carries `{status}` for `completed`, `failed`, `stopped` or
`budget_exceeded`; reconnect after a resumable budget stop. `session-ended`
carries an error code and means close the connection. Browser EventSource sends
its cookie with `withCredentials:true`.

Engine lifecycle events include `run.phase_changed`, `run.step_changed`,
`run.gate_opened`, `run.gate_closed`, `review.completed`, `artifact.delivered`,
`run.cost`, `run.published`, `run.failed`, `run.cancelled`. Terminal events are
sufficient to close a stage; do not wait for a terminal `run.phase_changed`.
Platform events used in S1–S2 are `stage.status_changed`, `decision.recorded`,
`attention.required`. Names reserved for later slices include
`iteration.started`, `budget.updated`, `engine.health_changed`.

For manual modes, `attention.required` is emitted when a gate opens or a stage
becomes `COMPLETED` or `FAILED`. Its payload has `stage_run_id` and nullable
`gate_id`. A phase change to `awaiting_researcher` alone does not emit attention.
The Platform sends existing `run_awaiting_review` notifications to active Project
Managers and Researchers, with project/run IDs; Reviewers are excluded. Display
these notices as “research needs attention”, including failed stages. Duplicate
callback delivery does not duplicate notifications. S2 suppresses these notices
in `auto`; S3 will add explicit cases where auto stops for human intervention.

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/notifications` | Existing notification list, including `run_awaiting_review`. |
| GET | `/api/v1/notifications/stream` | Existing `notifications` SSE snapshots; replace the list on each snapshot. |

## Administrator setup

These existing S1 routes require Platform Admin; writes also require CSRF.

| Method | Path | Request / result |
|---|---|---|
| GET | `/api/v1/admin/models` | `{stage:ModelProfileOut|null,review:ModelProfileOut|null}`. |
| PUT | `/api/v1/admin/models/{role}` | `role` is `stage` or `review`; model settings below. |
| GET | `/api/v1/admin/integrations` | Array of `{name,configured,updated_at}`. |
| PUT | `/api/v1/admin/integrations/{name}` | `{value:"credential"}`; returns metadata. |
| DELETE | `/api/v1/admin/integrations/{name}` | Deletes that integration. |

Model input: `provider` (`openai_compatible` or `bedrock`), `model`, optional
`base_url`, `api_key`, `extra`, `input_usd_per_mtok`, `output_usd_per_mtok`.
The review model must differ from the stage model by `(provider,model)`, otherwise
422 `REVIEW_MODEL_SAME_AS_STAGE`. A blank or omitted key preserves the saved key.
Responses include `has_api_key` and configuration metadata, never the saved key.
Before start, missing models get 409 `MODELS_NOT_CONFIGURED`, unsupported provider
422 `PROVIDER_NOT_SUPPORTED`, missing required integration 422 `INTEGRATION_MISSING`,
unavailable engine 503 `ENGINE_UNAVAILABLE`, incompatible contract major
409 `ENGINE_CONTRACT_MISMATCH`. Keep credential values out of client logs and URLs.

## Legacy route migration and G3 removal

Legacy routes are still registered through S2. Use stage routes for pipeline
runs. The following table lists existing old paths; replacements that require
S3 or later are described in prose and are not promised as registered S2 routes.

| Existing method | Existing path | Replacement / G3 action |
|---|---|---|
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/gates/{gate_id}` | Use the stage gate route above for pipeline runs; remove this legacy route at G3. |
| GET, POST | `/api/v1/projects/{project_id}/runs/{run_id}/frame-review` | Use artifact review gates; remove this route at G3. |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/frame-reviews` | Use stage review history; remove this route at G3. |
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/sync` | Background reconciliation arrives in S4; remove explicit sync at G3. |
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/abandon` | Explicit pipeline stop arrives in S3; remove abandon at G3. |
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/pause` | Existing legacy action; pipeline pause/resume support arrives in S3. |
| POST | `/api/v1/projects/{project_id}/runs/{run_id}/resume` | Existing legacy action; pipeline pause/resume support arrives in S3. |

Send-back, rerun, stop, interaction-mode changes and budget updates are later
slice work. Do not show those controls based solely on this handoff.
