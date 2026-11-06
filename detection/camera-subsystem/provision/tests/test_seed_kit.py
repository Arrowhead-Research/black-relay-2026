"""seed-kit.sh gives a clone its own identity, with nothing typed but one password.

Runs the real script on a scratch tree. Only what would change this machine or needs a Pi
is stubbed: nmcli, chpasswd, id, systemd-machine-id-setup and the provisioning Python.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "ops" / "seed-kit.sh"
PASSWORD = "kit two console pass"
PASSPHRASE = "A" * 24

STUB = r"""#!/usr/bin/env bash
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$STUB_LOG"
case "$name" in
  python) echo "$STUB_PASSPHRASE" ;;
  id) [[ -e $STUB_STATE/no-user ]] && exit 1 || echo 1000 ;;
  chpasswd) [[ -e $STUB_STATE/fail-chpasswd ]] && exit 9; cat >> "$STUB_STATE/chpasswd.stdin" ;;
  systemd-machine-id-setup)
    if [[ -e $STUB_STATE/same-id ]]; then cat "$STUB_STATE/old-id" > "$MACHINE_ID_FILE"
    else printf '%032x\n' "$RANDOM$RANDOM" > "$MACHINE_ID_FILE"; fi ;;
esac
exit 0
"""
STUBBED = "python id chpasswd systemd-machine-id-setup nmcli".split()

OLD_ID = "0" * 31 + "1\n"


class Kit:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.nm_dir = tmp_path / "nm"
        self.ssh_dir = tmp_path / "ssh"
        self.machine_id = tmp_path / "machine-id"
        self.dbus_id = tmp_path / "dbus-machine-id"
        self.marker = tmp_path / "black-relay" / "kit-seeded"
        self.log = tmp_path / "calls.log"
        for path in (self.bin, self.state, self.nm_dir, self.ssh_dir):
            path.mkdir()
        for name in STUBBED:
            stub = self.bin / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        (tmp_path / "serial").write_bytes(b"10000000abcdef01\0")
        self.log.touch()
        self.machine_id.write_text(OLD_ID)
        self.dbus_id.write_text(OLD_ID)
        (self.state / "old-id").write_text(OLD_ID)
        for name in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub", "ssh_host_rsa_key"):
            (self.ssh_dir / name).write_text("the image's key\n")
        (self.ssh_dir / "sshd_config").write_text("kept\n")

    @property
    def profile(self) -> Path:
        return self.nm_dir / "black-relay-setup-ap.nmconnection"

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        if stdin is None:
            stdin = f"{PASSWORD}\n{PASSWORD}\n"
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "NM_DIR": str(self.nm_dir),
            "SERIAL_FILE": str(self.tmp / "serial"),
            "PROVISION_PYTHON": str(self.bin / "python"),
            "SSH_DIR": str(self.ssh_dir),
            "MACHINE_ID_FILE": str(self.machine_id),
            "DBUS_MACHINE_ID_FILE": str(self.dbus_id),
            "SEED_MARKER": str(self.marker),
            "SUDO_USER": "kit-1",
            "SEED_ALLOW_NON_ROOT": "1",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "STUB_PASSPHRASE": PASSPHRASE,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def host_keys(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.ssh_dir.glob("ssh_host_*")}

    def set_password_log(self) -> str:
        path = self.state / "chpasswd.stdin"
        return path.read_text() if path.exists() else ""


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not on PATH")
    return Kit(tmp_path)


def test_a_seed_replaces_host_keys_and_machine_id_itself(kit: Kit) -> None:
    before = kit.host_keys()
    result = kit.run()
    assert result.returncode == 0, result.stderr
    after = kit.host_keys()
    assert after and "ssh_host_ed25519_key" in after
    assert not set(before.values()) & set(after.values())
    assert stat.S_IMODE((kit.ssh_dir / "ssh_host_ed25519_key").stat().st_mode) == 0o600
    assert stat.S_IMODE((kit.ssh_dir / "ssh_host_ed25519_key.pub").stat().st_mode) == 0o644
    assert (kit.ssh_dir / "sshd_config").read_text() == "kept\n"
    assert kit.machine_id.read_text() != OLD_ID
    assert not kit.dbus_id.exists()
    assert not [p for p in kit.ssh_dir.iterdir() if p.name.startswith(".")]


def test_two_kits_from_one_image_differ(tmp_path: Path) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not on PATH")
    fingerprints = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        kit = Kit(root)
        assert kit.run().returncode == 0
        fingerprints.append(kit.host_keys()["ssh_host_ed25519_key.pub"])
    assert fingerprints[0] != fingerprints[1]


def test_the_console_password_goes_by_stdin_and_nowhere_else(kit: Kit) -> None:
    result = kit.run()
    assert result.returncode == 0, result.stderr
    assert kit.set_password_log() == f"kit-1:{PASSWORD}\n"
    assert PASSWORD not in kit.log.read_text()  # every stub's argv
    assert PASSWORD not in result.stdout
    assert PASSWORD not in result.stderr
    assert PASSWORD not in kit.profile.read_text()


def test_a_seed_prints_the_label_and_installs_the_profile(kit: Kit) -> None:
    result = kit.run()
    assert "Setup WiFi:  BlackRelay-CAM-10000000abcdef01" in result.stdout
    assert f"Passphrase:  {PASSPHRASE}" in result.stdout
    assert stat.S_IMODE(kit.profile.stat().st_mode) == 0o600
    assert kit.marker.exists()


@pytest.mark.parametrize(
    "stdin",
    [
        "",  # no terminal, no input
        "\n\n\n\n\n\n",  # blank
        "short\nshort\nshort\nshort\nshort\nshort\n",
        f"{PASSWORD}\nsomething else\n" * 3,
    ],
)
def test_a_bad_password_changes_nothing(kit: Kit, stdin: str) -> None:
    before = kit.host_keys()
    result = kit.run(stdin=stdin)
    assert result.returncode != 0
    assert kit.host_keys() == before
    assert kit.machine_id.read_text() == OLD_ID
    assert not kit.profile.exists()
    assert not kit.marker.exists()
    assert kit.set_password_log() == ""
    assert PASSWORD not in result.stdout + result.stderr


def test_a_mistyped_password_can_be_retried(kit: Kit) -> None:
    result = kit.run(stdin=f"{PASSWORD}\nwrong\n{PASSWORD}\n{PASSWORD}\n")
    assert result.returncode == 0, result.stderr
    assert kit.set_password_log() == f"kit-1:{PASSWORD}\n"


@pytest.mark.parametrize("seeded_by", ["profile", "marker"])
def test_an_already_seeded_kit_refuses_and_changes_nothing(kit: Kit, seeded_by: str) -> None:
    if seeded_by == "profile":
        kit.profile.write_text("old\n")
    else:
        kit.marker.parent.mkdir()
        kit.marker.touch()
    before = kit.host_keys()
    result = kit.run()
    assert result.returncode != 0
    assert "--force" in result.stderr
    assert kit.host_keys() == before
    assert kit.machine_id.read_text() == OLD_ID
    assert kit.set_password_log() == ""


def test_force_replaces_everything_again(kit: Kit) -> None:
    assert kit.run().returncode == 0
    keys, machine_id = kit.host_keys(), kit.machine_id.read_text()
    (kit.state / "old-id").write_text(machine_id)
    new_password = "another console pass"
    result = kit.run("--force", stdin=f"{new_password}\n{new_password}\n")
    assert result.returncode == 0, result.stderr
    assert not set(keys.values()) & set(kit.host_keys().values())
    assert kit.machine_id.read_text() != machine_id
    assert kit.set_password_log().endswith(f"kit-1:{new_password}\n")


def test_a_failed_password_change_stops_before_identity_changes(kit: Kit) -> None:
    (kit.state / "fail-chpasswd").touch()
    before = kit.host_keys()
    result = kit.run()
    assert result.returncode != 0
    assert kit.host_keys() == before
    assert not kit.profile.exists()
    assert not kit.marker.exists()


def test_a_machine_id_that_did_not_change_is_an_error_and_no_seed(kit: Kit) -> None:
    (kit.state / "same-id").touch()
    result = kit.run()
    assert result.returncode != 0
    assert "machine ID" in result.stderr
    assert not kit.profile.exists()
    assert not kit.marker.exists()


def test_an_interrupted_seed_can_run_again_without_force(kit: Kit) -> None:
    (kit.state / "same-id").touch()
    assert kit.run().returncode != 0
    (kit.state / "same-id").unlink()
    assert kit.run().returncode == 0


def test_a_missing_console_user_changes_nothing(kit: Kit) -> None:
    (kit.state / "no-user").touch()
    before = kit.host_keys()
    result = kit.run()
    assert result.returncode != 0
    assert "kit-1" in result.stderr
    assert kit.host_keys() == before


def test_it_needs_root(kit: Kit) -> None:
    if os.geteuid() == 0:
        pytest.skip("this test needs a non-root user")
    env = {"PATH": f"{kit.bin}:/usr/bin:/bin", "SUDO_USER": "kit-1"}
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, input="", capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "run as root" in result.stderr
