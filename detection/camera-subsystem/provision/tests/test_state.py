"""Tests for ``cameraprovision.state`` — the provisioning state machine.

The full (state, event, has_config) matrix is pinned by
``test_every_other_pair_keeps_the_current_state``, so a spurious transition
fails even if no named test mentions that pair.
"""

from __future__ import annotations

import pytest

from cameraprovision.state import ProvisionEvent as E
from cameraprovision.state import ProvisionState as S
from cameraprovision.state import next_state

BOTH = [True, False]


def test_event_names_match_the_plan() -> None:
    assert {e.value for e in E} == {
        "boot",
        "config_received",
        "uplink_up",
        "uplink_timeout",
        "reprovision_button",
        "ap_window_elapsed",
    }


def test_state_names_match_the_plan() -> None:
    assert {s.name for s in S} == {
        "UNPROVISIONED",
        "AP_UP",
        "JOINING",
        "PROVISIONED",
        "FALLBACK_AP",
    }


# A boot is a fresh start, so the prior state must not matter: a normal reboot
# with good config joins silently from anywhere, including PROVISIONED.
@pytest.mark.parametrize("current", list(S))
def test_boot_without_config_opens_the_ap(current: S) -> None:
    assert next_state(current, E.BOOT, has_config=False) is S.AP_UP


@pytest.mark.parametrize("current", list(S))
def test_boot_with_config_joins(current: S) -> None:
    assert next_state(current, E.BOOT, has_config=True) is S.JOINING


@pytest.mark.parametrize("has_config", BOTH)
def test_joining_uplink_up_is_provisioned(has_config: bool) -> None:
    assert next_state(S.JOINING, E.UPLINK_UP, has_config=has_config) is S.PROVISIONED


@pytest.mark.parametrize("has_config", BOTH)
def test_joining_uplink_timeout_falls_back_to_the_ap(has_config: bool) -> None:
    assert next_state(S.JOINING, E.UPLINK_TIMEOUT, has_config=has_config) is S.FALLBACK_AP


@pytest.mark.parametrize("has_config", BOTH)
def test_ap_up_config_received_joins(has_config: bool) -> None:
    assert next_state(S.AP_UP, E.CONFIG_RECEIVED, has_config=has_config) is S.JOINING


@pytest.mark.parametrize("has_config", BOTH)
def test_provisioned_link_loss_does_not_reopen_the_ap(has_config: bool) -> None:
    assert next_state(S.PROVISIONED, E.UPLINK_TIMEOUT, has_config=has_config) is S.PROVISIONED


# Power-cut recovery: a node that booted before the router must not stay on the
# setup AP, or it would never rejoin without an operator.
def test_fallback_window_elapsed_with_config_rejoins() -> None:
    assert next_state(S.FALLBACK_AP, E.AP_WINDOW_ELAPSED, has_config=True) is S.JOINING


def test_fallback_window_elapsed_without_config_stays_on_the_ap() -> None:
    assert next_state(S.FALLBACK_AP, E.AP_WINDOW_ELAPSED, has_config=False) is S.FALLBACK_AP


@pytest.mark.parametrize("has_config", BOTH)
def test_fallback_config_received_joins(has_config: bool) -> None:
    assert next_state(S.FALLBACK_AP, E.CONFIG_RECEIVED, has_config=has_config) is S.JOINING


@pytest.mark.parametrize("has_config", BOTH)
@pytest.mark.parametrize("current", list(S))
def test_reprovision_button_opens_the_ap_from_every_state(current: S, has_config: bool) -> None:
    assert next_state(current, E.REPROVISION_BUTTON, has_config=has_config) is S.AP_UP


# Every pair the tests above assign a transition to. Anything else is a no-op.
_MAPPED = {
    (S.JOINING, E.UPLINK_UP),
    (S.JOINING, E.UPLINK_TIMEOUT),
    (S.AP_UP, E.CONFIG_RECEIVED),
    (S.FALLBACK_AP, E.CONFIG_RECEIVED),
    (S.FALLBACK_AP, E.AP_WINDOW_ELAPSED),
}
_UNMAPPED = [
    (s, e)
    for s in S
    for e in E
    if e not in {E.BOOT, E.REPROVISION_BUTTON} and (s, e) not in _MAPPED
]


@pytest.mark.parametrize("has_config", BOTH)
@pytest.mark.parametrize(("current", "event"), _UNMAPPED)
def test_every_other_pair_keeps_the_current_state(
    current: S, event: E, has_config: bool
) -> None:
    assert next_state(current, event, has_config=has_config) is current
