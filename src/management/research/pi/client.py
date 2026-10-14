import asyncio
from uuid import uuid4

import httpx
from pydantic import ValidationError

from management.core.engine_settings import EngineSettings
from management.core.responses import request_id_context, traceparent_context
from management.research.contracts import (
    ConsultationAccepted,
    ConsultationRequest,
    ConsultationState,
    PICapabilities,
    is_compatible,
    paths,
)


class PIError(Exception):
    pass


class PIUnavailable(PIError):
    """The PI could not be reached, timed out or failed on its side."""


class PIRejected(PIError):
    """The PI refused the request, or answered with something that is not the contract."""


class PIClient:
    def __init__(
        self, settings: EngineSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        self.transport = transport

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
            raise PIUnavailable("PI could not be reached") from None
        if response.status_code >= 500:
            raise PIUnavailable("PI is unavailable")
        if response.status_code >= 400:
            raise PIRejected(f"PI rejected the request ({response.status_code})")
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise PIRejected("PI returned an invalid contract response") from None

    async def capabilities(self) -> PICapabilities:
        caps = await self._request("GET", paths.PI_CAPABILITIES, PICapabilities)
        if not is_compatible(caps.contract_version):
            raise PIRejected("PI contract major version does not match")
        return caps

    async def consult(self, request: ConsultationRequest) -> ConsultationAccepted:
        return await self._request(
            "POST", paths.PI_CONSULTATIONS, ConsultationAccepted, json=request.to_wire()
        )

    async def get(self, consultation_id: str) -> ConsultationState:
        return await self._request(
            "GET",
            paths.PI_CONSULTATION.format(consultation_id=consultation_id),
            ConsultationState,
        )


def build_pi_client(settings) -> PIClient | None:
    return PIClient(settings.pi_service) if settings.pi_service else None
