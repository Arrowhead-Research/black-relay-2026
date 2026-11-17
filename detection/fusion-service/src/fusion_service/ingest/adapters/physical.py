"""Adapt the versioned team MQTT feed, not the native blackrelay/v1 local feed."""

from fusion_service.domain.errors import Rejected
from fusion_service.ingest.adapters.base import Adapted
from fusion_service.settings import Broker

ENVELOPE_KEYS = {"schema", "id", "timestamp", "site"}


def adapt(payload: dict, broker: Broker) -> Adapted:
    # All messages belong to the configured bridge. Individual sensors remain zone.id.
    # Keep seq/prev_seq and source (iq_remote/control4) inside data, distinct from the
    # fusion cursor and the envelope's broker/topic source object.
    if not broker.physical_node_id:
        raise Rejected("physical_node_id_required")
    data = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    return Adapted(payload["id"], broker.physical_node_id, data)
