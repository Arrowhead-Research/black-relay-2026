"""Derive a node's real health from Frigate's own MQTT topics (DESIGN.md 5.3).

Frigate publishes three things on the node-local broker that this module reads:

* ``<prefix>/available``: ``online``, ``stopped``, or ``offline`` (retained; the
  broker publishes ``offline`` as Frigate's will message).
* ``<prefix>/stats``: JSON from ``/api/stats``, sent every ``mqtt.stats_interval``.
* ``<prefix>/<camera>/recordings/state``: ``ON`` or ``OFF`` (retained).

Two more sources sit beside them. ``camera-link``'s state file gives the camera link's
state and any detector failure it found in Frigate's log, and ``wlan0`` gives the address
the central node reaches this node on.

**Fail safe.** Every signal starts False and turns True only on positive evidence.
No data, stale stats, or Frigate not ``online`` reports all three Frigate-derived
booleans False. Stats are stale by either of two measures: their age from local
receipt on a monotonic clock, which needs no trusted wall clock, or Frigate's own
``service.last_updated``, so a publisher repeating frozen stats cannot look current.
Frigate shares this host's clock, so the two timestamps are comparable, and a clock
step can only report False until the next stats message.

**detector_ok.** Frigate gives no direct sign of a working detector. A crashed detector
keeps its pid and its last speed, one that never ran reports the starting value, and one
timing out on every frame reports a speed with zeros for results. So a true needs detection
on, a speed since this Frigate started that is neither the starting value nor a timeout's,
no detection stuck in progress, and a fresh ``camera-link`` report saying it read Frigate's
log recently and found no failure. Without that report nothing can see a crash, so a missing,
corrupt or stale one means False. A quiet scene after a fresh start reports False until the
first real inference. That is accepted: an idle scene gives no evidence.

**recording_ok.** Frigate's recordings state is a setting, and ``camera_fps`` is the detect
stream's. Only ``camera-link`` can see segments being written, so a true also needs a fresh
report of ``record_writing``.

**Threading.** This class holds no lock. The bridge's single lock serializes every
call, like the rest of its shared state.
"""

from __future__ import annotations

import fcntl
import json
import math
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .status import NodeState

__all__ = ["FrigateHealth", "interface_ipv4"]

# The inference speed Frigate reports before its first inference, in milliseconds. Frigate
# v0.18.2: frigate/object_detection/base.py:331 starts avg_inference_speed at 0.01 s, and
# frigate/stats/util.py:155 reports it times 1000.
_INITIAL_INFERENCE_SPEED = 10.0
# At or above this, the average holds a timeout. Frigate v0.18.2:
# frigate/detectors/plugins/hailo8l.py:343 waits 1 s and then returns zeros, and
# frigate/object_detection/base.py:182 averages the 1000 ms in at one tenth, so whatever the
# average was, it is now at least 100 ms. Kit 1's Hailo runs at 7.65 ms.
_INFERENCE_SPEED_CEILING = 100.0
# A detection in progress for longer is stuck. Frigate v0.18.2: frigate/watchdog.py:124, whose
# restart keeps the speed, so nothing else in the stats shows it.
_DETECTION_STUCK_S = 10.0
# Frigate's uptime and last_updated are separate whole-second readings of the clock.
_START_TIME_TOLERANCE_S = 2.0
# Older than this, camera-link's state file says nothing, for or against.
_LINK_STATE_MAX_AGE_S = 300.0
# camera-link reads Frigate's log every 30 s. Older than this, it has missed three reads, and
# a clean result is only the value it started with.
_LOG_READ_MAX_AGE_S = 90.0
# camera-link rewrites its report every few seconds. Older than this, a stream may have died with
# nobody to say so, as with the log read above.
_RECORD_REPORT_MAX_AGE_S = 90.0
# NetworkManager's address for the setup AP, which is no way to reach this node.
_SETUP_AP_IP = "10.42.0.1"
_SIOCGIFADDR = 0x8915


def _is_number(value: Any) -> bool:
    """Return True for a real, finite number (a bool is not a measurement)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float, which JSON can carry
        return False


def _positive(value: Any) -> bool:
    """Return True for a real, finite number above zero."""
    return _is_number(value) and value > 0


def _is_inference_speed(value: Any) -> bool:
    """Return True for a speed that holds no timeout. The starting value is the caller's."""
    return _positive(value) and value < _INFERENCE_SPEED_CEILING


