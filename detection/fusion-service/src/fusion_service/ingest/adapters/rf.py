from fusion_service.ingest.adapters.base import Adapted
from fusion_service.settings import Broker

ENVELOPE_KEYS = {"schema", "id", "timestamp", "node_id", "site"}


def adapt(payload: dict, broker: Broker) -> Adapted:
    # Alerts may describe several nodes; only node-status has an unambiguous node_id.
    node_id = payload["node_id"] if payload["schema"] == "rf.node-status.v0" else None
    data = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    return Adapted(payload["id"], node_id, data)
