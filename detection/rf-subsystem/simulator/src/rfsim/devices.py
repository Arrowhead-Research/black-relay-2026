"""Emitter profiles, device kits, and identifier generation.

A *profile* describes how one kind of emitter behaves on the air. A *kit* is a
named bundle of profiles that a scenario attaches to an actor (for example a
"phone" is a Wi-Fi client plus a BLE advertiser; a "vehicle" is four TPMS
sensors plus a Bluetooth head unit).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

# Bluetooth SIG company identifiers commonly seen in BLE manufacturer data.
BLE_COMPANY_IDS = {"apple": 0x004C, "samsung": 0x0075, "google": 0x00E0, "microsoft": 0x0006}

PROBE_SSID_POOL = (
    "HomeNet", "HomeNet-5G", "NETGEAR42", "xfinitywifi", "attwifi", "Starbucks WiFi",
    "Linksys", "TP-Link_2.4G", "Hotel Guest", "Airport-Free-WiFi", "Office-Corp", "Spectrum-Setup",
)
TPMS_PROTOCOLS = ("Schrader", "Toyota", "Ford", "Hyundai-VDO", "Citroen", "Renault")
CAR_BT_NAMES = ("CAR MULTIMEDIA", "My Car", "Uconnect", "SYNC", "CarPlay", "Audio System")


@dataclass(frozen=True)
class Profile:
    name: str
    emitter_type: str
    tx_power_dbm: float
    frequencies_mhz: tuple[float, ...]
    period_s: tuple[float, float]  # uniform range between transmissions seen by Kismet
    sensitivity_dbm: float
    path_loss_exponent: float = 2.7
    shadow_sigma_db: float = 4.0
    base_loss: float = 0.1
    randomize_s: float | None = None  # address rotation interval; None = stable address
    requires_motion: bool = False  # only transmits while the actor is moving (TPMS)


WIFI_24 = (2412.0, 2437.0, 2462.0)
WIFI_5 = (5180.0, 5240.0, 5745.0, 5805.0)
BLE_ADV = (2402.0, 2426.0, 2480.0)

PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile("phone_wifi", "wifi_client", 15, WIFI_24 + WIFI_5, (20, 60), -90, randomize_s=900),
        Profile("phone_ble", "ble", 0, BLE_ADV, (4, 8), -95, shadow_sigma_db=6, randomize_s=900),
        Profile("watch_ble", "ble", -4, BLE_ADV, (4, 8), -95, shadow_sigma_db=6, randomize_s=900),
        Profile("tracker_ble", "ble", -4, BLE_ADV, (2, 4), -95, shadow_sigma_db=6, randomize_s=900),
        Profile("hotspot_ap", "wifi_ap", 18, WIFI_24, (5, 10), -90),
        Profile("infra_ap", "wifi_ap", 20, WIFI_24 + WIFI_5, (25, 35), -90, base_loss=0.05),
        Profile("car_bt", "bt_classic", 4, (2441.0,), (10, 20), -92, shadow_sigma_db=5),
        Profile("tpms", "tpms", -25, (315.0,), (15, 60), -95, path_loss_exponent=3.0,
                shadow_sigma_db=5, base_loss=0.2, requires_motion=True),
    )
}

KITS: dict[str, tuple[str, ...]] = {
    "phone": ("phone_wifi", "phone_ble"),
    "smartwatch": ("watch_ble",),
    "tracker": ("tracker_ble",),
    "hotspot": ("hotspot_ap",),
    "access_point": ("infra_ap",),
    "vehicle": ("tpms", "tpms", "tpms", "tpms", "car_bt"),
    "vehicle_no_bt": ("tpms", "tpms", "tpms", "tpms"),
}


def random_mac(rng: random.Random, *, locally_administered: bool) -> str:
    octets = [rng.randrange(256) for _ in range(6)]
    octets[0] &= 0xFC  # unicast, universally administered
    if locally_administered:
        octets[0] |= 0x02
    return ":".join(f"{o:02x}" for o in octets)


@dataclass
class Device:
    """One emitter carried by an actor."""

    device_id: str
    kit: str
    profile: Profile
    static_id: str | None
    attributes: dict[str, Any]
    rotation_key: str
    rotation_phase_s: float
    emitted_ids: dict[str, list[float]] = field(default_factory=dict)  # id -> [first_t, last_t]

    @property
    def randomized(self) -> bool:
        return self.static_id is None

    def emitter_id_at(self, t_s: float) -> str:
        if self.static_id is not None:
            return self.static_id
        assert self.profile.randomize_s is not None
        epoch = int((t_s + self.rotation_phase_s) // self.profile.randomize_s)
        return random_mac(random.Random(f"{self.rotation_key}:{epoch}"), locally_administered=True)


def build_kit(
    rng: random.Random,
    actor_id: str,
    kit: str,
    index: int,
    *,
    randomize: bool | None = None,
    overrides: dict[str, Any] | None = None,
) -> list[Device]:
    """Create the devices for one kit attached to an actor."""
    if kit not in KITS:
        raise ValueError(f"unknown kit {kit!r}; known kits: {sorted(KITS)}")
    overrides = overrides or {}
    vendor = rng.choice(("apple", "samsung", "google"))
    tpms_protocol = rng.choice(TPMS_PROTOCOLS)
    devices = []
    for n, profile_name in enumerate(KITS[kit]):
        profile = PROFILES[profile_name]
        device_id = f"{actor_id}/{kit}{index}/{profile_name}{n}"
        do_randomize = profile.randomize_s is not None if randomize is None else randomize
        if do_randomize and profile.randomize_s is None:
            raise ValueError(f"{device_id}: profile {profile_name!r} does not support address randomization")
        static_id = None
        if not do_randomize:
            if profile.emitter_type == "tpms":
                static_id = f"tpms:{rng.getrandbits(32):08x}"
            else:
                static_id = random_mac(rng, locally_administered=False)
        attrs = _attributes(rng, profile, vendor, tpms_protocol, do_randomize)
        attrs.update(overrides)
        devices.append(
            Device(
                device_id=device_id,
                kit=kit,
                profile=profile,
                static_id=static_id,
                attributes=attrs,
                rotation_key=f"{device_id}:{rng.getrandbits(64)}",
                rotation_phase_s=rng.uniform(0, profile.randomize_s or 1),
            )
        )
    return devices


def _attributes(rng: random.Random, profile: Profile, vendor: str, tpms_protocol: str, randomized: bool) -> dict[str, Any]:
    t = profile.emitter_type
    if t == "wifi_client":
        attrs: dict[str, Any] = {"probed_ssids": sorted(rng.sample(PROBE_SSID_POOL, rng.randint(0, 3)))}
        if not randomized:
            attrs["manufacturer"] = vendor
    elif t == "ble":
        attrs = {"ble_company_id": BLE_COMPANY_IDS[vendor]}
        if profile.name == "watch_ble":
            attrs["device_class"] = "watch"
        elif profile.name == "tracker_ble":
            attrs["device_class"] = "tracker"
    elif t == "wifi_ap":
        if profile.name == "hotspot_ap":
            label = {"apple": "iPhone", "samsung": "Galaxy", "google": "Pixel"}[vendor]
            attrs = {"ssid": f"{label}-{rng.getrandbits(16):04X}", "encryption": "WPA2", "device_class": "mobile_hotspot"}
        else:
            attrs = {"ssid": f"NET-{rng.getrandbits(16):04X}", "encryption": "WPA2"}
    elif t == "bt_classic":
        attrs = {"name": rng.choice(CAR_BT_NAMES), "device_class": "car_audio"}
    elif t == "tpms":
        attrs = {"tpms_protocol": tpms_protocol}
    else:  # pragma: no cover - guarded by PROFILES
        raise ValueError(t)
    attrs["randomized_address"] = randomized
    return attrs
