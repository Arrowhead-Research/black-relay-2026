"""seed-kit.sh gives a clone its own identity, with nothing typed but the kit's number.

Runs the real script on a scratch tree. Only what would change this machine or needs a Pi
is stubbed: nmcli, chpasswd, id, getent, useradd, usermod, visudo, chown and
systemd-machine-id-setup.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "ops" / "seed-kit.sh"
DEFAULT = "BlackRelay2026"

STUB = r"""#!/usr/bin/env bash
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$STUB_LOG"
case "$name" in
  id)
    user=${*: -1}
    [[ -e $STUB_STATE/no-user ]] && exit 1
    [[ $user == kit-1 || -e $STUB_STATE/home/$user ]] || exit 1
    if [[ $1 == -Gn && -e $STUB_STATE/no-groups ]]; then echo "$user"
    elif [[ $1 == -Gn ]]; then echo "$user adm sudo video"; else echo 1000; fi ;;
  getent) echo "$2:x:1000:1000::$STUB_STATE/home/$2:/bin/bash" ;;
  useradd)
    [[ -e $STUB_STATE/fail-useradd ]] && exit 4
    mkdir -p "$STUB_STATE/home/${*: -1}" ;;
  usermod) [[ -e $STUB_STATE/fail-usermod ]] && exit 6 ;;
  visudo) [[ -e $STUB_STATE/fail-visudo ]] && exit 1; cp "${*: -1}" "$STUB_STATE/sudoers.checked" ;;
  chown) ;;
  chpasswd) [[ -e $STUB_STATE/fail-chpasswd ]] && exit 9; cat >> "$STUB_STATE/chpasswd.stdin" ;;
  systemd-machine-id-setup)
    if [[ -e $STUB_STATE/same-id ]]; then cat "$STUB_STATE/old-id" > "$MACHINE_ID_FILE"
    else printf '%032x\n' "$RANDOM$RANDOM" > "$MACHINE_ID_FILE"; fi ;;
  nmcli) [[ $* == "connection reload" && -e $STUB_STATE/fail-reload ]] && exit 8 ;;
