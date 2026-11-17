"""Wrap an adapted payload in the fusion envelope: site identity, timestamps, provenance."""

from datetime import datetime
from uuid import uuid4

from fusion_service.domain.errors import Rejected
from fusion_service.domain.events import Event, Source
from fusion_service.domain.time import parse_stamp, stamp
from fusion_service.domain.topics import TOPICS
from fusion_service.ingest.adapters import ADAPTERS
from fusion_service.settings import Broker, Settings


def normalize(
    payload: dict, topic: str, broker: Broker, settings: Settings, now: datetime
) -> Event:
    contract = TOPICS[topic]
    if contract.subsystem not in broker.subsystems:
        raise Rejected("subsystem_not_configured")
    adapted = ADAPTERS[contract.subsystem](payload, broker)
    if contract.subsystem in {"rf", "physical"} and "site" in payload:
        site = payload["site"]["id"]
    else:
        site = broker.node_sites.get(adapted.node_id or "") or broker.site_id
    if not isinstance(site, str) or not site.strip() or len(site) > 128:
        raise Rejected("missing_or_invalid_site")
    try:
        occurred = parse_stamp(payload["timestamp"])
    except (ValueError, OverflowError):
        raise Rejected("invalid_timestamp") from None
    if (occurred - now).total_seconds() > settings.future_tolerance_s:
        raise Rejected("future_timestamp")
    return Event(
        cursor=0,
        id=str(uuid4()),
        source_id=adapted.source_id,
        source_schema=payload["schema"],
        timestamp=stamp(occurred),
        received_at=stamp(now),
        site_id=site,
        subsystem=contract.subsystem,
        node_id=adapted.node_id,
        type=contract.event_type,
        data=adapted.data,
        source=Source(broker_id=broker.id, topic=topic, payload=payload),
    )
