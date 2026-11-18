"""Composition root: build the long-lived objects and start/stop them in order."""

from dataclasses import dataclass

from fusion_service.domain.time import Clock
from fusion_service.ingest import Contracts, Ingest
from fusion_service.mqtt import Connection
from fusion_service.settings import Settings
from fusion_service.snapshots import FrigateSnapshots
from fusion_service.storage import Maintenance, Store


@dataclass
class Runtime:
    settings: Settings
    clock: Clock
    store: Store
    ingest: Ingest
    connections: list[Connection]
    maintenance: Maintenance
    snapshots: FrigateSnapshots | None

    def start(self, *, start_mqtt: bool = True) -> None:
        self.store.maintain(self.clock())
        self.maintenance.start()
        if start_mqtt:
            for connection in self.connections:
                connection.start()

    def stop(self) -> None:
        for connection in self.connections:
            connection.stop()
        self.maintenance.stop()
        if self.snapshots is not None:
            self.snapshots.close()
        # The store is closed only after every producer thread has finished.
        self.store.close()

    def broker_status(self) -> list[dict]:
        return [connection.snapshot() for connection in self.connections]


def build_runtime(settings: Settings, clock: Clock) -> Runtime:
    contracts = Contracts(settings)
    store = Store(settings)
    try:
        ingest = Ingest(settings, store, contracts, clock)
        connections = [Connection(broker, ingest) for broker in settings.brokers]
        maintenance = Maintenance(store, clock)
        snapshots = None
        if password := settings.camera_snapshot_password():
            snapshots = FrigateSnapshots(
                password.get_secret_value(),
                timeout_s=settings.snapshot_timeout_s,
                max_bytes=settings.max_snapshot_bytes,
            )
    except BaseException:
        store.close()
        raise
    return Runtime(settings, clock, store, ingest, connections, maintenance, snapshots)
