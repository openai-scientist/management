import asyncio
import time
from uuid import uuid4

import httpx
from pydantic import ValidationError

from management.contracts import (
    Capabilities,
    EventPage,
    GateAnswer,
    Health,
    RunCreateRequest,
    RunCreateResponse,
    RunState,
    is_compatible,
    paths,
)
from management.core.engine_settings import EngineSettings
from management.core.responses import request_id_context, traceparent_context


class EngineError(Exception):
    pass


class EngineUnavailable(EngineError):
    pass


class EngineRejected(EngineError):
    def __init__(self, status_code: int, code: str):
        super().__init__(f"Engine rejected request ({code})")
        self.status_code = status_code
        self.code = code


class EngineContractMismatch(EngineError):
    pass


class EngineClient:
    def __init__(
        self,
        engine_key: str,
        settings: EngineSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.engine_key = engine_key
        self.settings = settings
        self.transport = transport
        self._capabilities: Capabilities | None = None
        self._capabilities_at = 0.0

    async def _request(self, method, path, model, **kwargs):
        headers = {
            paths.SERVICE_KEY_HEADER: self.settings.service_key.get_secret_value(),
            paths.REQUEST_ID_HEADER: request_id_context.get() or str(uuid4()),
            paths.TRACEPARENT_HEADER: traceparent_context.get()
            or f"00-{uuid4().hex}-{uuid4().hex[:16]}-01",
        }
        try:
            async with (
                asyncio.timeout(self.settings.timeout_seconds),
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.settings.timeout_seconds,
                    headers=headers,
                ) as client,
            ):
                response = await client.request(
                    method, self.settings.base_url.rstrip("/") + path, **kwargs
                )
        except (httpx.RequestError, TimeoutError):
            raise EngineUnavailable("Engine could not be reached") from None
        if response.status_code >= 500:
            raise EngineUnavailable("Engine is unavailable")
        if response.status_code >= 400:
            try:
                body = response.json()
                code = (
                    body.get("code", "ENGINE_REJECTED")
                    if isinstance(body, dict)
                    else "ENGINE_REJECTED"
                )
            except ValueError:
                code = "ENGINE_REJECTED"
            raise EngineRejected(response.status_code, code)
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise EngineContractMismatch("Engine returned an invalid contract response") from None

    async def health(self) -> Health:
        return await self._request("GET", paths.ENGINE_HEALTH, Health)

    async def capabilities(self, *, force: bool = False) -> Capabilities:
        if force or self._capabilities is None or time.monotonic() - self._capabilities_at >= 60:
            caps = await self._request("GET", paths.ENGINE_CAPABILITIES, Capabilities)
            if not is_compatible(caps.contract_version) or caps.engine_key != self.engine_key:
                raise EngineContractMismatch("Engine contract major version or key does not match")
            self._capabilities = caps
            self._capabilities_at = time.monotonic()
        return self._capabilities

    async def create_run(self, req: RunCreateRequest) -> RunCreateResponse:
        return await self._request("POST", paths.ENGINE_RUNS, RunCreateResponse, json=req.to_wire())

    async def get_run(self, engine_run_id: str) -> RunState:
        return await self._request(
            "GET", paths.ENGINE_RUN.format(engine_run_id=engine_run_id), RunState
        )

    async def events(self, engine_run_id: str, after: int = 0, limit: int = 500) -> EventPage:
        return await self._request(
            "GET",
            paths.ENGINE_RUN_EVENTS.format(engine_run_id=engine_run_id),
            EventPage,
            params={"after": after, "limit": limit},
        )

    async def answer_gate(self, engine_run_id: str, gate_id: str, answer: GateAnswer) -> RunState:
        return await self._request(
            "POST",
            paths.ENGINE_RUN_GATE.format(engine_run_id=engine_run_id, gate_id=gate_id),
            RunState,
            json=answer.model_dump(mode="json", by_alias=True),
        )

    async def pause(self, engine_run_id: str) -> RunState:
        return await self._request(
            "POST", paths.ENGINE_RUN_PAUSE.format(engine_run_id=engine_run_id), RunState
        )

    async def resume(self, engine_run_id: str) -> RunState:
        return await self._request(
            "POST", paths.ENGINE_RUN_RESUME.format(engine_run_id=engine_run_id), RunState
        )

    async def cancel(self, engine_run_id: str) -> RunState:
        return await self._request(
            "POST", paths.ENGINE_RUN_CANCEL.format(engine_run_id=engine_run_id), RunState
        )
