"""In-process PI service for Platform tests. It records requests; tests send the results."""

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from management.core.engine_settings import EngineSettings
from management.research.contracts import (
    CONTRACT_VERSION,
    ConsultationAccepted,
    ConsultationKind,
    ConsultationRequest,
    ConsultationResult,
    ConsultationState,
    PICapabilities,
    PIHealth,
)
from management.research.pi.client import PIClient

PI_SERVICE_KEY = "test-pi-service-key-0123456789abcdef"


def pi_overrides():
    return {"pi_service": {"base_url": "http://pi.test", "service_key": PI_SERVICE_KEY}}


class FakePI:
    def __init__(self, harness):
        self.harness = harness
        self.requests: list[ConsultationRequest] = []
        self.wire: list[dict] = []
        self.results: dict[str, ConsultationResult] = {}
        self.unreachable = False
        self.app = FastAPI()
        self._routes()

    def _routes(self) -> None:
        @self.app.middleware("http")
        async def auth(request: Request, call_next):
            if self.unreachable:
                return JSONResponse(status_code=503, content={"code": "PI_UNAVAILABLE"})
            if request.headers.get("X-Service-Key") != PI_SERVICE_KEY:
                return JSONResponse(status_code=401, content={"code": "SERVICE_KEY_INVALID"})
            return await call_next(request)

        @self.app.get("/health")
        async def health():
            return PIHealth(status="ok", service_version="test", contract_version=CONTRACT_VERSION)

        @self.app.get("/capabilities")
        async def capabilities():
            return PICapabilities(
                service_version="test",
                contract_version=CONTRACT_VERSION,
                kinds=list(ConsultationKind),
            )

        @self.app.post("/consultations", status_code=202)
        async def consult(body: dict):
            request = ConsultationRequest.model_validate(body)
            created = request.consultation_id not in {r.consultation_id for r in self.requests}
            if created:
                self.requests.append(request)
                self.wire.append(body)
            return ConsultationAccepted(consultation_id=request.consultation_id, created=created)

        @self.app.get("/consultations/{consultation_id}")
        async def state(consultation_id: str):
            result = self.results.get(consultation_id)
            return ConsultationState(
                consultation_id=consultation_id,
                status="decided" if result else "running",
                result=result,
            )

    def install(self) -> "FakePI":
        settings = EngineSettings(**pi_overrides()["pi_service"])
        client = PIClient(settings, transport=httpx.ASGITransport(app=self.app))
        self.harness.app.state.pi_client = client
        return self

    def of_kind(self, kind: str) -> list[ConsultationRequest]:
        return [r for r in self.requests if r.kind == kind]

    @property
    def last(self) -> ConsultationRequest:
        return self.requests[-1]

    async def respond(self, request: ConsultationRequest, result: ConsultationResult, *, key=None):
        """Call the Platform callback the way the PI service does."""
        self.results[request.consultation_id] = result
        path = request.callback_url.removeprefix("http://localhost:8080")
        async with self.harness.client() as client:
            return await client.post(
                path,
                json=result.model_dump(mode="json"),
                headers={"X-Service-Key": key or PI_SERVICE_KEY},
            )
