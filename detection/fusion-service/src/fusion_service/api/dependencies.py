"""Typed access to the runtime for route handlers. The only place app.state is read."""

from typing import Annotated

from fastapi import Depends, Request

from fusion_service.domain.time import Clock
from fusion_service.runtime import Runtime
from fusion_service.storage import Store


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def get_store(runtime: RuntimeDep) -> Store:
    return runtime.store


def get_clock(runtime: RuntimeDep) -> Clock:
    return runtime.clock


def get_broker_status(runtime: RuntimeDep) -> list[dict]:
    return runtime.broker_status()


StoreDep = Annotated[Store, Depends(get_store)]
ClockDep = Annotated[Clock, Depends(get_clock)]
BrokerStatusDep = Annotated[list[dict], Depends(get_broker_status)]
