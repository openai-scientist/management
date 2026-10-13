from management.research.engines.client import EngineClient


class EngineRegistry:
    def __init__(self, clients: dict[str, EngineClient]):
        self.clients = clients
        self.capabilities = {}

    def get(self, engine_key: str) -> EngineClient:
        return self.clients[engine_key]

    async def refresh_capabilities(self, engine_key: str):
        self.capabilities[engine_key] = await self.get(engine_key).capabilities(force=True)
        return self.capabilities[engine_key]


def build_engine_registry(settings) -> EngineRegistry:
    return EngineRegistry(
        {key: EngineClient(key, engine) for key, engine in settings.engines.items()}
    )
