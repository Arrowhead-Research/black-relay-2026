from fastapi import APIRouter

from fusion_service.api.dependencies import BrokerStatusDep, StoreDep
from fusion_service.api.v1.schemas import Health

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health)
def health(store: StoreDep, brokers: BrokerStatusDep) -> Health:
    return Health.assess(store.health(), brokers)
