import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from management.research.contracts import (
    CONTRACT_VERSION,
    TERMINAL_PHASES,
    ArtifactMeta,
    Capabilities,
    EventBatch,
    EventClass,
    EventEnvelope,
    EventPage,
    FailureInfo,
    Gate,
    GateAnswer,
    GateOption,
    Health,
    ModelRole,
    Phase,
    PipelineRecommendation,
    ResearchProfile,
    ReviewFindings,
    RunCost,
    RunCreateRequest,
    RunCreateResponse,
    RunState,
    TelemetryBatch,
    TelemetryRecord,
    TokenUsage,
)
from management.research.contracts.base import PIPELINE_RECOMMENDATION, REVIEW_FINDINGS


class FakeEngine:
    """In-process engine for Platform tests; credentials never leave memory."""

    def __init__(
        self,
        *,
        engine_key: str = "fake",
        stage_keys: list[str] | None = None,
        output_schema: str = "fake.output/v1",
        input_schemas: list[str] | None = None,
        service_key: str = "test-service-key",
        callback_transport: httpx.AsyncBaseTransport | None = None,
        auto_revision: bool = True,
        profiles: list[ResearchProfile] | None = None,
        reuses_prior_run: bool = False,
        integrations: list[str] | None = None,
        optional_integrations: list[str] | None = None,
        providers: list[str] | None = None,
    ):
        self.engine_key = engine_key
        self.service_key = service_key
        self.callback_transport = callback_transport
        self.output_schema = output_schema
        self.capabilities = Capabilities(
            engine_key=engine_key,
            engine_version="test",
            contract_version=CONTRACT_VERSION,
            stage_keys=stage_keys or ["hypothesis"],
            input_schemas=input_schemas or [],
            output_schemas=[{"schema": output_schema, "primary": True}],
            steps=[{"step_key": "work", "label": "Work", "order": 1}],
            activity_types=[f"{engine_key}.work"],
            auto_revision=auto_revision,
            profiles=profiles if profiles is not None else [ResearchProfile()],
            reuses_prior_run=reuses_prior_run,
            integrations=integrations or [],
            optional_integrations=optional_integrations or [],
            providers=providers if providers is not None else ["openai_compatible"],
        )
        self.app = FastAPI()
        self.requests: list[RunCreateRequest] = []
        self.request_headers: list[dict[str, str]] = []
        self.states: dict[str, RunState] = {}
        self.events: dict[str, list[EventEnvelope]] = {}
        self.llm_calls: list[tuple[Phase, str]] = []
        self.primary_content = json.dumps({"hypotheses": [{"id": "H1"}, {"id": "H2"}]}).encode()
        self._by_stage: dict[str, str] = {}
        self._requests: dict[str, RunCreateRequest] = {}
        self._delivered: dict[str, int] = {}
        self._closed: set[str] = set()
        self._primary: dict[str, str] = {}
        self._versions: dict[tuple[str, str], int] = {}
        self._answers: dict[tuple[str, str], GateAnswer] = {}
        self._paused: dict[str, Phase] = {}
        self._scripted = False
        self._unreachable = False
        self._health: Literal["ok", "degraded", "down"] = "ok"
        self._routes()

    @staticmethod
    def _error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"code": code, "message": message})

    def _routes(self) -> None:
        @self.app.middleware("http")
        async def auth(request: Request, call_next):
            if self._unreachable:
                return self._error(503, "ENGINE_UNAVAILABLE", "Engine is unreachable")
            key = request.headers.get("X-Service-Key")
            if not (request.url.path == "/health" and key is None) and key != self.service_key:
                return self._error(401, "SERVICE_KEY_INVALID", "Invalid service key")
            return await call_next(request)

        @self.app.exception_handler(RequestValidationError)
        async def validation_error(request, exc):
            details = [
                {key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()
            ]
            return JSONResponse(
                status_code=422,
                content={
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request",
                    "details": details,
                },
            )

        @self.app.get("/health")
        async def health(x_service_key: str | None = Header(default=None)):
            model = Health(
                status=self._health,
                engine_version="test",
                contract_version=CONTRACT_VERSION,
                active_runs=sum(s.phase not in TERMINAL_PHASES for s in self.states.values()),
                checks={"storage_writable": True} if x_service_key else None,
            )
            return model.model_dump(mode="json", exclude_none=True)

        @self.app.get("/capabilities")
        async def capabilities():
            return self.capabilities

        @self.app.post("/runs")
        async def create(req: RunCreateRequest, request: Request):
            if req.stage_run_id in self._by_stage:
                state = self.states[self._by_stage[req.stage_run_id]]
                return RunCreateResponse(
                    engine_run_id=state.engine_run_id, phase=state.phase, created=False
                )
            engine_id = str(uuid4())
            self.requests.append(req)
            self.request_headers.append(dict(request.headers))
            self._requests[engine_id] = req
            self._by_stage[req.stage_run_id] = engine_id
            self.states[engine_id] = RunState(
                engine_run_id=engine_id,
                stage_run_id=req.stage_run_id,
                phase=Phase.drafting,
                step_key="work",
                waiting_input=False,
                open_gate=None,
            )
            self.events[engine_id] = []
            self._delivered[engine_id] = 0
            await self._emit(engine_id, "run.phase_changed", payload={"from": None})
            if self._scripted:
                await self._script(engine_id)
            return RunCreateResponse(
                engine_run_id=engine_id, phase=self.states[engine_id].phase, created=True
            )

        @self.app.get("/runs/{engine_run_id}")
        async def state(engine_run_id: str):
            return self.states.get(engine_run_id) or self._error(
                404, "RUN_NOT_FOUND", "Run not found"
            )

        @self.app.get("/runs/{engine_run_id}/events")
        async def events(
            engine_run_id: str, after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=500)
        ):
            if engine_run_id not in self.states:
                return self._error(404, "RUN_NOT_FOUND", "Run not found")
            return EventPage(
                events=[e for e in self.events[engine_run_id] if e.source_seq > after][:limit]
            )

        @self.app.post("/runs/{engine_run_id}/gates/{gate_id}")
        async def answer(engine_run_id: str, gate_id: str, body: GateAnswer):
            if engine_run_id not in self.states:
                return self._error(404, "RUN_NOT_FOUND", "Run not found")
            state = self.states[engine_run_id]
            prior = self._answers.get((engine_run_id, gate_id))
            if prior is not None:
                return (
                    state if prior == body else self._error(409, "GATE_NOT_OPEN", "Gate is closed")
                )
            gate = state.open_gate
            if gate is None or gate.gate_id != gate_id:
                return self._error(409, "GATE_NOT_OPEN", "Gate is not open")
            if gate.kind == "question":
                if body.action != "answer" or body.option_id not in [
                    o.option_id for o in gate.options
                ]:
                    return self._error(422, "GATE_OPTION_INVALID", "Invalid option")
            elif body.action == "answer":
                return self._error(422, "GATE_ACTION_INVALID", "Review requires approve or comment")
            self._answers[(engine_run_id, gate_id)] = body
            state.open_gate = None
            state.waiting_input = False
            await self._emit(
                engine_run_id,
                "run.gate_closed",
                payload={
                    "gate_id": gate_id,
                    "action": body.action,
                    "actor": body.actor,
                },
            )
            if body.action == "approve":
                await self.publish(engine_run_id)
            elif body.action == "comment":
                await self.to_phase(engine_run_id, Phase.revising)
                if self._scripted:
                    await self._script(engine_run_id)
            return state

        @self.app.post("/runs/{engine_run_id}/pause")
        async def pause(engine_run_id: str):
            state = self.states.get(engine_run_id)
            if state is None:
                return self._error(404, "RUN_NOT_FOUND", "Run not found")
            if state.phase in TERMINAL_PHASES:
                return self._error(409, "RUN_CLOSED", "Run has ended")
            if state.phase != Phase.paused:
                self._paused[engine_run_id] = state.phase
                await self.to_phase(engine_run_id, Phase.paused)
            return state

        @self.app.post("/runs/{engine_run_id}/resume")
        async def resume(engine_run_id: str):
            state = self.states.get(engine_run_id)
            if state is None:
                return self._error(404, "RUN_NOT_FOUND", "Run not found")
            if state.phase == Phase.paused:
                await self.to_phase(engine_run_id, self._paused.pop(engine_run_id))
                if self._scripted and state.open_gate is None:
                    await self._script(engine_run_id)
            return state

        @self.app.post("/runs/{engine_run_id}/cancel")
        async def cancel(engine_run_id: str):
            state = self.states.get(engine_run_id)
            if state is None:
                return self._error(404, "RUN_NOT_FOUND", "Run not found")
            if state.phase not in TERMINAL_PHASES:
                state.open_gate = None
                state.waiting_input = False
                await self.to_phase(engine_run_id, Phase.cancelled)
                await self._emit(engine_run_id, "run.cancelled")
            return state

    def scripted(self) -> None:
        self._scripted = True

    def set_health(self, status: Literal["ok", "degraded", "down"]) -> None:
        self._health = status

    def set_unreachable(self, value: bool) -> None:
        self._unreachable = value

    async def _emit(
        self,
        engine_id: str,
        event_type: str,
        *,
        payload=None,
        event_class: EventClass = EventClass.lifecycle,
        summary=None,
        actor=None,
        step_key=None,
    ) -> None:
        state = self.states[engine_id]
        event = EventEnvelope(
            source_seq=len(self.events[engine_id]) + 1,
            type=event_type,
            event_class=event_class,
            phase=state.phase,
            step_key=step_key or state.step_key,
            payload=payload or {},
            actor=actor,
            summary=summary,
            occurred_at=datetime.now(UTC),
        )
        self.events[engine_id].append(event)
        state.last_source_seq = event.source_seq
        await self._deliver(engine_id)

    async def _deliver(self, engine_id: str) -> None:
        if self.callback_transport is None or engine_id in self._closed:
            return
        req = self._requests[engine_id]
        async with httpx.AsyncClient(transport=self.callback_transport) as client:
            for _ in range(3):
                pending = self.events[engine_id][self._delivered[engine_id] :]
                if not pending:
                    return
                response = await client.post(
                    req.callback_url + "/events",
                    headers={"X-Service-Key": self.service_key},
                    json=EventBatch(events=pending).model_dump(mode="json", by_alias=True),
                )
                if response.status_code < 300:
                    self._delivered[engine_id] = pending[-1].source_seq
                    return
                if response.status_code != 409:
                    return
                code = response.json().get("code")
                if code == "STAGE_CLOSED":
                    self._closed.add(engine_id)
                    state = self.states[engine_id]
                    state.phase = Phase.cancelled
                    state.open_gate = None
                    state.waiting_input = False
                    return
                if code != "EVENT_GAP":
                    return
                self._delivered[engine_id] = response.json()["delivered_seq"]

    async def to_phase(
        self, engine_run_id: str, phase: Phase, *, step_key: str | None = None
    ) -> None:
        state = self.states[engine_run_id]
        before = state.phase
        state.phase = phase
        if step_key:
            state.step_key = step_key
        await self._emit(engine_run_id, "run.phase_changed", payload={"from": before})

    async def _llm(self, engine_id: str) -> None:
        state = self.states[engine_id]
        req = self._requests[engine_id]
        role = ModelRole.review if state.phase == Phase.reviewing else ModelRole.stage
        model = getattr(req.models, role)
        self.llm_calls.append((state.phase, model.model))
        await self.emit_activity(engine_id, f"{self.engine_key}.work", "Completed model task")
        if self.callback_transport is not None:
            record = TelemetryRecord(
                occurred_at=datetime.now(UTC),
                level="INFO",
                message="Model call finished",
                platform_run_id=req.platform_run_id,
                stage_run_id=req.stage_run_id,
                engine_run_id=engine_id,
                phase=state.phase,
                step_key=state.step_key,
                llm=dict(
                    role=role,
                    provider=model.provider,
                    model=model.model,
                    input_tokens=10,
                    output_tokens=2,
                    latency_ms=1,
                    outcome="ok",
                ),
            )
            async with httpx.AsyncClient(transport=self.callback_transport) as client:
                await client.post(
                    req.callback_url + "/telemetry",
                    headers={"X-Service-Key": self.service_key},
                    json=TelemetryBatch(records=[record]).model_dump(mode="json", by_alias=True),
                )

    async def _script(self, engine_id: str) -> None:
        await self._llm(engine_id)
        await self.to_phase(engine_id, Phase.reviewing)
        await self._llm(engine_id)
        await self.open_review_gate(engine_id)

    async def _upload(
        self, engine_id: str, schema: str, content: bytes, *, primary=False, review_round=None
    ) -> str:
        key = (engine_id, schema)
        version = self._versions.get(key, 0) + 1
        meta = ArtifactMeta(
            schema=schema,
            version=version,
            primary=primary,
            filename=f"artifact-{version}.json",
            media_type="application/json",
            sha256=hashlib.sha256(content).hexdigest(),
            review_round=review_round,
        )
        if self.callback_transport is not None:
            async with httpx.AsyncClient(transport=self.callback_transport) as client:
                response = await client.post(
                    self._requests[engine_id].callback_url + "/artifacts",
                    headers={"X-Service-Key": self.service_key},
                    data={"meta": meta.model_dump_json(by_alias=True)},
                    files={"file": (meta.filename, content, meta.media_type)},
                )
                response.raise_for_status()
                artifact_id = response.json()["artifact_id"]
        else:
            artifact_id = str(uuid4())
        self._versions[key] = version
        await self._emit(
            engine_id,
            "artifact.delivered",
            payload={
                "artifact_id": artifact_id,
                "schema": schema,
                "version": version,
                "primary": primary,
            },
        )
        return artifact_id

    async def open_question_gate(
        self, engine_run_id: str, *, step_key: str, options: list[str], default: str
    ) -> Gate:
        state = self.states[engine_run_id]
        gate = Gate(
            gate_id=str(uuid4()),
            kind="question",
            step_key=step_key,
            prompt="Choose an option",
            options=[GateOption(option_id=o, label=o) for o in options],
            default_option_id=default,
            opened_at=datetime.now(UTC),
        )
        if self._requests[engine_run_id].interaction_mode in {"auto", "light"}:
            await self.emit_activity(
                engine_run_id, f"{self.engine_key}.work", "Selected default option", actor="system"
            )
        else:
            state.open_gate = gate
            state.waiting_input = True
            await self._emit(
                engine_run_id,
                "run.gate_opened",
                payload=gate.model_dump(mode="json", by_alias=True),
            )
        return gate

    async def open_review_gate(
        self, engine_run_id: str, *, findings: ReviewFindings | None = None
    ) -> Gate:
        findings = findings or ReviewFindings(
            round=self._versions.get((engine_run_id, self.output_schema), 0) + 1,
            reviewed_artifact_id="reviewed",
            reviewer_model=self._requests[engine_run_id].models.review.model,
            findings=[],
            passed=True,
        )
        review_id = await self._upload(
            engine_run_id,
            REVIEW_FINDINGS,
            findings.model_dump_json().encode(),
            review_round=findings.round,
        )
        primary_id = await self._upload(
            engine_run_id, self.output_schema, self.primary_content, primary=True
        )
        self._primary[engine_run_id] = primary_id
        await self._emit(
            engine_run_id,
            "review.completed",
            payload={"round": findings.round, "passed": findings.passed, "artifact_id": review_id},
        )
        await self.to_phase(engine_run_id, Phase.awaiting_researcher)
        gate = Gate(
            gate_id=str(uuid4()),
            kind="artifact_review",
            step_key="work",
            prompt="Review result",
            artifact_ids=[primary_id],
            opened_at=datetime.now(UTC),
        )
        state = self.states[engine_run_id]
        state.open_gate = gate
        state.waiting_input = True
        await self._emit(
            engine_run_id, "run.gate_opened", payload=gate.model_dump(mode="json", by_alias=True)
        )
        return gate

    async def publish(
        self, engine_run_id: str, *, recommendation: PipelineRecommendation | None = None
    ) -> None:
        if engine_run_id not in self._primary:
            self._primary[engine_run_id] = await self._upload(
                engine_run_id, self.output_schema, self.primary_content, primary=True
            )
        if recommendation:
            await self._upload(
                engine_run_id, PIPELINE_RECOMMENDATION, recommendation.model_dump_json().encode()
            )
        self.states[engine_run_id].open_gate = None
        self.states[engine_run_id].waiting_input = False
        await self.to_phase(engine_run_id, Phase.published)
        await self._emit(
            engine_run_id, "run.published", payload={"artifact_id": self._primary[engine_run_id]}
        )

    async def fail(self, engine_run_id: str, code: str, message: str) -> None:
        state = self.states[engine_run_id]
        state.failure = FailureInfo(code=code, message=message)
        state.open_gate = None
        state.waiting_input = False
        await self.to_phase(engine_run_id, Phase.failed)
        await self._emit(engine_run_id, "run.failed", payload=state.failure.model_dump())

    async def emit_activity(
        self, engine_run_id: str, type: str, summary: str, *, actor="agent", step_key="work"
    ) -> None:
        await self._emit(
            engine_run_id,
            type,
            event_class=EventClass.activity,
            summary=summary,
            actor=actor,
            step_key=step_key,
        )

    async def report_cost(
        self,
        engine_run_id: str,
        cost_usd: Decimal | None,
        *,
        usage: dict[ModelRole, TokenUsage] | None = None,
    ) -> None:
        if cost_usd is not None:
            self.states[engine_run_id].cost_usd = cost_usd
        cost = RunCost(cost_usd=cost_usd, usage=usage or {})
        await self._emit(engine_run_id, "run.cost", payload=cost.model_dump(mode="json"))