def _is_stuck(detection_start: Any, stats_updated: float) -> bool:
    """Timed against the stats' own clock. An unreadable start cannot show it is not stuck."""
    if not _is_number(detection_start):
        return True
    return detection_start > 0 and stats_updated - detection_start > _DETECTION_STUCK_S


def _decode_text(payload: Any) -> Optional[str]:
    """Decode a raw MQTT payload to text, or ``None`` if it cannot be decoded."""
    if isinstance(payload, str):
        return payload
    try:
        return bytes(payload).decode("utf-8")
    except (TypeError, ValueError):
        return None


def _read_ipv4(interface: str) -> Optional[str]:
    request = struct.pack("256s", interface.encode()[:15])
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            reply = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, request)
    except OSError:  # no such interface, or it has no address
        return None
    return socket.inet_ntoa(reply[20:24])


def interface_ipv4(interface: str) -> Optional[str]:
    """Return the IPv4 address the central node can reach this node on, if it has one."""
    address = _read_ipv4(interface)
    return None if address == _SETUP_AP_IP else address


@dataclass(frozen=True)
class _LinkReport:
    """What a believable camera-link state file says."""

    state: str
    retry_after: Optional[float]
    reason: Optional[str]
    # It read Frigate's log recently and found no failure.
    detector_clean: bool
    record_writing: bool


def _parse_link_report(content: bytes, now: float) -> Optional[_LinkReport]:
    """Return the report in a state file's ``content``, or ``None`` if it says nothing usable."""
    try:
        report = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(report, dict):
        return None
    state, updated = report.get("state"), report.get("updated")
    if not state or not isinstance(state, str) or not _is_number(updated):
        return None
    if abs(now - updated) > _LINK_STATE_MAX_AGE_S:
        return None
    retry_after, reason = report.get("retry_after"), report.get("reason")
    read_at = report.get("log_read_at")
    return _LinkReport(
        state=state,
        retry_after=retry_after if _is_number(retry_after) else None,
        reason=reason if isinstance(reason, str) else None,
        detector_clean=(
            report.get("detector_error") is False
            and _positive(read_at)
            and abs(now - read_at) <= _LOG_READ_MAX_AGE_S
        ),
        record_writing=(
            report.get("record_writing") is True
            and abs(now - updated) <= _RECORD_REPORT_MAX_AGE_S
        ),
    )


