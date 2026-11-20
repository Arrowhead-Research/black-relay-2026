from fastapi import APIRouter, HTTPException, Path, Response

from fusion_service.api.dependencies import SnapshotsDep, StoreDep
from fusion_service.api.errors import ErrorResponse
from fusion_service.snapshots.frigate import node_address, snapshot_path

router = APIRouter(tags=["events"])


@router.get(
    "/events/{event_id}/snapshot",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {}}, "description": "The detection's snapshot"},
        404: {"model": ErrorResponse},
        501: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
def snapshot(
    store: StoreDep,
    snapshots: SnapshotsDep,
    event_id: str = Path(max_length=64),
) -> Response:
    """The still image Frigate saved for a camera detection, fetched from the kit on request.

    The kit keeps snapshots for a limited time (7 days as configured), after which this is 404.
    """
    event = store.event(event_id)
    if event is None:
        raise HTTPException(404, "no such event")
    if event.type != "camera.detection" or event.node_id is None:
        raise HTTPException(404, "event has no snapshot")
    path = snapshot_path(event.data.get("attributes", {}).get("snapshot_url"))
    if path is None:
        raise HTTPException(404, "event has no snapshot")
    status = store.node_status(event.site_id, "camera", event.node_id)
    address = None
    if status is not None:
        address = node_address(status.data.get("attributes", {}).get("node_address"))
    if address is None:
        raise HTTPException(404, "node address unknown: no usable heartbeat yet")
    result = snapshots.fetch(address, path)
    if result is None:
        raise HTTPException(404, "snapshot no longer on the node")
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )
