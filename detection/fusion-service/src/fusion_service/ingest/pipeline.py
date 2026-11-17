"""Decode, normalize and store one message; report whether it may be acknowledged."""

import logging
import threading
from collections import Counter

from fusion_service.domain.errors import Rejected, StorageUnavailable, Unsupported
from fusion_service.domain.time import Clock, utcnow
from fusion_service.ingest.contracts import Contracts
from fusion_service.ingest.normalize import normalize
from fusion_service.settings import Broker, Settings
from fusion_service.storage.store import Store

log = logging.getLogger(__name__)


class Ingest:
    def __init__(
        self, settings: Settings, store: Store, contracts: Contracts, clock: Clock = utcnow
    ):
        self.settings, self.store, self.contracts, self.clock = settings, store, contracts, clock
        self.lock = threading.Lock()
        self.counts = {broker.id: Counter() for broker in settings.brokers}

    def count(self, broker_id: str, name: str) -> None:
        with self.lock:
            self.counts[broker_id][name] += 1

    def counters(self, broker_id: str) -> dict:
        with self.lock:
            return dict(self.counts[broker_id])

    def handle(self, broker: Broker, topic: str, raw: bytes) -> bool:
        """True: durably stored, duplicate, or permanent rejection; QoS 1 may be acknowledged."""
        try:
            payload = self.contracts.decode(topic, raw)
            event = normalize(payload, topic, broker, self.settings, self.clock())
            inserted = self.store.insert(event, broker.namespace)
            self.count(broker.id, "accepted" if inserted else "duplicate")
        except Unsupported as error:
            self.count(broker.id, "unsupported")
            log.warning("MQTT input rejected broker=%s reason=%s", broker.id, error)
        except Rejected as error:
            self.count(broker.id, "rejected")
            log.warning("MQTT input rejected broker=%s reason=%s", broker.id, error)
        except StorageUnavailable:
            self.count(broker.id, "storage_failure")
            log.warning(
                "MQTT storage unavailable broker=%s; reconnecting without PUBACK", broker.id
            )
            return False
        except Exception:
            # No payload, exception text or traceback: validators may embed input values.
            self.count(broker.id, "internal_error")
            log.error("MQTT ingest failed broker=%s; reconnecting without PUBACK", broker.id)
            return False
        return True
