"""Turn raw broker messages into stored fusion events: decode, normalize, persist."""

from fusion_service.ingest.contracts import Contracts
from fusion_service.ingest.pipeline import Ingest

__all__ = ["Contracts", "Ingest"]
