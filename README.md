# AI Research Platform API

The Platform API of the AI Research Experimentation Platform. It is the backend that a web frontend talks to.

## Purpose and scope

The Platform handles sign-in, projects, who may do what, the inputs of a research run, the review steps, result files, comments, notifications and the audit log.

The research itself is done by separate **engine** services. The Platform sends them the inputs of a run, shows progress, records who reviewed what and keeps a copy of the results. It stores metadata and files only. It never reads or judges scientific content.

The Platform does not run experiments, host a frontend, or manage organizations. The project is the top-level scope.

## Features

- Accounts: passwordless sign-in with an emailed 6-digit code or link (the first one creates the account), optional Google sign-in, cookie sessions with CSRF protection, profile pictures, accounts created by an admin.
- Projects: five fixed roles, invitations by email, archive and restore. There is no hard delete.
- Research inputs: versioned CSV datasets, uploaded or imported through a data connection (PostgreSQL, MySQL, BigQuery, Google Sheets, Google Drive, Prometheus, InfluxDB). Project files, a topic and domains.
- Runs: one active run per project, staged through engines, with a spending cap, gates for human review and result files.
- Collaboration: comments, notifications over SSE, an audit log of every change.
- Administration: admin overview, redacted request events, usage, error-rate alerts.

## Tech stack

| Area | Choice |
|---|---|
| API | Python 3.12+, FastAPI, Uvicorn |
| Database | PostgreSQL 16, SQLAlchemy (async, `asyncpg`), Alembic |
| Files | A local directory or a Cloudflare R2 bucket |
| Email | Resend |
| External data | `asyncpg`, PyMySQL, `google-cloud-bigquery` |
| Tooling | `uv`, Ruff, pytest, import-linter, Docker Compose, Task |

## Quick start

