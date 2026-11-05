"""Tests for ``cameraprovision.uplink`` — the L3 check and watchdog decision.

Uplink means WiFi associated plus a lease (DESIGN.md 7.4), so the probe has no
broker or central-node method at all: an outage there cannot influence these
decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cameraprovision.state import ProvisionState
from cameraprovision.uplink import should_relaunch_ap, uplink_up


@dataclass
class FakeProbe:
    associated: bool
    lease: bool

    def wifi_associated(self) -> bool:
        return self.associated

    def ipv4_lease(self) -> bool:
        return self.lease


@pytest.mark.parametrize(
    ("associated", "lease", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_uplink_up_requires_association_and_lease(
    associated: bool, lease: bool, expected: bool
) -> None:
    assert uplink_up(FakeProbe(associated, lease)) is expected


def test_relaunch_when_joining_without_uplink_past_timeout() -> None:
    assert should_relaunch_ap(
        state=ProvisionState.JOINING, seconds_since_boot=121.0, uplink=False, timeout_s=120.0
    )


def test_relaunch_exactly_at_timeout() -> None:
    assert should_relaunch_ap(
        state=ProvisionState.JOINING, seconds_since_boot=120.0, uplink=False, timeout_s=120.0
    )


def test_no_relaunch_when_joining_with_uplink() -> None:
    assert not should_relaunch_ap(
        state=ProvisionState.JOINING, seconds_since_boot=121.0, uplink=True, timeout_s=120.0
    )


def test_no_relaunch_when_joining_before_timeout() -> None:
    assert not should_relaunch_ap(
        state=ProvisionState.JOINING, seconds_since_boot=119.9, uplink=False, timeout_s=120.0
    )


@pytest.mark.parametrize("uplink", [True, False])
@pytest.mark.parametrize("state", [s for s in ProvisionState if s is not ProvisionState.JOINING])
def test_only_joining_can_relaunch(state: ProvisionState, uplink: bool) -> None:
    assert not should_relaunch_ap(
        state=state, seconds_since_boot=10_000.0, uplink=uplink, timeout_s=120.0
    )
