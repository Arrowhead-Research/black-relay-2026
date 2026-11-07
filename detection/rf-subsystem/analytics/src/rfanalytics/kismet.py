"""Kismet 2026-10-R1 REST ingest adapter -> ``rf.observation.v0``.

This polls Kismet's device views and normalizes devices into the observation
contract consumed by ``rfanalytics run`` (DESIGN 7.3.1). It is intentionally
small and stateful only for deduplication: live analytics still happens in
``rfanalytics run``.

Notes / current limitations (validate against your Kismet build and capture):
- **Per-node RSSI:** Kismet's aggregate device signal is widely populated, but
  per-seenby (per datasource/node) RSSI may be absent. When per-seenby RSSI is
  unavailable, this adapter emits one observation per seeing node using the
  aggregate ``last_signal`` and sets ``attributes.rssi_is_aggregate=true``.
  Zone localization benefits from true per-node RSSI; improve this when
  per-source signal is available.
- **TPMS:** RTL433/TPMS device field shapes vary by Kismet/rtl_433 version.
  Mapping is best-effort and should be verified with real TPMS decodes.
- **Auth/config:** credentials are read from CLI args or environment variables;
  never commit them.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from rfanalytics.observation import SCHEMA_ID

_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class KismetIngestError(RuntimeError):
    pass


@dataclass
class _SeenState:
    last_time: float = 0.0


@dataclass
class KismetPoller:
    """Poll Kismet and yield new ``rf.observation.v0`` records."""

    base_url: str
    username: str | None = None
    password: str | None = None
    # (emitter_id, node_id) -> last seenby/device time emitted
    state: dict[tuple[str, str], _SeenState] = field(default_factory=dict)

    def fetch_devices(self, since: float | None = None) -> list[dict[str, Any]]:
        path = "/devices/views/all/devices.json"
        params = {"fields": ",".join(_FIELDS)}
        if since is not None and since > 0:
            path = f"/devices/last-time/{since}/devices.json"
        url = self.base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        if self.username is not None or self.password is not None:
            token = base64.b64encode(f"{self.username or ''}:{self.password or ''}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KismetIngestError(f"Kismet request failed: {exc}") from exc
        if not isinstance(data, list):
            raise KismetIngestError(f"expected device list, got {type(data).__name__}")
        return data

    def observations_from_device(self, device: dict[str, Any]) -> list[dict[str, Any]]:
        phy = device.get("kismet.device.base.phyname") or ""
        mac = str(device.get("kismet.device.base.macaddr") or "").lower()
        dev_type = str(device.get("kismet.device.base.type") or "")
        emitter_type = _emitter_type(phy, dev_type, device)
        emitter_id = _emitter_id(emitter_type, mac, device)
        if emitter_type is None or emitter_id is None:
            return []

        freq = _frequency_mhz(device)
        agg_rssi = _last_signal(device.get("kismet.device.base.signal"))
        base_ts = float(device.get("kismet.device.base.last_time") or device.get("kismet.device.base.mod_time") or 0)
        attrs = _attributes(emitter_type, device, mac)
        if not isinstance(attrs, dict):
            attrs = {}

        out: list[dict[str, Any]] = []
        for sb in device.get("kismet.device.base.seenby") or []:
            source = (sb.get("kismet.common.seenby.source") or {})
            src_name = str(source.get("kismet.datasource.name") or source.get("kismet.datasource.interface") or "")
            node_id = _node_id(src_name)
            if not node_id:
                continue
            sb_ts = float(sb.get("kismet.common.seenby.last_time") or source.get("kismet.datasource.last_time") or base_ts or 0)
            if sb_ts <= 0:
                continue
            key = (emitter_id, node_id)
            prev = self.state.get(key)
            if prev and sb_ts <= prev.last_time:
                continue
            sb_rssi = _last_signal(sb.get("kismet.common.seenby.signal"))
            rssi = sb_rssi if sb_rssi is not None else agg_rssi
            if rssi is None:
                continue
            rec_attrs = dict(attrs)
            if sb_rssi is None:
                rec_attrs["rssi_is_aggregate"] = True
            rec = {
                "schema": SCHEMA_ID,
                "timestamp": _iso_utc(sb_ts),
                "node_id": node_id,
                "emitter_type": emitter_type,
                "emitter_id": emitter_id,
                "rssi_dbm": int(max(-130, min(0, round(rssi)))),
                "frequency_mhz": float(freq) if freq and freq > 0 else _default_freq(emitter_type),
                "attributes": rec_attrs,
            }
            out.append(rec)
            self.state[key] = _SeenState(last_time=sb_ts)
        return out

    def poll(self, since: float | None = None) -> tuple[list[dict[str, Any]], float]:
        devices = self.fetch_devices(since=since)
        obs: list[dict[str, Any]] = []
        max_t = since or 0.0
        for dev in devices:
            obs.extend(self.observations_from_device(dev))
            t = float(dev.get("kismet.device.base.last_time") or dev.get("kismet.device.base.mod_time") or 0)
            if t > max_t:
                max_t = t
        obs.sort(key=lambda r: (r["timestamp"], r["node_id"], r["emitter_id"]))
        return obs, max_t


_FIELDS = [
    "kismet.device.base.key",
    "kismet.device.base.macaddr",
    "kismet.device.base.phyname",
    "kismet.device.base.type",
    "kismet.device.base.name",
    "kismet.device.base.commonname",
    "kismet.device.base.manuf",
    "kismet.device.base.frequency",
    "kismet.device.base.channel",
    "kismet.device.base.first_time",
    "kismet.device.base.last_time",
    "kismet.device.base.mod_time",
    "kismet.device.base.signal/kismet.common.signal.last_signal",
    "kismet.device.base.signal/kismet.common.signal.type",
    "kismet.device.base.seenby",
    "dot11.device/dot11.device.last_beaconed_ssid",
    "dot11.device/dot11.device.last_probed_ssid_record/dot11.probedssid.ssid",
    "bluetooth.device",
    "rtl433.device",
    "sensor.device",
]


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_signal(signal: Any) -> int | None:
    if not isinstance(signal, dict):
        return None
    v = signal.get("kismet.common.signal.last_signal")
    if v in (None, 0) and signal.get("kismet.common.signal.type") in (None, "", "none"):
        return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    if iv == 0:
        return None
    return iv


def _node_id(datasource_name: str) -> str | None:
    # "wbt-1:wifi" -> "wbt-1"; also accept plain node-like names.
    name = datasource_name.strip()
    if not name:
        return None
    node = name.split(":", 1)[0].strip().lower()
    if not node:
        return None
    node = re.sub(r"[^a-z0-9-]+", "-", node).strip("-")
    if not node or not re.match(r"^[a-z0-9][a-z0-9-]*$", node):
        return None
    return node


def _emitter_type(phy: str, dev_type: str, device: dict[str, Any]) -> str | None:
    p = phy.lower()
    t = dev_type.lower()
    if "rtl433" in p or "rtl_433" in p or "433" in p and "rtl" in p or device.get("rtl433.device"):
        return "tpms"
    if "bluetooth" in p or "btle" in p or "br/edr" in t or "btle" in t:
        if "br/edr" in t or "classic" in t:
            return "bt_classic"
        if "btle" in t or "le" in t:
            return "ble"
        # Kismet sometimes reports generic Bluetooth; prefer BLE for advertising-heavy views.
        if device.get("bluetooth.device"):
            bd = device.get("bluetooth.device") or {}
            btype = str(bd.get("bluetooth.device.le_limited_discoverable", "")) + str(bd.get("bluetooth.device.type", ""))
            if "br" in btype.lower():
                return "bt_classic"
        return "ble"
    if "802.11" in p or "wifi" in p or "dot11" in p or device.get("dot11.device"):
        if "ap" in t or "access point" in t or "infrastructure" in t:
            return "wifi_ap"
        if "client" in t or "wired" in t or "ad-hoc" in t:
            return "wifi_client"
        # Heuristic: beacons/probed SSID presence often indicates AP; fall back to client for ambiguous.
        if (device.get("dot11.device") or {}).get("dot11.device.last_beaconed_ssid"):
            return "wifi_ap"
        return "wifi_client"
    return None


def _emitter_id(emitter_type: str, mac: str, device: dict[str, Any]) -> str | None:
    if emitter_type == "tpms":
        tid = _tpms_id(device)
        return f"tpms:{tid}" if tid else None
    if _MAC.match(mac):
        return mac
    return None


def _tpms_id(device: dict[str, Any]) -> str | None:
    # Best-effort: rtl433/sensor structures vary. Search common id fields.
    candidates = []
    for container_key in ("rtl433.device", "sensor.device", "kismet.device.base"):
        c = device.get(container_key)
        if isinstance(c, dict):
            candidates.append(c)
    if isinstance(device.get("rtl433.device"), dict):
        candidates.extend([v for v in device["rtl433.device"].values() if isinstance(v, dict)])
    for c in candidates:
        for key in ("rtl433.device.id", "rtl433.device.common.id", "sensor.device.id", "id", "kismet.common.id"):
            v = c.get(key)
            if v is None:
                continue
            if isinstance(v, int):
                return f"{v & 0xFFFFFFFF:08x}"
            s = str(v).strip().lower().replace("0x", "").replace(":", "")
            if re.fullmatch(r"[0-9a-f]{1,8}", s):
                return s.zfill(8)
    # Fallback: derive from MAC-like commonname if present.
    cn = str(device.get("kismet.device.base.commonname") or "").lower().replace(":", "")
    if re.fullmatch(r"[0-9a-f]{8}", cn):
        return cn
    return None


def _frequency_mhz(device: dict[str, Any]) -> float:
    f = device.get("kismet.device.base.frequency")
    try:
        fv = float(f)
    except (TypeError, ValueError):
        return 0.0
    # Kismet may report kHz in some contexts; values > 100000 are kHz.
    if fv > 100000:
        return fv / 1000.0
    if 1 <= fv < 1000 and "rtl433" in str(device.get("kismet.device.base.phyname") or "").lower():
        # e.g. 315
        return fv
    return fv


def _default_freq(emitter_type: str) -> float:
    return {"wifi_ap": 2412.0, "wifi_client": 2412.0, "bt_classic": 2402.0, "ble": 2402.0, "tpms": 315.0}.get(
        emitter_type, 2402.0
    )


def _attributes(emitter_type: str, device: dict[str, Any], mac: str) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    manuf = device.get("kismet.device.base.manuf")
    if isinstance(manuf, str) and manuf:
        attrs["manufacturer"] = manuf
    name = device.get("kismet.device.base.name") or device.get("kismet.device.base.commonname")
    if isinstance(name, str) and name and name.lower() != mac:
        attrs["name"] = name
    if emitter_type in ("wifi_ap", "wifi_client"):
        if _is_randomized_mac(mac):
            attrs["randomized_address"] = True
        dot11 = device.get("dot11.device") or {}
        ssid = None
        b = dot11.get("dot11.device.last_beaconed_ssid_record") or {}
        if isinstance(b, dict):
            ssid = b.get("dot11.advertisedssid.ssid")
        if not ssid:
            ssid = dot11.get("dot11.device.last_beaconed_ssid")
        if isinstance(ssid, str) and ssid:
            attrs["ssid"] = ssid
    elif emitter_type in ("ble", "bt_classic"):
        if _is_randomized_mac(mac):
            attrs["randomized_address"] = True
    elif emitter_type == "tpms":
        r = device.get("rtl433.device") or {}
        # flatten a few common rtl433 fields if present
        for k in list(r.keys())[:50]:
            lk = str(k).lower()
            if lk.endswith("model") and isinstance(r[k], str):
                attrs.setdefault("tpms_protocol", r[k])
            if "pressure" in lk and isinstance(r[k], (int, float)):
                attrs.setdefault("pressure_kpa", float(r[k]))
            if "temperature" in lk and isinstance(r[k], (int, float)):
                attrs.setdefault("temperature_c", float(r[k]))
    return attrs


def _is_randomized_mac(mac: str) -> bool:
    # Locally administered bit in first octet.
    if not _MAC.match(mac):
        return False
    try:
        first = int(mac.split(":")[0], 16)
    except ValueError:
        return False
    return bool(first & 0x02)
