"""Application factory and entrypoint. Each line of create_app names the module that owns it."""

import argparse
import logging
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI

from fusion_service import __version__
from fusion_service.api import v1
from fusion_service.api.errors import register_exception_handlers
from fusion_service.api.security import BearerTokenAuth
from fusion_service.domain.time import Clock, utcnow
from fusion_service.ingest import Contracts
from fusion_service.lifespan import build_lifespan
from fusion_service.settings import Settings, default_config_path, load_settings


def create_app(
    settings: Settings | None = None,
    *,
    start_mqtt: bool = True,
    clock: Clock = utcnow,
) -> FastAPI:
    settings = settings or load_settings(default_config_path())
    app = FastAPI(
        title="Black Relay Event Fusion API",
        version=__version__,
        lifespan=build_lifespan(settings, clock=clock, start_mqtt=start_mqtt),
        dependencies=[Depends(BearerTokenAuth.from_settings(settings))],
        redirect_slashes=False,
    )
    register_exception_handlers(app)
    app.include_router(v1.router)
    return app


def main() -> None:
    """`fusion-service [serve|check-config]`; the container ENTRYPOINT."""
    parser = argparse.ArgumentParser(description="Central fusion JSON/REST service")
    parser.add_argument("command", choices=["serve", "check-config"], nargs="?", default="serve")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings(args.config)
        Contracts(settings)
    except Exception as error:
        # Pydantic/YAML exceptions can contain credentials or input; show only the class.
        parser.exit(
            2,
            f"Configuration invalid ({type(error).__name__}); "
            "check settings, schema paths and required environment variables.\n",
        )
    if args.command == "check-config":
        print("Configuration, secrets and reference schemas are valid.")
        return
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
