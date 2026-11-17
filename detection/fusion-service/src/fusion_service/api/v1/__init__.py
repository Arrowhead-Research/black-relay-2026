"""Version 1 of the consumer API: /api/v1/{events,nodes,health}."""

from fastapi import APIRouter

from fusion_service.api.errors import COMMON_ERROR_RESPONSES
from fusion_service.api.v1 import events, health, nodes

API_VERSION = "v1"

router = APIRouter(prefix=f"/api/{API_VERSION}", responses=COMMON_ERROR_RESPONSES)
router.include_router(events.router)
router.include_router(nodes.router)
router.include_router(health.router)

__all__ = ["API_VERSION", "router"]
