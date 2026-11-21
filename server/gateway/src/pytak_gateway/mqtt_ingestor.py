"""src/pytak_gateway/mqtt_ingestor.py

This is a proof of concept to remove the fusion service and have the gateway
talk directly to the MQT central broker.

"""

import asyncio
import json
import logging
import os
import socket
import ssl

import paho.mqtt.client as mqtt
from pydantic import BaseModel, SecretStr, ValidationError

from .cot_publisher import CotPublisher
from .models import DetectionEvent, HealthReport
from .registry import Registry
from .tak_connection import TakConnection

logger = logging.getLogger(__name__)
MAX_PAYLOAD = 8192  # an event or a health report is a few hundred characters; mosquitto.conf holds the same limit


class MQTTConfig(BaseModel):
    host: str = "localhost"
    port: int = 8883
    topic: str = "nodes/#"
    cafile: str
    username: str
    password: SecretStr


def mqtt_config_from_env() -> MQTTConfig:
    """The broker takes TLS and a login only, so the service refuses to start without both."""
    for name in ("MQTT_TLS_CAFILE", "MQTT_USERNAME", "MQTT_PASSWORD"):
        if not os.getenv(name):
            raise ValueError(f"{name} is not set: the mqtt service needs the broker's CA and its own login")
    return MQTTConfig(
        host=os.getenv("MQTT_HOST", "localhost"),
        port=int(os.getenv("MQTT_PORT", "8883")),
        topic=os.getenv("MQTT_TOPIC", "nodes/#"),
        cafile=os.environ["MQTT_TLS_CAFILE"],
        username=os.environ["MQTT_USERNAME"],
        password=os.environ["MQTT_PASSWORD"],
    )


def node_of_topic(topic: str, subscription: str) -> tuple[str, str, str] | None:
    """The node a message speaks for: the three topic levels right under the subscription's base
    (`nodes/#` and `nodes/b1/camera/cam-01/events` give `("b1", "camera", "cam-01")`: site, subsystem, node).
    None when it cannot be told."""
    if not subscription.endswith("/#"):
        return None
    base = subscription[:-1]  # "nodes/"
    if not topic.startswith(base):
        return None
    levels = topic[len(base) :].split("/")
    return (levels[0], levels[1], levels[2]) if len(levels) >= 3 and all(levels[:3]) else None


def parse_message(payload: str) -> DetectionEvent | HealthReport | None:
    """One message, either shape. A health report carries `up`; anything else must be an event.
    None for anything outside the contract: logged and dropped, never guessed."""
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return HealthReport(**data) if "up" in data else DetectionEvent(**data)
    except (ValueError, ValidationError) as err:
        logger.error(f"Message outside the contract, dropped: {err}")
        return None


async def handle_payload(gateway: CotPublisher, payload: str, topic_node: tuple[str, str, str] | None) -> None:
    """Apply one message, if it speaks for the node whose topic it arrived on. The broker's ACL
    ties a topic to a login, so this is what stops one node speaking for another."""
    if len(payload) > MAX_PAYLOAD:
        logger.warning("message of %d characters on the topic of %r: dropped unread", len(payload), topic_node)
        return
    msg = parse_message(payload)
    if msg is None:
        return
    if topic_node is None or (msg.subsystem.value, msg.node_id) != topic_node[1:]:
        logger.warning(
            "message for %s/%r arrived on the topic of %r: dropped", msg.subsystem.value, msg.node_id[:64], topic_node
        )
        return
    site = topic_node[0]
    if isinstance(msg, HealthReport):
        await gateway.handle_health(site, msg)
    elif isinstance(msg, DetectionEvent):
        await gateway.handle_event(site, msg)


class MQTTIngestor(mqtt.Client):
    def __init__(
        self,
        gateway: CotPublisher,
        mqtt_config: MQTTConfig,
        event_loop: asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self.gateway = gateway
        self.mqtt_config = mqtt_config
        self.event_loop = event_loop

    def on_connect(self, client, userdata, flags, rc):
        logger.info(f"Connected to MQTT broker with result code {rc}")
        client.subscribe(self.mqtt_config.topic)
        logger.info(f"Subscribed to topic: {self.mqtt_config.topic}")

    def on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")

        except UnicodeDecodeError as err:
            logger.error(f"Failed to decode MQTT message: {err}")
            return

        logger.debug(f"Received MQTT message on {msg.topic}")

        if not payload:
            logger.warning("Empty message payload, skipping")
            return

        topic_node = node_of_topic(msg.topic, self.mqtt_config.topic)
        asyncio.run_coroutine_threadsafe(handle_payload(self.gateway, payload, topic_node), self.event_loop)

    def secure(self) -> None:
        """TLS against the broker's CA (the certificate and its hostname are checked) and this
        service's own login. Called before the first connect; there is no plain path."""
        context = ssl.create_default_context(cafile=self.mqtt_config.cafile)  # verifies the chain and the name
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.tls_set_context(context)
        self.username_pw_set(self.mqtt_config.username, self.mqtt_config.password.get_secret_value())

    async def run(self, connection_retry_delay: int = 5):
        self.secure()

        while True:
            try:
                await self.event_loop.run_in_executor(
                    None, self.connect, self.mqtt_config.host, self.mqtt_config.port, 60
                )

                self.loop_start()  # Start mqtt client loop
                logger.info("MQTT ingestor started")

                try:
                    while True:
                        await asyncio.sleep(1)

                except asyncio.CancelledError:
                    logger.info("MQTT ingestor stopping")
                    return

            except (socket.gaierror, ConnectionRefusedError, OSError) as err:
                logger.warning(f"Failed to connect to MQTT broker: {err}.")
                await asyncio.sleep(connection_retry_delay)

            finally:
                self.loop_stop()
                self.disconnect()


async def run_mqtt_ingestor(tak_config: dict[str, str]) -> int:
    try:
        mqtt_config = mqtt_config_from_env()
    except ValueError as err:
        logger.error(str(err))
        return 1

    tak = TakConnection(tak_config)

    if not await tak.start():
        return 1

    gateway = CotPublisher(Registry.load_from_env(), tak.send_many)
    ingestor = MQTTIngestor(gateway, mqtt_config, asyncio.get_event_loop())
    resend = asyncio.create_task(gateway.resend_loop())

    try:
        await ingestor.run()

    except KeyboardInterrupt:
        logger.info("Exiting")

    finally:
        resend.cancel()

    return 0
