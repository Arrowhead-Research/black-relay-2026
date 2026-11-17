"""Wire adapters for independently authored subsystems: one per subsystem, one interface."""

from fusion_service.domain.events import Subsystem
from fusion_service.ingest.adapters import camera, physical, rf
from fusion_service.ingest.adapters.base import Adapted, Adapter

ADAPTERS: dict[Subsystem, Adapter] = {
    "camera": camera.adapt,
    "rf": rf.adapt,
    "physical": physical.adapt,
}

__all__ = ["ADAPTERS", "Adapted", "Adapter"]
