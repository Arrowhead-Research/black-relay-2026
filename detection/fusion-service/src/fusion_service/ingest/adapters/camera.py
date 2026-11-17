from fusion_service.ingest.adapters.base import Adapted
from fusion_service.settings import Broker

ENVELOPE_KEYS = {"schema", "event_id", "timestamp", "node_id"}


def adapt(payload: dict, broker: Broker) -> Adapted:
    data = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    return Adapted(payload["event_id"], payload["node_id"], data)
