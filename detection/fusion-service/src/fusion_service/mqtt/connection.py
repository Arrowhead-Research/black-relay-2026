"""One paho client per broker: connect, subscribe, hand messages to Ingest, ack, reconnect."""

import logging
import threading
import time

import paho.mqtt.client as mqtt

from fusion_service.domain.time import stamp
from fusion_service.domain.topics import TOPICS, topics_for
from fusion_service.ingest.pipeline import Ingest
from fusion_service.settings import Broker, required_env

log = logging.getLogger(__name__)


class Connection:
    def __init__(self, broker: Broker, ingest: Ingest, client_factory=mqtt.Client):
        self.broker, self.ingest = broker, ingest
        self.stop_event, self.retry = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.state = {
            "id": broker.id,
            "connected": False,
            "subscribed": False,
            "last_message_at": None,
            "last_error": None,
            "connections": 0,
        }
        self.client = client_factory(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=broker.client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
            manual_ack=True,
        )
        self.client.connect_timeout = 5
        if broker.username_env:
            self.client.username_pw_set(
                required_env(broker.username_env),
                required_env(broker.password_env),
            )
        if broker.tls_ca:
            self.client.tls_set(
                ca_certs=str(broker.tls_ca),
                certfile=str(broker.tls_cert) if broker.tls_cert else None,
                keyfile=str(broker.tls_key) if broker.tls_key else None,
            )
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message
        self.thread = threading.Thread(target=self.run, name=f"mqtt-{broker.id}", daemon=True)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self) -> dict:
        with self.lock:
            result = dict(self.state)
        result["counts"] = self.ingest.counters(self.broker.id)
        return result

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.update(last_error="connection_refused")
            self.retry.set()
            return
        with self.lock:
            self.state.update(connected=True, subscribed=False, last_error=None)
            self.state["connections"] += 1
        topics = topics_for(self.broker.subsystems)
        unwanted = [topic for topic in TOPICS if topic not in topics]
        if unwanted:
            client.unsubscribe(unwanted)
        rc, _ = client.subscribe([(topic, 1) for topic in topics])
        if rc != mqtt.MQTT_ERR_SUCCESS:
            self.update(last_error="subscribe_failed")
            self.retry.set()

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        good = bool(reason_codes) and all(code.value < 128 for code in reason_codes)
        self.update(subscribed=good, last_error=None if good else "subscription_refused")
        if not good:
            self.retry.set()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.update(connected=False, subscribed=False)

    def on_message(self, client, userdata, message):
        self.update(last_message_at=stamp(self.ingest.clock()))
        if self.ingest.handle(self.broker, message.topic, message.payload):
            if message.qos > 0 and client.ack(message.mid, message.qos) != mqtt.MQTT_ERR_SUCCESS:
                self.update(last_error="ack_failed")
                self.retry.set()
        else:
            self.update(last_error="ingest_unavailable")
            self.retry.set()

    def run(self):
        delay = 1
        while not self.stop_event.is_set():
            self.retry.clear()
            started = time.monotonic()
            try:
                self.client.connect(self.broker.host, self.broker.port, keepalive=30)
                while not self.stop_event.is_set() and not self.retry.is_set():
                    if self.client.loop(timeout=0.2) != mqtt.MQTT_ERR_SUCCESS:
                        self.update(last_error="connection_lost")
                        break
            except Exception:
                self.update(last_error="connection_failed")
                log.warning("MQTT connection unavailable broker=%s", self.broker.id)
            finally:
                self.client.disconnect()
                self.update(connected=False, subscribed=False)
            if time.monotonic() - started > 60:
                delay = 1
            self.stop_event.wait(delay)
            delay = min(30, delay * 2)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        # The store is closed only after all callbacks finish.
        if self.thread.is_alive():
            self.thread.join()
