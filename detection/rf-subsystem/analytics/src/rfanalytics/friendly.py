"""Friendly device list (DESIGN.md 7.3.3, CEN-03).

Entries match an exact emitter ID (MAC, BLE address, or ``tpms:<id>``) or a
Wi-Fi network name (SSID, for access points). Friendly observations are
dropped before any processing or storage.

Limitation: devices that randomize their address cannot be excluded by ID.
Fingerprint-based matching is planned with the identity layer (7.3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from rfanalytics.observation import Observation

SOURCES = frozenset({"node-registration", "operator", "tak", "test"})


@dataclass(frozen=True)
class FriendlyEntry:
    label: str
    source: str = "operator"
    emitter_id: str | None = None
    ssid: str | None = None
    expires: datetime | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if (self.emitter_id is None) == (self.ssid is None):
            raise ValueError(f"friendly entry {self.label!r}: set exactly one of emitter_id or ssid")
        if self.source not in SOURCES:
            raise ValueError(f"friendly entry {self.label!r}: unknown source {self.source!r}")
        if self.expires is not None and self.expires.tzinfo is None:
            raise ValueError(f"friendly entry {self.label!r}: expires must include a UTC offset")

    def active_at(self, t: float) -> bool:
        return self.expires is None or t < self.expires.timestamp()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"label": self.label, "source": self.source}
        if self.emitter_id:
            d["emitter_id"] = self.emitter_id
        if self.ssid:
            d["ssid"] = self.ssid
        if self.expires:
            d["expires"] = self.expires.isoformat().replace("+00:00", "Z")
        if self.notes:
            d["notes"] = self.notes
        return d


class FriendlyList:
    def __init__(self, entries: list[FriendlyEntry] | None = None) -> None:
        self._by_id: dict[str, FriendlyEntry] = {}
        self._by_ssid: dict[str, FriendlyEntry] = {}
        for entry in entries or []:
            self.add(entry)

    def __len__(self) -> int:
        return len(self._by_id) + len(self._by_ssid)

    def add(self, entry: FriendlyEntry) -> None:
        if entry.emitter_id is not None:
            self._by_id[entry.emitter_id.lower()] = entry
        else:
            self._by_ssid[entry.ssid] = entry  # type: ignore[index]

    def match(self, obs: Observation) -> FriendlyEntry | None:
        entry = self._by_id.get(obs.emitter_id)
        if entry is None and obs.emitter_type == "wifi_ap":
            ssid = obs.attributes.get("ssid")
            if ssid is not None:
                entry = self._by_ssid.get(ssid)
        if entry is not None and entry.active_at(obs.t):
            return entry
        return None

    def entries(self) -> list[FriendlyEntry]:
        return [*self._by_id.values(), *self._by_ssid.values()]

    def to_yaml(self) -> str:
        return yaml.safe_dump({"entries": [e.to_dict() for e in self.entries()]}, sort_keys=False)


def load_friendly(path: str | Path) -> FriendlyList:
    data = yaml.safe_load(Path(path).read_text()) or {}
    entries = []
    for raw in data.get("entries", []):
        expires = raw.get("expires")
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        entries.append(FriendlyEntry(
            label=raw["label"],
            source=raw.get("source", "operator"),
            emitter_id=raw.get("emitter_id"),
            ssid=raw.get("ssid"),
            expires=expires,
            notes=raw.get("notes", ""),
        ))
    return FriendlyList(entries)
