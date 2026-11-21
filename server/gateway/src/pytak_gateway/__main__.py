import asyncio
import logging
import os
import sys
from urllib.parse import urlparse

from .gateway_server import GatewayServer
from .hash_password import prompt_and_print_hash
from .http_monitor import run_http_monitor
from .mqtt_ingestor import run_mqtt_ingestor

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s]: %(message)s")

logger = logging.getLogger(__name__)


def create_tak_config() -> dict[str, str] | None:
    cot_url = os.getenv("COT_URL")

    if not cot_url:
        logger.warning("COT_URL environment variable not set")
        return None

    config = {"COT_URL": cot_url}

    if urlparse(cot_url).scheme.split("+")[0] == "tls":  # tls:// and tls+wo://
        tls_cert = os.getenv("TLS_CERT")
        tls_key = os.getenv("TLS_KEY")
        tls_password = os.getenv("TLS_PASSWORD")
        tls_cafile = os.getenv("TLS_CAFILE")
        tls_dont_verify = os.getenv("TLS_DONT_VERIFY", "").lower() in (
            "true",
            "1",
            "yes",
        )
        tls_dont_check_hostname = os.getenv("TLS_DONT_CHECK_HOSTNAME", "").lower() in (
            "true",
            "1",
            "yes",
        )

        if tls_cert:
            config["PYTAK_TLS_CLIENT_CERT"] = tls_cert
        if tls_key:
            config["PYTAK_TLS_CLIENT_KEY"] = tls_key
        if tls_password:
            config["PYTAK_TLS_CLIENT_PASSWORD"] = tls_password
        if tls_cafile:
            config["PYTAK_TLS_CLIENT_CAFILE"] = tls_cafile
        if tls_dont_verify:
            config["PYTAK_TLS_DONT_VERIFY"] = "True"
        if tls_dont_check_hostname:
            config["PYTAK_TLS_DONT_CHECK_HOSTNAME"] = "True"

    return config


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m pytak_gateway [http|mqtt|http-monitor|hash-password]")
        return 1

    service = sys.argv[1]
    if service == "hash-password":
        return prompt_and_print_hash()
    tak_config = create_tak_config()

    if service == "http":
        GatewayServer.from_env(tak_config).run()

    elif tak_config is None:
        return 1

    elif service == "mqtt":
        asyncio.run(run_mqtt_ingestor(tak_config))

    elif service == "http-monitor":
        return asyncio.run(run_http_monitor(tak_config))

    else:
        print(f"Unknown service: {service}")
        print("Available services: http, mqtt, http-monitor, hash-password")
        return 1


if __name__ == "__main__":
    sys.exit(main())
