"""Observation -> analyzer -> alert engine plumbing, shared by ``run`` and ``live``.

``Pipeline.process`` takes one observation and returns the analytics events
and outbound messages it produced. ``Pipeline.tick`` advances time-based rules
(loiter, node offline) without an observation, which the live Kismet loop uses
so a silent node or a quiet site still produces alerts on the wall clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rfanalytics.analyzer import Analyzer
from rfanalytics.events import Event
from rfanalytics.observation import Observation

if TYPE_CHECKING:
    from rfanalytics.alerts import AlertEngine, Message


class Pipeline:
    def __init__(self, analyzer: Analyzer, engine: AlertEngine | None = None, tick_interval_s: float = 1.0) -> None:
        self.analyzer = analyzer
        self.engine = engine
        self.tick_interval_s = tick_interval_s
        self._next_tick = float("-inf")
        self._last_tick = float("-inf")

    def process(self, obs: Observation) -> tuple[list[Event], list[Message]]:
        messages = self.engine.observe(obs) if self.engine else []
        events = self.analyzer.ingest(obs)
        if self.engine:
            for ev in events:
                messages += self.engine.handle(ev)
            if obs.t >= self._next_tick:
                messages += self.tick(obs.t)
        return events, messages

    def tick(self, now: float) -> list[Message]:
        """Run time-based rules at ``now``. Ticks never move backwards."""
        if not self.engine or now < self._last_tick:
            return []
        self._last_tick = now
        self._next_tick = now + self.tick_interval_s
        return self.engine.tick(now)

    def flush(self) -> tuple[list[Event], list[Message]]:
        events = self.analyzer.flush()
        messages = [m for ev in events for m in self.engine.handle(ev)] if self.engine else []
        return events, messages