esac
exit 0
"""
STUBBED = "id getent useradd usermod visudo chown chpasswd systemd-machine-id-setup nmcli".split()

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
        self.sudoers = tmp_path / "sudoers.d"
        self.autologin = tmp_path / "getty" / "autologin.conf"
        self.log = tmp_path / "calls.log"
        for path in (self.bin, self.state, self.nm_dir, self.ssh_dir, self.sudoers):
            path.mkdir()
        # The image's user, with the build host's key, and the image's console autologin.
        image_ssh = self.state / "home" / "kit-1" / ".ssh"
        image_ssh.mkdir(parents=True)
        (image_ssh / "authorized_keys").write_text("ssh-ed25519 AAAA build-host\n")
        self.autologin.parent.mkdir()
        self.autologin.write_text("[Service]\nExecStart=-/sbin/agetty --autologin kit-1 %I\n")
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

    def run(self, *args: str, kit: str | None = "1", **extra: str) -> subprocess.CompletedProcess:
        if kit is not None:
            args = ("--kit", kit, *args)
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "NM_DIR": str(self.nm_dir),
            "SERIAL_FILE": str(self.tmp / "serial"),
            "SSH_DIR": str(self.ssh_dir),
            "MACHINE_ID_FILE": str(self.machine_id),
            "DBUS_MACHINE_ID_FILE": str(self.dbus_id),
            "SEED_MARKER": str(self.marker),
            "SUDO_USER": "kit-1",
            "SEED_ALLOW_NON_ROOT": "1",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "SUDOERS_DIR": str(self.sudoers),
            "AUTOLOGIN_DROPIN": str(self.autologin),
            **extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            input="",
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


def test_the_console_password_is_the_default_and_goes_by_stdin_alone(kit: Kit) -> None:
    result = kit.run()
    assert result.returncode == 0, result.stderr
    assert kit.set_password_log() == f"kit-1:{DEFAULT}\n"
    assert DEFAULT not in kit.log.read_text()  # every stub's argv
    assert DEFAULT not in result.stderr


def test_a_seed_prints_the_label_and_installs_the_profile(kit: Kit) -> None:
    result = kit.run()
    assert "Setup WiFi:  BlackRelay-CAM-10000000abcdef01" in result.stdout
    assert f"Passphrase:  {DEFAULT}" in result.stdout
    assert f"psk={DEFAULT}" in kit.profile.read_text()
    assert stat.S_IMODE(kit.profile.stat().st_mode) == 0o600
    assert kit.marker.exists()


def _reboot_notice(text: str) -> bool:
    return "reboot now" in text.lower() and "machine id" in text.lower()


def test_a_seed_says_to_reboot_now_and_why_and_the_label_stays_four_lines(kit: Kit) -> None:
    result = kit.run()
    assert result.returncode == 0, result.stderr
    assert _reboot_notice(result.stderr)
    assert len(result.stdout.splitlines()) == 4  # what goes on the label
    assert "change-passwords.sh" in result.stderr


def test_the_reboot_notice_appears_even_if_the_reload_fails(kit: Kit) -> None:
    (kit.state / "fail-reload").touch()
    result = kit.run()
    assert result.returncode != 0
    assert _reboot_notice(result.stderr)


@pytest.mark.parametrize("args", [(), ("--kit",), ("--kit", "0"), ("--kit", "100"), ("--kit", "two"), ("--kit", "2", "x")])
def test_it_needs_the_kits_number_and_changes_nothing_without_it(kit: Kit, args: tuple) -> None:
    before = kit.host_keys()
    result = kit.run(*args, kit=None)
    assert result.returncode != 0
    assert "--kit N" in result.stderr
    assert kit.host_keys() == before
    assert kit.set_password_log() == ""
    assert not kit.profile.exists()


def test_kit_one_keeps_the_images_user_and_loses_its_autologin(kit: Kit) -> None:
    result = kit.run(kit="1")
    assert result.returncode == 0, result.stderr
    calls = kit.log.read_text()
    assert "useradd" not in calls
    assert "usermod" not in calls
    assert not kit.autologin.exists()
    assert (kit.sudoers / "010_black-relay-kit").read_text() == "kit-1 ALL=(ALL) NOPASSWD: ALL\n"
    assert "log in as kit-1" in result.stderr


def test_another_kit_gets_its_own_user_with_the_images_groups_key_and_sudo(kit: Kit) -> None:
    result = kit.run(kit="2")
    assert result.returncode == 0, result.stderr
    calls = kit.log.read_text()
    assert "useradd --create-home --shell /bin/bash --comment Black Relay kit 2 --groups adm,sudo,video kit-2" in calls
    assert kit.set_password_log() == f"kit-2:{DEFAULT}\n"
    key = kit.state / "home" / "kit-2" / ".ssh" / "authorized_keys"
    assert key.read_text() == "ssh-ed25519 AAAA build-host\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    rule = kit.sudoers / "010_black-relay-kit"
    assert rule.read_text() == "kit-2 ALL=(ALL) NOPASSWD: ALL\n"
    assert stat.S_IMODE(rule.stat().st_mode) == 0o440
    assert (kit.state / "sudoers.checked").read_text() == rule.read_text()
    assert "log in as kit-2" in result.stderr


def test_an_image_user_in_no_group_but_its_own_still_seeds(kit: Kit) -> None:
    """Found in the container run: the list of other groups was empty, the pipeline that builds
    it failed, and the script stopped without a word."""
    (kit.state / "no-groups").touch()
    result = kit.run(kit="2")
    assert result.returncode == 0, result.stderr
    assert "useradd --create-home --shell /bin/bash --comment Black Relay kit 2 kit-2" in kit.log.read_text()
    assert kit.marker.exists()


def test_the_images_user_is_closed_only_after_the_kits_user_is_ready(kit: Kit) -> None:
    result = kit.run(kit="2")
    assert result.returncode == 0, result.stderr
    calls = kit.log.read_text().splitlines()
    closed = calls.index("usermod --lock --expiredate 1 kit-1")
    assert closed > max(i for i, call in enumerate(calls) if call.startswith(("useradd", "visudo", "chpasswd")))
    assert not kit.autologin.exists()


@pytest.mark.parametrize("failing", ["fail-useradd", "fail-visudo", "fail-chpasswd"])
def test_a_kit_user_that_cannot_be_made_leaves_the_images_user_open(kit: Kit, failing: str) -> None:
    (kit.state / failing).touch()
    before = kit.host_keys()
    result = kit.run(kit="2")
    assert result.returncode != 0
    assert "usermod" not in kit.log.read_text()
    assert kit.autologin.exists()
    assert kit.host_keys() == before
    assert not kit.profile.exists()
    assert not kit.marker.exists()
    if failing == "fail-visudo":
        assert not (kit.sudoers / "010_black-relay-kit").exists()
        assert not [p for p in kit.sudoers.iterdir() if p.name.startswith(".")]


def test_a_seed_stopped_while_closing_the_images_user_can_run_again(kit: Kit) -> None:
    (kit.state / "fail-usermod").touch()
    result = kit.run(kit="2")
    assert result.returncode != 0
    assert not kit.marker.exists()
    (kit.state / "fail-usermod").unlink()
    (kit.state / "old-id").write_text(kit.machine_id.read_text())
    result = kit.run(kit="2")
    assert result.returncode == 0, result.stderr
    assert kit.log.read_text().count("useradd") == 1  # the user from the first run is kept


def test_the_default_is_accepted_in_a_utf8_locale(kit: Kit) -> None:
    """Kit 1 runs in en_GB.UTF-8, where a range of characters follows the dictionary, not ASCII."""
    names = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=False).stdout
    locales = [n for n in names.split() if n.lower().startswith("en_") and "utf" in n.lower()]
    if not locales:
        pytest.skip("no English UTF-8 locale on this host")
    result = kit.run(kit="2", LANG=locales[0], LC_ALL=locales[0])
    assert result.returncode == 0, result.stderr
    assert kit.set_password_log() == f"kit-2:{DEFAULT}\n"


def test_a_build_can_name_another_default(kit: Kit) -> None:
    result = kit.run(kit="3", BLACK_RELAY_DEFAULT_PASSWORD="Another-Default_9")
    assert result.returncode == 0, result.stderr
    assert kit.set_password_log() == "kit-3:Another-Default_9\n"
    assert "Passphrase:  Another-Default_9" in result.stdout


@pytest.mark.parametrize("default", ["short", "has space in it", "amp&ersand99", "x" * 64])
def test_a_default_the_setup_wifi_cannot_take_changes_nothing(kit: Kit, default: str) -> None:
    before = kit.host_keys()
    result = kit.run(BLACK_RELAY_DEFAULT_PASSWORD=default)
    assert result.returncode != 0
    assert kit.host_keys() == before
    assert kit.set_password_log() == ""
    assert not kit.profile.exists()


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


def test_force_replaces_everything_again_and_puts_the_default_back(kit: Kit) -> None:
    assert kit.run().returncode == 0
    keys, machine_id = kit.host_keys(), kit.machine_id.read_text()
    (kit.state / "old-id").write_text(machine_id)
    kit.profile.write_text(kit.profile.read_text().replace(DEFAULT, "a changed passphrase"))
    result = kit.run("--force")
    assert result.returncode == 0, result.stderr
    assert not set(keys.values()) & set(kit.host_keys().values())
    assert kit.machine_id.read_text() != machine_id
    assert kit.set_password_log() == f"kit-1:{DEFAULT}\n" * 2
    assert f"psk={DEFAULT}" in kit.profile.read_text()


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
        ["bash", str(SCRIPT), "--kit", "1"], env=env, input="", capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "run as root" in result.stderr
