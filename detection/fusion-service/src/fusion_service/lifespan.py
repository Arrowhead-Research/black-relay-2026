"""FastAPI lifespan: the runtime lives on app.state for the duration of the server."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fusion_service.domain.time import Clock
from fusion_service.runtime import build_runtime
from fusion_service.settings import Settings


def build_lifespan(settings: Settings, *, clock: Clock, start_mqtt: bool = True):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        runtime = build_runtime(settings, clock)
        app.state.runtime = runtime
        try:
            runtime.start(start_mqtt=start_mqtt)
            yield
        finally:
            runtime.stop()

    return lifespan
