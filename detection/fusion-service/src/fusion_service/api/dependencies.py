"""Typed access to the runtime for route handlers. The only place app.state is read."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from fusion_service.domain.time import Clock
from fusion_service.runtime import Runtime
from fusion_service.snapshots import FrigateSnapshots
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


def get_snapshots(runtime: RuntimeDep) -> FrigateSnapshots:
    if runtime.snapshots is None:
        raise HTTPException(501, "camera_snapshot_password_env is not configured")
    return runtime.snapshots


StoreDep = Annotated[Store, Depends(get_store)]
ClockDep = Annotated[Clock, Depends(get_clock)]
BrokerStatusDep = Annotated[list[dict], Depends(get_broker_status)]
SnapshotsDep = Annotated[FrigateSnapshots, Depends(get_snapshots)]
