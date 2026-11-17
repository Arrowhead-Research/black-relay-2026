from typing import NamedTuple, Protocol

from fusion_service.settings import Broker


class Adapted(NamedTuple):
    source_id: str
    node_id: str | None
    data: dict


class Adapter(Protocol):
    def __call__(self, payload: dict, broker: Broker) -> Adapted: ...