class FrigateHealth:
    """Cache Frigate's health topics and evaluate them into a :class:`NodeState`.

    ``camera`` is the Frigate camera name; with none configured no signal that reads
    the camera's stats can be proven, so they stay False. ``link_state_path`` is
    ``camera-link``'s state file, and ``address_fn`` gives the node's address. A file that
    cannot be read and an address that cannot be found leave their attributes out, and
    prove nothing. The clocks are injectable for tests.
    """

    def __init__(
        self,
        *,
        camera: Optional[str],
        stale_after_s: float,
        topic_prefix: str = "frigate",
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_fn: Callable[[], float] = time.time,
        link_state_path: Optional[Path] = None,
        address_fn: Callable[[], Optional[str]] = lambda: None,
    ) -> None:
        self._camera = camera
        self._stale_after_s = stale_after_s
        self._monotonic_fn = monotonic_fn
        self._wall_fn = wall_fn
        self._link_state_path = link_state_path
        self._address_fn = address_fn
        # Survive a lost local broker: Frigate's running average does too, so its value
        # still tells the same story afterwards.
        self._frigate_started: Optional[float] = None
        self._inferred: set[str] = set()
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
            if not self._available:
                self._forget_inference()
        elif topic == self._recordings_topic:
            self._recording_on = _decode_text(payload) == "ON"

    def _update_stats(self, payload: Any) -> None:
        text = _decode_text(payload)
        try:
            stats = json.loads(text) if text is not None else None
        except (ValueError, RecursionError):
            stats = None
        if isinstance(stats, dict):
            self._stats = stats
            self._stats_received_at = float(self._monotonic_fn())
            self._note_inference(stats)
        else:
            self._stats = None
            self._stats_received_at = None

    def _forget_inference(self) -> None:
        self._frigate_started = None
        self._inferred.clear()

    def _note_inference(self, stats: dict) -> None:
        """Keep which detectors have left the starting value since this Frigate started.

        Kept rather than read off each message, because the running average of a detector
        really taking 10 ms can land on the starting value.
        """
        started = self._start_time(stats)
        if (
            started is None
            or self._frigate_started is not None
            and abs(started - self._frigate_started) > _START_TIME_TOLERANCE_S
        ):
            self._forget_inference()
        self._frigate_started = started
        detectors = stats.get("detectors")
        if isinstance(detectors, dict):
            for name, detector in detectors.items():
                speed = detector.get("inference_speed") if isinstance(detector, dict) else None
                if _is_inference_speed(speed) and speed != _INITIAL_INFERENCE_SPEED:
                    self._inferred.add(name)

    @staticmethod
    def _service(stats: dict) -> dict:
        service = stats.get("service")
        return service if isinstance(service, dict) else {}

    def _start_time(self, stats: dict) -> Optional[float]:
        service = self._service(stats)
        updated, uptime = service.get("last_updated"), service.get("uptime")
        return float(updated - uptime) if _is_number(updated) and _is_number(uptime) else None

    def _fresh_stats(self) -> Optional[dict]:
        """Return the stats if Frigate is online and they are fresh, else ``None``."""
        if not self._available or self._stats is None or self._stats_received_at is None:
            return None
        if float(self._monotonic_fn()) - self._stats_received_at > self._stale_after_s:
            return None
        updated = self._service(self._stats).get("last_updated")
        if not _positive(updated) or abs(float(self._wall_fn()) - updated) > self._stale_after_s:
            return None
        return self._stats

    def _camera_stats(self, stats: dict) -> dict:
        cameras = stats.get("cameras")
        camera = cameras.get(self._camera) if isinstance(cameras, dict) else None
        return camera if isinstance(camera, dict) else {}

    def _camera_online(self, stats: dict) -> bool:
        return _positive(self._camera_stats(stats).get("camera_fps"))

    @staticmethod
    def _recorder_running(stats: dict) -> bool:
        processes = stats.get("processes")
        recorder = processes.get("recording") if isinstance(processes, dict) else None
        return isinstance(recorder, dict) and _positive(recorder.get("pid"))

    def _detector_ok(self, stats: dict) -> bool:
        """True when detection is on and every detector has really inferred, and still reports."""
        if self._camera_stats(stats).get("detection_enabled") is not True:
            return False
        detectors = stats.get("detectors")
        if not isinstance(detectors, dict) or not detectors:
            return False
        updated = self._service(stats).get("last_updated")
        return all(
            isinstance(detector, dict)
            and name in self._inferred
            and _is_inference_speed(detector.get("inference_speed"))
            and _positive(detector.get("pid"))
            and not _is_stuck(detector.get("detection_start"), updated)
            for name, detector in detectors.items()
        )

    def _link_report(self) -> Optional[_LinkReport]:
        if self._link_state_path is None:
            return None
        try:
            content = self._link_state_path.read_bytes()
        except OSError:
            return None  # camera-link is not running, which the heartbeat shows by omission
        return _parse_link_report(content, float(self._wall_fn()))

    def evaluate(self, *, link_online: bool) -> NodeState:
        """Lower the cached topics and the two other sources to a heartbeat's state."""
        report = self._link_report()
        stats = self._fresh_stats()
        camera_online = stats is not None and self._camera_online(stats)
        return NodeState(
            camera_online=camera_online,
            link_online=link_online,
            # The recordings state is Frigate's setting: it stays ON with no frames or disk.
            recording_ok=(
                stats is not None
                and self._recording_on
                and camera_online
                and self._recorder_running(stats)
                and report is not None
                and report.record_writing
            ),
            detector_ok=(
                stats is not None
                and self._detector_ok(stats)
                and report is not None
                and report.detector_clean
            ),
            camera_link_state=report.state if report else None,
            retry_after=report.retry_after if report else None,
            reason=report.reason if report else None,
            node_address=self._address_fn(),
        )
