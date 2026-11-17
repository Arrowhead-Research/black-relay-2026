from fastapi import APIRouter, Query

from fusion_service.api.dependencies import StoreDep
from fusion_service.api.errors import ErrorResponse
from fusion_service.api.v1.schemas import EventPage
from fusion_service.domain.events import EventType, Subsystem

router = APIRouter(tags=["events"])


@router.get(
    "/events",
    response_model=EventPage,
    responses={
        410: {"model": ErrorResponse},
        400: {"model": ErrorResponse},
    },
)
def events(
    store: StoreDep,
    after: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    site_id: str | None = Query(default=None, max_length=128),
    subsystem: Subsystem | None = None,
    type: EventType | None = None,
    node_id: str | None = Query(default=None, max_length=128),
) -> EventPage:
    page = store.events(
        after,
        limit,
        site_id=site_id,
        subsystem=subsystem,
        type=type,
        node_id=node_id,
    )
    return EventPage(
        events=page.events,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )
