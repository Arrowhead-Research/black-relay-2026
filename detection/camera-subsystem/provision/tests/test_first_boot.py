"""first-boot.sh seeds a kit from the number on its boot partition, once, with nobody there.

Runs the real script on a scratch tree, with a stub in place of seed-kit.sh and of everything
that would change this machine: getent, raspi-config and systemctl. sync is stubbed too: the real
one waits for every disk on the host, and held this suite for as long as an SD card was being
written.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

OPS = Path(__file__).parents[1] / "ops"
SCRIPT = OPS / "first-boot.sh"
UNIT = OPS / "black-relay-first-boot.service"
LABEL = (
    "BLACK RELAY CAMERA KIT\nSetup WiFi:  BlackRelay-CAM-10000000abcdef01\n"
    "Passphrase:  BlackRelay2026\nSetup page:  http://10.42.0.1/\n"
)

STUB = r"""#!/usr/bin/env bash
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$STUB_LOG"
case "$name" in
  getent) [[ -e $STUB_STATE/no-user ]] || echo "kit-1:x:$2:$2::/home/kit-1:/bin/bash" ;;
  raspi-config) [[ -e $STUB_STATE/fail-expand ]] && exit 1 ;;
  seed-kit.sh)
    printf 'CONSOLE_USER=%s\n' "${CONSOLE_USER:-}" >> "$STUB_LOG"
    if [[ -e $STUB_STATE/fail-seed ]]; then echo "seed-kit: useradd is missing" >&2; exit 1; fi
    printf '%s' "$STUB_LABEL"
    mkdir -p "$(dirname "$SEED_MARKER")"; : > "$SEED_MARKER"
    echo "seed-kit: reboot now." >&2
    [[ -e $STUB_STATE/fail-reload ]] && exit 8 ;;
esac
exit 0
"""


class Card:
    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "bin"
        self.ops = tmp_path / "ops"
        self.boot = tmp_path / "boot"
        self.state = tmp_path / "state"
        self.marker = tmp_path / "black-relay" / "kit-seeded"
        self.log = tmp_path / "calls.log"
        for path in (self.bin, self.ops, self.boot, self.state):
            path.mkdir()
        for directory, name in (
            (self.bin, "getent"),
            (self.bin, "raspi-config"),
            (self.bin, "systemctl"),
            (self.bin, "sync"),
            (self.ops, "seed-kit.sh"),
        ):
            stub = directory / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.log.touch()
        self.number = self.boot / "black-relay-kit"
        self.label = self.boot / "black-relay-label.txt"
        self.failure = self.boot / "black-relay-first-boot.log"

    def run(self, number: str | None = "2\n") -> subprocess.CompletedProcess:
        if number is not None:
            self.number.write_text(number)
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "BOOT_DIR": str(self.boot),
            "OPS_DIR": str(self.ops),
            "SEED_MARKER": str(self.marker),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "STUB_LABEL": LABEL,
            "LANG": "en_GB.UTF-8",
        }
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, input="", capture_output=True, text=True, check=False
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def left_untouched(self) -> bool:
        calls = self.calls()
        return (
            self.number.exists()
            and not self.label.exists()
            and not self.marker.exists()
            and not any(call.startswith("systemctl") for call in calls)
        )


@pytest.fixture
def card(tmp_path: Path) -> Card:
    return Card(tmp_path)


def test_a_first_boot_seeds_the_kit_with_its_number_as_the_images_user(card: Card) -> None:
    result = card.run("2\n")
    assert result.returncode == 0, result.stderr
    calls = card.calls()
    assert "getent passwd 1000" in calls
    assert "seed-kit.sh --kit 2" in calls
    assert "CONSOLE_USER=kit-1" in calls


def test_it_leaves_the_label_removes_the_number_and_reboots_last(card: Card) -> None:
    result = card.run()
    assert result.returncode == 0, result.stderr
    assert card.label.read_text() == LABEL
    assert not card.number.exists()
    assert not card.failure.exists()
    assert card.calls()[-2:] == ["sync ", "systemctl --no-block reboot"]
    assert not [p for p in card.boot.iterdir() if p.name.startswith(".")]


def test_the_root_filesystem_is_grown_before_the_seed(card: Card) -> None:
    card.run()
    calls = card.calls()
    assert calls.index("raspi-config nonint do_expand_rootfs") < calls.index("seed-kit.sh --kit 2")


def test_a_filesystem_that_would_not_grow_does_not_stop_the_seed(card: Card) -> None:
    (card.state / "fail-expand").touch()
    result = card.run()
    assert result.returncode == 0, result.stderr
    assert card.label.exists()
    assert "do_expand_rootfs" in result.stderr


@pytest.mark.parametrize("number", ["7", "7\r\n", " 42 \n", "\n9\n"])
def test_a_number_written_by_any_editor_is_read(card: Card, number: str) -> None:
    result = card.run(number)
    assert result.returncode == 0, result.stderr
    assert f"seed-kit.sh --kit {number.strip()}" in card.calls()


@pytest.mark.parametrize("number", ["", "0", "100", "two", "2 3", "-1", "2; reboot", "1.5"])
def test_a_number_that_is_not_one_to_99_changes_nothing_and_says_why_on_the_card(
    card: Card, number: str
) -> None:
    result = card.run(number)
    assert result.returncode != 0
    assert card.left_untouched()
    assert not any(call.startswith("seed-kit.sh") for call in card.calls())
    assert "1 to 99" in card.failure.read_text()


def test_a_seed_that_fails_keeps_the_number_writes_why_and_does_not_reboot(card: Card) -> None:
    (card.state / "fail-seed").touch()
    result = card.run()
    assert result.returncode != 0
    assert card.left_untouched()
    assert "useradd is missing" in card.failure.read_text()
    assert not [p for p in card.boot.iterdir() if p.name.startswith(".")]


def test_a_seed_that_only_failed_its_reload_counts_as_seeded(card: Card) -> None:
    """seed-kit.sh reloads NetworkManager last, after the seed is whole. The reboot loads the
    profile anyway, and a kit left with its number would be refused a seed on every boot."""
    (card.state / "fail-reload").touch()
    result = card.run()
    assert result.returncode == 0, result.stderr
    assert card.label.read_text() == LABEL
    assert not card.number.exists()
    assert card.calls()[-1] == "systemctl --no-block reboot"


def test_a_kit_already_seeded_only_loses_the_number(card: Card) -> None:
    """Power lost after the seed and before the number went."""
    card.marker.parent.mkdir()
    card.marker.touch()
    result = card.run()
    assert result.returncode == 0, result.stderr
    assert not card.number.exists()
    assert not any(call.startswith(("seed-kit.sh", "systemctl")) for call in card.calls())


def test_an_image_with_no_user_changes_nothing(card: Card) -> None:
    (card.state / "no-user").touch()
    result = card.run()
    assert result.returncode != 0
    assert card.left_untouched()
    assert "uid 1000" in card.failure.read_text()


def test_with_no_number_it_fails_without_touching_anything(card: Card) -> None:
    result = card.run(None)
    assert result.returncode != 0
    assert not card.label.exists()
    assert card.calls() == []


def test_the_unit_runs_only_while_the_number_is_there_and_is_enabled_for_boot() -> None:
    unit = UNIT.read_text()
    assert "ConditionPathExists=/boot/firmware/black-relay-kit" in unit
    assert "ExecStart=/opt/black-relay/ops/first-boot.sh" in unit
    assert "Type=oneshot" in unit
    assert "WantedBy=multi-user.target" in unit
    # seed-kit.sh has NetworkManager reload its profiles.
    assert "NetworkManager.service" in unit.split("After=")[1].splitlines()[0]


def test_the_script_reads_the_file_the_unit_waits_for() -> None:
    script = SCRIPT.read_text()
    assert 'boot_dir=${BOOT_DIR:-/boot/firmware}' in script
    assert 'number_file="$boot_dir/black-relay-kit"' in script
