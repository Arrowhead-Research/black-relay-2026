"""The L3 uplink check and the AP-fallback watchdog decision (DESIGN.md 7.4).

Pure logic over an injected probe. Uplink deliberately excludes the broker,
Tailscale, and the central node: an outage there must never reopen the AP.
"""

from __future__ import annotations

from typing import Protocol

from cameraprovision.state import ProvisionState


class UplinkProbe(Protocol):
    def wifi_associated(self) -> bool: ...

    def ipv4_lease(self) -> bool: ...


def uplink_up(probe: UplinkProbe) -> bool:
    """True only when WiFi is associated and a DHCP lease / routable IPv4 exists."""
    return probe.wifi_associated() and probe.ipv4_lease()


def should_relaunch_ap(
    *, state: ProvisionState, seconds_since_boot: float, uplink: bool, timeout_s: float
) -> bool:
    """True only for a ``JOINING`` node with no uplink once the timeout has passed."""
    return state is ProvisionState.JOINING and not uplink and seconds_since_boot >= timeout_s
