"""Derive a node's real health from Frigate's own MQTT topics (DESIGN.md 5.3).

Frigate publishes three things on the node-local broker that this module reads:

* ``<prefix>/available``: ``online``, ``stopped``, or ``offline`` (retained; the
  broker publishes ``offline`` as Frigate's will message).
* ``<prefix>/stats``: JSON from ``/api/stats``, sent every ``mqtt.stats_interval``.
* ``<prefix>/<camera>/recordings/state``: ``ON`` or ``OFF`` (retained).

**Fail safe.** Every signal starts False and turns True only on positive evidence.
No data, stats older than ``stale_after_s``, or Frigate not ``online`` reports all
three Frigate-derived booleans False. Stats age is measured from local receipt on
a monotonic clock, because a node without NTP sync has no trusted wall clock to
compare Frigate's own timestamps with.

**Threading.** This class holds no lock. The bridge's single lock serializes every
call, like the rest of its shared state.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable, Optional

from .status import NodeState

__all__ = ["FrigateHealth"]


def _positive(value: Any) -> bool:
    """Return True for a real, finite number above zero (a bool is not a measurement)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _decode_text(payload: Any) -> Optional[str]:
    """Decode a raw MQTT payload to text, or ``None`` if it cannot be decoded."""
    if isinstance(payload, str):
        return payload
    try:
        return bytes(payload).decode("utf-8")
    except (TypeError, ValueError):
        return None


class FrigateHealth:
    """Cache Frigate's health topics and evaluate them into a :class:`NodeState`.

    ``camera`` is the Frigate camera name; with none configured the camera and
    recording signals can never be proven, so they stay False. ``monotonic_fn`` is
    injectable for tests.
    """

    def __init__(
        self,
        *,
        camera: Optional[str],
        stale_after_s: float,
        topic_prefix: str = "frigate",
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._camera = camera
        self._stale_after_s = stale_after_s
        self._monotonic_fn = monotonic_fn
        self._available_topic = f"{topic_prefix}/available"
        self.stats_topic = f"{topic_prefix}/stats"
        self._recordings_topic = f"{topic_prefix}/{camera}/recordings/state" if camera else None
        self.reset()

    @property
    def topics(self) -> list[str]:
        """Every topic this module reads, for the local client to subscribe to."""
        topics = [self._available_topic, self.stats_topic]
        if self._recordings_topic:
            topics.append(self._recordings_topic)
        return topics

    def handles(self, topic: str) -> bool:
        return topic in self.topics

    def reset(self) -> None:
        """Forget all cached values. Called when the local broker connection drops.

        ``available`` and ``recordings/state`` are published only on change, so a
        cached value cannot be verified once the connection is gone. Retained copies
        repopulate them on the next subscribe.
        """
        self._available = False
        self._recording_on = False
        self._stats: Optional[dict] = None
        self._stats_received_at: Optional[float] = None

    def update(self, topic: str, payload: Any) -> None:
        """Record one message. A malformed payload counts as no evidence; never raises."""
        if topic == self.stats_topic:
            self._update_stats(payload)
        elif topic == self._available_topic:
            self._available = _decode_text(payload) == "online"
        elif topic == self._recordings_topic:
            self._recording_on = _decode_text(payload) == "ON"

    def _update_stats(self, payload: Any) -> None:
        text = _decode_text(payload)
        try:
            stats = json.loads(text) if text is not None else None
        except ValueError:
            stats = None
        if isinstance(stats, dict):
            self._stats = stats
            self._stats_received_at = float(self._monotonic_fn())
        else:
            self._stats = None
            self._stats_received_at = None

    def _fresh_stats(self) -> Optional[dict]:
        """Return the stats if Frigate is online and they are fresh, else ``None``."""
        if not self._available or self._stats is None or self._stats_received_at is None:
            return None
        if float(self._monotonic_fn()) - self._stats_received_at > self._stale_after_s:
            return None
        return self._stats

    def _camera_online(self, stats: dict) -> bool:
        cameras = stats.get("cameras")
        if not isinstance(cameras, dict):
            return False
        camera = cameras.get(self._camera)
        return isinstance(camera, dict) and _positive(camera.get("camera_fps"))

    @staticmethod
    def _detector_ok(stats: dict) -> bool:
        """True when at least one detector exists and every one of them is reporting."""
        detectors = stats.get("detectors")
        if not isinstance(detectors, dict) or not detectors:
            return False
        return all(
            isinstance(detector, dict)
            and _positive(detector.get("inference_speed"))
            and bool(detector.get("pid"))
            for detector in detectors.values()
        )

    def evaluate(self, *, link_online: bool) -> NodeState:
        """Lower the cached topics to the four health booleans."""
        stats = self._fresh_stats()
        if stats is None:
            return NodeState(
                camera_online=False,
                link_online=link_online,
                recording_ok=False,
                detector_ok=False,
            )
        return NodeState(
            camera_online=self._camera_online(stats),
            link_online=link_online,
            recording_ok=self._recording_on,
            detector_ok=self._detector_ok(stats),
        )
