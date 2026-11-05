"""The provisioning state machine, as a pure transition function.

See DESIGN.md 7.4. The rules that matter most:

* A ``PROVISIONED`` node never reopens the AP on link loss. Only the physical
  re-provision button does that.
* A node with a saved config must not stay on the fallback AP. It alternates
  between the AP and rejoin attempts, so it recovers by itself when the router
  returns after a power cut.
"""

from __future__ import annotations

from enum import Enum


class ProvisionState(Enum):
    UNPROVISIONED = "unprovisioned"
    AP_UP = "ap_up"
    JOINING = "joining"
    PROVISIONED = "provisioned"
    FALLBACK_AP = "fallback_ap"


class ProvisionEvent(Enum):
    BOOT = "boot"
    CONFIG_RECEIVED = "config_received"
    UPLINK_UP = "uplink_up"
    UPLINK_TIMEOUT = "uplink_timeout"
    REPROVISION_BUTTON = "reprovision_button"
    AP_WINDOW_ELAPSED = "ap_window_elapsed"


_S = ProvisionState
_E = ProvisionEvent

# (current, event) -> (next state with a saved config, next state without one).
_TRANSITIONS: dict[tuple[ProvisionState, ProvisionEvent], tuple[ProvisionState, ProvisionState]] = {
    **{(state, _E.BOOT): (_S.JOINING, _S.AP_UP) for state in _S},
    **{(state, _E.REPROVISION_BUTTON): (_S.AP_UP, _S.AP_UP) for state in _S},
    (_S.AP_UP, _E.CONFIG_RECEIVED): (_S.JOINING, _S.JOINING),
    (_S.JOINING, _E.UPLINK_UP): (_S.PROVISIONED, _S.PROVISIONED),
    (_S.JOINING, _E.UPLINK_TIMEOUT): (_S.FALLBACK_AP, _S.FALLBACK_AP),
    (_S.FALLBACK_AP, _E.CONFIG_RECEIVED): (_S.JOINING, _S.JOINING),
    (_S.FALLBACK_AP, _E.AP_WINDOW_ELAPSED): (_S.JOINING, _S.FALLBACK_AP),
}


def next_state(
    current: ProvisionState, event: ProvisionEvent, *, has_config: bool
) -> ProvisionState:
    """Return the state after ``event``. Unmapped pairs keep ``current``."""
    entry = _TRANSITIONS.get((current, event))
    if entry is None:
        return current
    with_config, without_config = entry
    return with_config if has_config else without_config
