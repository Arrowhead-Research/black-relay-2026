from fastapi import APIRouter, Query

from fusion_service.api.dependencies import ClockDep, StoreDep
from fusion_service.api.v1.schemas import NodePage
from fusion_service.domain.events import Subsystem

router = APIRouter(tags=["nodes"])


@router.get("/nodes", response_model=NodePage)
def nodes(
    store: StoreDep,
    clock: ClockDep,
    site_id: str | None = Query(default=None, max_length=128),
    subsystem: Subsystem | None = None,
) -> NodePage:
    return NodePage(nodes=store.nodes(clock(), site_id, subsystem))