You need Python 3.12+, `uv`, Docker with Compose, and [Task](https://taskfile.dev/docs/installation).

```bash
cp .env.example .env.local
task up        # PostgreSQL + API on 127.0.0.1:8080
task migrate   # on a fresh database
```

The API runs at `http://localhost:8080`. Swagger UI is at `/docs`, the schema at `/openapi.json`. Liveness and readiness are `/api/v1/health/live` and `/api/v1/health/ready`.

**First Platform Admin.** The local stack creates one from `DEFAULT_ADMIN_EMAIL` (see `.env.example`) once migrations are applied: run `task migrate`, then `task restart`. Sign in as that address with an emailed code; without `RESEND_API_KEY` the email, code and link included, is in the API log. Staging and production refuse this setting.

Without it, sign in once with the address you want, then promote it:

```bash
docker compose --env-file .env.local -f docker/docker-compose.dev.yml run --rm api python scripts/bootstrap_admin.py --email admin@example.com
```

The script refuses to run when a Platform Admin exists. It rejects an address that never signed in, or a suspended account. The Docker image ships `scripts/`.

`task --list` shows all shortcuts: `up`, `down`, `restart`, `status`, `logs`, `migrate`, and the `prod:*` set.

## Project structure

```text
src/management/
  main.py          FastAPI app; mounts every router
  core/ db/ infra/ settings, errors, security, sessions | engine, session, model registry | file stores, email, SecretBox
  audit/ access/   audit events | project roles and membership checks
  identity/        sign-in, Google sign-in, users, avatars, one-time codes
  notifications/   notifications and their stream
  projects/        projects, invitations, comments, project files
  connections/     data connections and their connectors
  datasets/        datasets, versions, imports
  monitoring/      admin overview, usage, log monitoring
  research/        runs, stages, gates, engine clients, contracts/
alembic/  scripts/  docker/  tests/  docs/contract/  Taskfile.yml
```

Each feature is a package `src/management/<feature>/` with `router`, `schemas`, `service` and `models`. Layers, high to low:

`research | monitoring` > `datasets` > `connections` > `projects` > `identity | notifications` > `audit | access` > `core | db | infra`

A module imports only from lower layers. No module imports another feature's router. Model modules are shared. `uv run lint-imports` checks this. `docs/contract/` holds the generated engine contract. `AGENTS.md` lists the working rules.

## Development and tests

```bash
uv sync --all-groups
uv run pytest tests/unit -q        # fast lane: no app, DB or network
uv run pytest -q                   # full suite, SQLite in memory
uv run pytest -m live              # real services; needs PLATFORM_* variables
uv run ruff check src alembic tests scripts
uv run ruff format --check src alembic tests scripts
uv run lint-imports
uv run python -m alembic upgrade head
```

Alembic owns the schema. App startup never creates or migrates tables. Never point a developer command at a production database.

Tests marked `live` skip themselves unless their variable is set:

| Variable | Turns on |
|---|---|
| `PLATFORM_POSTGRES_TEST_URL` | Real PostgreSQL behavior (locks, concurrency), against a scratch database |
| `PLATFORM_MYSQL_TEST_URL` | MySQL connector against a real server (user may create databases and users) |
| `PLATFORM_BIGQUERY_TEST_SERVICE_ACCOUNT` | Path of a service account key file; BigQuery itself |
| `PLATFORM_LIVE_CONNECTOR_TESTS=1` | Flows through public databases; with `PLATFORM_LIVE_RNACENTRAL_PASSWORD` for RNAcentral |
| `PLATFORM_LIVE_GOOGLE_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`, `_SPREADSHEET`, `_FOLDER` | With the line above: Google Sheets and Drive. The refresh token needs the `drive.readonly` scope |
| `PLATFORM_LIVE_PROMETHEUS_URL` (`_USERNAME`, `_TOKEN`) | With the same line: a Prometheus server |
| `PLATFORM_LIVE_INFLUXDB_URL`, `_DATABASE` (`_TOKEN`, `_PRIVATE=1`) | With the same line: an InfluxDB server; `_PRIVATE=1` allows a private host for that one test |

The live flows are in `tests/e2e/test_live_connectors.py`. CI never runs them. Keep refresh tokens and passwords out of committed files and shell history.

## How the Platform works

```text
Sign in with an emailed code
  -> create a project               the creator is its Project Manager
  -> invite members by email        role: project_manager, researcher or reviewer; accept within 24 hours
  -> add data (optional)            upload a CSV, or import through a data connection
  -> start a run                    topic, domains, optional budget
  -> engines work, stage by stage   the run asks for review at gates
  -> a Project Manager or Researcher answers each gate
  -> engines deliver result files   members read, download and comment
```

**Projects.** Project status is derived, never set by hand, except `completed`: `draft` (no dataset), `data_ready`, `researching` (a run is active), `needs_review`, `completed` (set by a Project Manager, can be reopened). `archived` replaces the others while the project is archived.

**Datasets.** A CSV or an `.xlsx` workbook (first sheet, converted to CSV). UTF-8, unique header names, equal row widths, 50 MiB by default (`DATASET_MAX_UPLOAD_BYTES`). A new file is a new version. Nothing is overwritten.

**Connections.** A project saves a connection to an external source, browses it, previews rows and imports a table or a `SELECT` as a dataset version. Credentials are encrypted with `CONNECTION_SECRET_KEY` and never returned. Without the key the feature is off (`503 CONNECTIONS_NOT_CONFIGURED`). Reads are read-only as far as the connection can make them; connect with a user that can only read. Hosts are resolved once and loopback, private and link-local addresses are refused; only `CONNECTION_ALLOW_PRIVATE_HOSTS` (local development only) lifts this. Google Sheets and Drive also need the Google settings and `APP_URL`.

**Runs.** A project works on one run at a time (`409 RUN_ACTIVE`). Run status, as defined in `src/management/research/schemas/runs.py`:

`queued`, `running`, `paused`, `awaiting_review`, `completed`, `budget_exceeded`, `failed`, `stopped`

`RUN_BUDGET_ENFORCED` (default `false`) switches the USD cap on: off, a run never ends as `budget_exceeded` and engines get a nominal cap of `RUN_MAX_BUDGET_USD` x 100.

`queued`, `running`, `paused` and `awaiting_review` are active. The other four are not. A Project Manager can abandon a stuck run: the Platform marks it `failed` and does not tell the engine, so check the engine side. `POST .../runs/{run}/sync` asks Popper for the current state; the Platform does not poll.

**Rules that always hold.** A project keeps one active Project Manager (`LAST_PROJECT_MANAGER`). An archived project is read-only (`PROJECT_ARCHIVED`). Adding a member is an invitation: the user is a member only after accepting, and the project answers `404` until then. Every change goes to the audit log.

### How runs call engines

With `ENGINES` set, runs go through the engine pipeline:

- `ENGINES` is JSON: engine key to `{base_url, service_key, timeout_seconds}`. `base_url` includes the engine prefix, for example `http://i2h:8000/v1`.
- `PIPELINES` is JSON: pipeline name to an ordered list of `{stage_key, engine, loop_back_to}`. `DEFAULT_PIPELINE` (default `default`) names the one a run uses. `loop_back_to` points to earlier stages. Settings fail to load if a pipeline names an unknown engine or repeats a stage key.
- The Platform calls each engine with `X-Service-Key` set to that engine's `service_key`.
- Engines call back at `{ENGINE_CALLBACK_BASE_URL}{API_PREFIX}/internal/engines/{engine_key}/runs/{stage_run_id}/events` and `.../artifacts`, and download input artifacts from `/internal/engines/{engine_key}/artifacts/{artifact_id}`. `ENGINE_CALLBACK_BASE_URL` falls back to `PUBLIC_BASE_URL`. A callback without a valid key gets `401 SERVICE_KEY_INVALID` before its body is read.
- Users answer gates through `POST /projects/{id}/runs/{run}/gates/{gate_id}`. The Platform forwards the answer. Pause, resume and abandon work the same way.

The wire contract (paths, models, error codes, versioning) is owned here: [docs/contract/README.md](docs/contract/README.md). Models live in `src/management/research/contracts/`. An engine with an incompatible major version is rejected with `ENGINE_CONTRACT_MISMATCH`.

**Popper (legacy path).** When `ENGINES` is empty, runs go to Popper at `POPPER_BASE_URL`. Popper calls back at `/api/v1/internal/popper/runs/{run_id}/events`, `/status` and `/artifacts` with `POPPER_CALLBACK_KEY` in `X-Service-Key`. Without `POPPER_BASE_URL`, everything works except starting, syncing and reviewing a run, which answer `503 POPPER_NOT_CONFIGURED`.

## Roles and permissions

A role alone decides access. There is no permission table or custom role. A user has one platform role (`user` or `platform_admin`) and at most one role per project.

| Action | Platform Admin | Project Manager | Researcher | Reviewer |
|---|:-:|:-:|:-:|:-:|
| Create a project (any signed-in user; creator becomes Project Manager) | yes | yes | yes | yes |
| List projects | all | own | own | own |
| Read project, members, datasets, runs, results, comments | yes | yes | yes | yes |
| Update, archive, restore, complete, reopen a project | yes | yes | no | no |
| Invite, change role, remove a member; cancel or resend an invitation | yes | yes | no | no |
| Upload or rename datasets; upload or delete project files | yes | yes | yes | no |
| Create, test, browse, import from a data connection | yes | yes | yes | no |
| Start or sync a run; answer a gate | yes | yes | yes | no |
| Abandon a run | yes | yes | no | no |
| Comment; edit own comment | yes | yes | yes | yes |
| Delete a comment | any | any | own | own |
| Read the project audit log | yes | yes | no | no |
| Suspend users, grant Platform Admin, global audit log, admin pages | yes | no | no | no |

Someone who is not a member gets `404` for everything in a project. A member whose role is too low gets `403 ROLE_REQUIRED`. The exception is the project audit log: a Researcher or Reviewer gets `404`, so the log stays hidden.

## Authentication

- The login name is the email. There are no passwords: `POST /api/v1/auth/email/start` emails a 6-digit code and a link to `{APP_URL}/auth/verify#token=...`, and creates the account for an unknown address. It answers the same for every address. Either the code (`email/verify`) or the link (`email/verify-link`, after `email/link-info` shows the masked address) signs in; using one ends the other. Both last `OTP_TTL_MINUTES` (10). Google sign-in works beside it.
- `user.onboarded` is false until the user sets a name with `PATCH /api/v1/auth/me`; the frontend asks for one first.
- `email/verify` and `email/verify-link` set an HttpOnly session cookie (`platform_session`; `__Host-platform_session` in the production template) and return a CSRF token. After a reload, `GET /api/v1/auth/me` restores the user and `GET /api/v1/auth/csrf-token` returns the token.
- Send `credentials: "include"`, an `Origin` listed in `CORS_ALLOWED_ORIGINS`, and `X-CSRF-Token` on every `POST`, `PATCH`, `PUT` and `DELETE`. Exempt are the four calls made before a session exists: `email/start`, `email/verify`, `email/link-info`, `email/verify-link`. A bad token gets `403 CSRF_INVALID`.
- A session lasts 30 days (`SESSION_ABSOLUTE_DAYS`) and ends after 7 idle days (`SESSION_IDLE_MINUTES`). On `401` with `SESSION_EXPIRED` or `USER_SUSPENDED` the cookie is already cleared.
- **Google sign-in** is off until `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` and `APP_URL` are all set; its two routes answer `404` until then. The browser navigates to `GET /api/v1/auth/google/start`. It returns to `{APP_URL}/` signed in, or to `{APP_URL}/auth/login?error=CODE` with `GOOGLE_SIGN_IN_FAILED`, `GOOGLE_EMAIL_NOT_VERIFIED` or `USER_SUSPENDED`. Only verified `@gmail.com` and Google Workspace addresses are accepted.
- **Rate limits** are counted per client address and API process over `AUTH_SESSION_RATE_WINDOW_SECONDS` (60) and answer `429 RATE_LIMITED` with `Retry-After`: `email/start` uses `AUTH_SESSION_RATE_LIMIT` (10); `email/verify`, `email/link-info` and `email/verify-link` share `AUTH_CODE_RATE_LIMIT` (20). Ten wrong codes in a row lock the code for an hour; the link still works.

## API

Swagger UI at `/docs` is the reference for every route, body and error. Route groups under `/api/v1`: `health`, `auth` (including `auth/google`), `users`, `audit`, `projects` (members, comments, project files, invite candidates), `invitations`, `connections` (including `connections/google`), `datasets`, `projects/{id}/runs` (events, gates, review, results, stages), `notifications`, `admin` (usage, overview, log monitoring, models) and `internal` (engine and Popper callbacks, service key only). Public routes answer in an envelope: `success`, `message`, `data` and `meta`, or `error` with a `code` on failure. Engine callbacks use bare bodies.

Four SSE streams push snapshots. Each event replaces the client's view; do not append.

- `GET /notifications/stream?limit=50`: event `notifications`.
- `GET /projects/{id}/invite-candidates/stream`: event `invite-candidates`; takes the query of the matching GET.
- `GET /projects/{id}/runs/{run}/events/stream`: run events.
- `GET /admin/log-monitoring/stream`: admin monitoring updates.

Streams send a keep-alive every 15 seconds that rechecks the session without extending idle expiry. `session-ended` or `access-ended` means close the stream. The proxy must disable buffering and allow read timeouts above 15 seconds. `EventSource` clients reconnect and get a fresh snapshot.

## Configuration

`.env.example` is the complete reference: every setting in `src/management/core/config.py`, with its default and a comment. Settings load from `.env.local` and the process environment. The Compose files forward a subset; `ENGINES`, `PIPELINES` and `ENGINE_CALLBACK_BASE_URL` are not forwarded yet, so add them to `api.environment` to use engines in Compose.

Production (`APP_ENV=production`) must set:

| Setting | Rule |
|---|---|
| `APP_ENV` | `production` (the production Compose file sets it) |
| `DATABASE_URL` | PostgreSQL, `postgresql+asyncpg://...`; the same password as `POSTGRES_PASSWORD` |
| `SESSION_SIGNING_SECRET` | 32+ characters, not starting with `local-only-` |
| `CORS_ALLOWED_ORIGINS`, `APP_URL` | HTTPS only; `APP_URL` is required, sign-in links point to it |
| `PUBLIC_BASE_URL` | Address engines and Popper use to reach this API, without `/api/v1` |
| `RESEND_API_KEY` | Required in staging and production; no account can be verified without it |
| `POPPER_SERVICE_KEY`, `POPPER_CALLBACK_KEY` | 32+ characters each, when `POPPER_BASE_URL` is set |
| `STORAGE_BACKEND` | `local` (a volume) or `r2` |
| `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | With `r2`, plus `R2_ACCOUNT_ID` or `R2_ENDPOINT_URL` (for buckets with a jurisdiction) |
| `CONNECTION_SECRET_KEY` | A Fernet key; enables data connections |
| `GOOGLE_OAUTH_*`, `GOOGLE_PICKER_*` | Only if you use Google sign-in, Sheets, Drive or the picker |

Generate the connection key and keep a copy. Replacing it makes every saved connection unusable until it is created again:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Production also forces secure cookies and refuses `DEFAULT_ADMIN_EMAIL` and `CONNECTION_ALLOW_PRIVATE_HOSTS`.

## Deployment and operations

Production uses its own Compose project, database volume and storage volume.

```bash
cp docker/prod.env.example docker/prod.env.local   # replace every placeholder
task prod:config    # runs `docker compose config --quiet`: validates the Compose file and variable substitution
task prod:up
task prod:migrate
```

`task prod:config` does not load the application settings. The API validates them when it starts, and refuses to start on a bad value. Other tasks: `prod:status`, `prod:logs`, `prod:down` (keeps the volumes).

- The API binds to `127.0.0.1:${API_PORT:-8080}` and expects a TLS-terminating reverse proxy. PostgreSQL has no host port.
- **Releasing a migration.** Migrate before the new code serves requests: `docker compose --env-file docker/prod.env.local -f docker/docker-compose.prod.yml build api`, then `task prod:migrate`, then `task prod:up`.
- **Storage.** Run a single API replica, or give every replica the same volume. Back up the storage volume with the database: rows point at files by key. Switching backend does not move files; copy them with the same keys first. Keep an R2 bucket private: every download goes through this API.
- **Graceful shutdown.** Docker runs Uvicorn with `--timeout-graceful-shutdown 30` and Compose waits 40 seconds, so a restart is bounded even with open SSE streams. Add the same flag when you start Uvicorn by hand.
- **Per-process limits.** Sign-in, code and connection limits are counted per client address and per API process. Behind a proxy, Uvicorn must trust only the proxy addresses, or all requests may share one address. Add a shared rate limit at the ingress. The Google callbacks have no rate limit of their own.
- **Slow imports.** An import keeps its request open up to `CONNECTION_IMPORT_TIMEOUT_SECONDS` (300). A proxy with a shorter read timeout cuts the response, but the import still finishes.
- **Database firewalls.** The server has no fixed outbound address, so a database behind an allowlist cannot be reached.
