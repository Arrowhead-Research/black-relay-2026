"""Primary-link beacon: iq-remote -> control4 (panel-side adapters only; one writer, one reader).

Why it exists: the Control4 panel interface must be contacted only when it is actually needed (primary down for
`timers.fallback_after_s`). Deciding that needs the primary link state. Taking it from the normalizer would create a
normalizer->adapter channel. Instead the primary adapter publishes its own link state in a tiny file and the
fallback adapter only reads it. Compose: a dedicated volume, mounted rw in iq-remote and ro in control4; nothing else
mounts it.

Clock independence (a Pi without RTC steps its wall clock at boot / on NTP sync):
  * the file carries NO timestamps: {"v":1,"boot":<random id per writer process>,"ctr":<monotonic counter>,"up":bool}
  * the reader never compares the writer's clock to its own. It remembers the (boot, ctr) pair it last saw and the
    time ON ITS OWN MONOTONIC CLOCK at which that pair last *changed*. A beacon whose pair has not changed for
    STALE_AFTER_S is dead/hung, whatever any wall clock says.
  * an unreadable / torn / non-dict / ill-typed file keeps the previous reading; only UNREADABLE_AFTER consecutive
    failures count as "primary unknown => down".
  * "down for" / "up for" are durations on the reader's monotonic clock since the effective state last flipped
    (a reader that has never seen a live beacon counts from its own start).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Callable

NAME = "iq_remote.beacon"
STALE_AFTER_S = 4.0
UNREADABLE_AFTER = 2


class BeaconWriter:
    def __init__(self, path: str):
        self.path = path
        self.boot = uuid.uuid4().hex
        self.ctr = 0

    def write(self, up: bool) -> None:
        self.ctr += 1
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"v": 1, "boot": self.boot, "ctr": self.ctr, "up": bool(up)}, f)
        os.replace(tmp, self.path)


def _parse(raw: object) -> tuple[str, int, bool] | None:
    if not isinstance(raw, dict):
        return None
    boot, ctr, up = raw.get("boot"), raw.get("ctr"), raw.get("up")
    if not isinstance(boot, str) or not boot or isinstance(ctr, bool) or not isinstance(ctr, int) \
            or not isinstance(up, bool):
        return None
    return boot, ctr, up


class BeaconReader:
    def __init__(self, path: str, now: Callable[[], float] = time.monotonic, stale_after: float = STALE_AFTER_S):
        self.path = path
        self._now = now
        self.stale_after = stale_after
        t = now()
        self._key: tuple[str, int] | None = None
        self._up = False
        self._last_change = t
        self._fails = 0
        self._unreadable = False
        self._eff = False
        self._eff_since = t          # "down since our own start" until a live beacon is seen

    # ------------------------------------------------------------------ polling
    def poll(self) -> None:
        reading = None
        try:
            with open(self.path) as f:
                reading = _parse(json.load(f))
        except (OSError, ValueError):
            reading = None
        t = self._now()
        if reading is None:
            self._fails += 1
            if self._fails >= UNREADABLE_AFTER:
                self._unreadable = True
        else:
            self._fails = 0
            self._unreadable = False
            boot, ctr, up = reading
            if (boot, ctr) != self._key:
                self._key = (boot, ctr)
                self._last_change = t
            self._up = up
        self._settle(t)

    def _compute(self, t: float) -> bool:
        if self._unreadable or self._key is None:
            return False
        if t - self._last_change > self.stale_after:
            return False                              # (boot, ctr) frozen: writer dead or hung
        return self._up

    def _settle(self, t: float) -> None:
        eff = self._compute(t)
        if eff == self._eff:
            return                                    # a second reason to be down does not reset "down since"
        self._eff = eff
        if eff:
            self._eff_since = t
        else:
            stale = (not self._unreadable and self._key is not None and t - self._last_change > self.stale_after)
            self._eff_since = self._last_change if stale else t

    # ------------------------------------------------------------------ queries (poll first)
    def primary_down_for(self) -> float:
        self._settle(self._now())
        return 0.0 if self._eff else max(0.0, self._now() - self._eff_since)

    def primary_up_for(self) -> float:
        self._settle(self._now())
        return max(0.0, self._now() - self._eff_since) if self._eff else 0.0
