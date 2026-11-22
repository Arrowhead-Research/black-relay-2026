"""ops/install-node.sh, run for real against a fake root with every system command stubbed.

The stubs log each call, so these tests can say what the installer never does: reboot, start a
service, load the firewall, or install hailo-all. What needs a real system is in
tests/container_install_node.sh.
"""

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cameralink" / "ops" / "install-node.sh"
GOOD_HEF_SHA = "6a20a3952732c7ea5f3f94a586f004015d3ae7d8f6ec5f7b2fcaa4126237124e"
CACHE_MOUNT = r"run-frigate\x2dcache.mount"
SAVED_BROKER = "add element inet black_relay central_hosts { 192.168.1.22 }\n"

STUB = r"""#!/bin/bash
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$STUB_LOG"
state=$STUB_STATE
fail_if() { if [[ -e $state/fail-$1 ]]; then exit 7; fi; }
case "$name $*" in
  "id -u camerabridge") [[ -e $state/user ]] && echo 998 || exit 1 ;;
  "useradd "*) touch "$state/user" ;;
  "python3 -m venv "*)
    dir=${*: -1}
    mkdir -p "$dir/bin"
    for tool in pip python camera-provision camerabridge camera-link; do
      cp "$0" "$dir/bin/$tool"
    done
    ;;
  "pip "*) fail_if pip ;;
  "camera-link --help"|"camera-provision run --help"|"camerabridge run --help") ;;
  "python -c import cameralink.reolink") fail_if reolink ;;
  "nft -c -f "*) fail_if nft-check ;;
  "nft list table inet black_relay") [[ -e $state/table ]] || exit 1 ;;
  "nmcli -t -f RUNNING general") [[ -e $state/nm-down ]] && echo stopped || echo running ;;
  "nmcli -t -f NAME connection show") [[ -e $state/wired ]] && echo 'Wired connection 1' ;;
  "nmcli connection delete "*) fail_if nm-delete; rm -f "$state/wired" ;;
  "modinfo -F version hailo_pci") [[ -e $state/no-driver ]] && exit 1 || echo 4.21.0 ;;
  "dpkg -s hailo-all") [[ -e $state/hailo-all ]] || exit 1 ;;
  "dpkg -s tailscale") [[ -e $state/tailscale ]] || exit 1 ;;
  "apt-get "*purge*tailscale*) rm -f "$state/tailscale" ;;
  "docker image inspect "*) [[ -e $state/no-image ]] && exit 1 || true ;;
  "docker version "*) [[ -e $state/docker-down ]] && exit 1 || true ;;
  "sshd -T") echo passwordauthentication no; echo permitrootlogin no ;;
  "systemd-analyze verify "*) fail_if verify ;;
  "ss "*) [[ -e $state/ss ]] && cat "$state/ss" ;;
  "ip -o addr show dev eth0") echo '2: eth0 inet 10.0.0.5/24 scope global eth0' ;;
  "ip -o addr show dev tailscale0") [[ -e $state/ts-if ]] && echo '5: tailscale0 inet 100.64.0.9/32 scope global tailscale0' ;;
  "systemctl enable "*) fail_if enable ;;
esac
exit 0
"""
STUBBED = (
    "id useradd python3 nft nmcli modinfo dpkg apt-get docker sshd systemd-analyze ss ip systemctl "
    "dnsmasq systemd-tmpfiles tailscale reboot shutdown poweroff"
).split()


class Rig:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "root"
        self.state = tmp_path / "state"
        self.log = tmp_path / "calls.log"
        self.hef = tmp_path / "yolov6n.hef"
        self.bin = tmp_path / "bin"
        for path in (self.root, self.state, self.bin):
            path.mkdir()
        self.log.touch()
        for name in STUBBED:
            stub = self.bin / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        for path in (
            "etc/mosquitto/conf.d",
            "etc/NetworkManager/system-connections",
            "etc/docker",
            "etc/modprobe.d",
        ):
            (self.root / path).mkdir(parents=True)
        self.hef.write_bytes(b"model")

    def path(self, relative: str) -> Path:
        return self.root / relative

    def set(self, *flags: str) -> None:
        for flag in flags:
            (self.state / flag).touch()

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def run(self, *args: str, good_sha: str | None = None) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "BLACK_RELAY_ROOT": str(self.root),
            "BLACK_RELAY_HEF_SHA256": good_sha or hashlib.sha256(self.hef.read_bytes()).hexdigest(),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
        )

    def install(self, *args: str) -> subprocess.CompletedProcess:
        return self.run("--hef", str(self.hef), *args)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


INSTALLED = [
    ("etc/systemd/system/camera-link.service", 0o644, "cameralink/ops/camera-link.service"),
    ("etc/systemd/system/camera-link-dhcp.service", 0o644, "cameralink/ops/camera-link-dhcp.service"),
    ("etc/systemd/system/frigate.service", 0o644, "cameralink/ops/frigate.service"),
    (f"etc/systemd/system/{CACHE_MOUNT}", 0o644, f"cameralink/ops/{CACHE_MOUNT}"),
    ("etc/docker/daemon.json", 0o644, "cameralink/ops/daemon.json"),
    ("etc/modprobe.d/hailo_pci.conf", 0o644, "cameralink/ops/hailo_pci.conf"),
    (
        "etc/NetworkManager/system-connections/black-relay-camera.nmconnection",
        0o600,
        "cameralink/ops/black-relay-camera.nmconnection",
    ),
    ("etc/nftables.conf", 0o644, "provision/ops/black-relay.nft"),
    ("etc/black-relay/central-hosts.nft", 0o644, "provision/ops/central-hosts.nft"),
    ("etc/systemd/system/ssh.service.d/black-relay.conf", 0o644, "provision/ops/ssh-needs-firewall.conf"),
    ("etc/systemd/system/camera-provision.service", 0o644, "provision/ops/camera-provision.service"),
    ("etc/systemd/system/camerabridge.service", 0o644, "provision/ops/camerabridge.service"),
    ("etc/systemd/system/camerabridge-config.path", 0o644, "provision/ops/camerabridge-config.path"),
    (
        "etc/systemd/system/camerabridge-config-changed.service",
        0o644,
        "provision/ops/camerabridge-config-changed.service",
    ),
    ("etc/mosquitto/conf.d/black-relay.conf", 0o644, "provision/ops/mosquitto-black-relay.conf"),
    (
        "etc/systemd/system/mosquitto.service.d/black-relay.conf",
        0o644,
        "provision/ops/mosquitto-service-override.conf",
    ),
    (
        "etc/NetworkManager/dnsmasq-shared.d/black-relay-captive.conf",
        0o644,
        "provision/ops/black-relay-captive.conf",
    ),
    ("etc/systemd/journald.conf.d/90-black-relay.conf", 0o644, "provision/ops/journald-persistent.conf"),
    ("opt/black-relay/ops/README.md", 0o644, "provision/ops/README.md"),
    (
        "opt/black-relay/ops/black-relay-setup-ap.nmconnection.tmpl",
        0o644,
        "provision/ops/black-relay-setup-ap.nmconnection.tmpl",
    ),
    ("opt/black-relay/ops/central-hosts.nft", 0o644, "provision/ops/central-hosts.nft"),
    ("opt/black-relay/ops/seed-kit.sh", 0o755, "provision/ops/seed-kit.sh"),
    ("opt/black-relay/ops/change-passwords.sh", 0o755, "provision/ops/change-passwords.sh"),
    ("opt/black-relay/ops/first-boot.sh", 0o755, "provision/ops/first-boot.sh"),
    (
        "etc/systemd/system/black-relay-first-boot.service",
        0o644,
        "provision/ops/black-relay-first-boot.service",
    ),
]


def test_every_ops_file_has_a_place():
    """A file added to either ops directory must be added to the installer too."""
    named = {source.rsplit("/", 1)[1] for _, _, source in INSTALLED} | {
        "install-node.sh",
        "reach-check.sh",
        "field-run-kit1.md",
    }
    for directory in ("cameralink/ops", "provision/ops"):
        for file in (ROOT / directory).iterdir():
            assert file.name in named, file


@pytest.mark.parametrize("target, mode, source", INSTALLED, ids=[i[0] for i in INSTALLED])
def test_installs_each_file_with_its_mode(rig, target, mode, source):
    assert rig.install().returncode == 0
    installed = rig.path(target)
    assert installed.read_bytes() == (ROOT / source).read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == mode


def test_the_cache_mount_keeps_its_literal_backslash(rig):
    rig.install()
    names = [p.name for p in rig.path("etc/systemd/system").iterdir()]
    assert "run-frigate\\x2dcache.mount" in names
    assert "run-frigate-cache.mount" not in names


def test_leaves_no_staging_file_behind(rig):
    rig.install()
    assert [p for p in rig.root.rglob("*install-tmp*")] == []


def test_installs_the_three_packages_into_their_own_venvs(rig):
    rig.install()
    calls = rig.calls()
    for name, package in (
        ("provision", "provision"),
        ("bridge", "bridge"),
        ("cameralink", "cameralink"),
    ):
        venv = rig.path(f"opt/black-relay/{name}")
        assert f"python3 -m venv {venv}" in calls
        assert any(c.startswith("pip install") and c.endswith(f"/{package}") for c in calls)


def test_proves_the_camera_library_imports_with_the_venvs_python(rig):
    """camera-link --help loads it lazily, so a bad wheel would first show at a factory camera."""
    assert rig.install().returncode == 0
    assert "python -c import cameralink.reolink" in rig.calls()


def test_a_camera_library_that_cannot_import_fails_the_install(rig):
    rig.set("fail-reolink")
    result = rig.install()
    assert result.returncode not in (0, 3)
    assert "INSTALL FAILED" in result.stderr + result.stdout


@pytest.mark.parametrize(
    "secret", ["relay/.env", "bridge/.env", "relay/.env.old", "field-run.local.env"]
)
def test_refuses_a_source_tree_holding_a_secrets_file(rig, secret):
    leaked = rig.path(f"opt/black-relay/src/{secret}")
    leaked.parent.mkdir(parents=True)
    leaked.write_text("RELAY_API_TOKEN=do-not-print\n")
    result = rig.install()
    assert result.returncode not in (0, 3)
    assert secret in result.stderr + result.stdout
    assert "do-not-print" not in result.stderr + result.stdout
    assert not rig.path("opt/black-relay/cameralink").exists()


def test_the_env_example_alone_does_not_stop_the_install(rig):
    example = rig.path("opt/black-relay/src/relay/.env.example")
    example.parent.mkdir(parents=True)
    example.write_text("RELAY_API_TOKEN=\n")
    assert rig.install().returncode == 0


def test_creates_the_service_user_once(rig):
    rig.install()
    rig.install()
    assert sum(c.startswith("useradd ") for c in rig.calls()) == 1


# --- the model ---------------------------------------------------------------


def test_copies_the_model_when_its_hash_matches(rig):
    assert rig.install().returncode == 0
    assert rig.path("var/lib/frigate/config/model_cache/yolov6n.hef").read_bytes() == b"model"


def test_the_real_hash_is_the_default(rig):
    text = SCRIPT.read_text()
    assert f"BLACK_RELAY_HEF_SHA256:-{GOOD_HEF_SHA}" in text


def test_a_wrong_model_hash_fails_and_installs_nothing(rig):
    result = rig.run("--hef", str(rig.hef), good_sha="0" * 64)
    assert result.returncode == 1
    assert "INSTALL FAILED" in result.stderr
    assert "yolov6n.hef" in result.stderr
    assert not rig.path("var/lib/frigate/config/model_cache/yolov6n.hef").exists()
    assert not rig.path("etc/nftables.conf").exists()


def test_no_model_given_fails_on_first_install_and_passes_on_a_rerun(rig):
    first = rig.run()
    assert first.returncode == 1
    assert "--hef" in first.stderr
    assert rig.install().returncode == 0
    assert rig.run().returncode == 0


def test_a_damaged_installed_model_with_no_source_fails(rig):
    rig.install()
    rig.path("var/lib/frigate/config/model_cache/yolov6n.hef").write_bytes(b"damaged")
    assert rig.run().returncode == 1


def test_the_installed_model_can_be_its_own_source(rig):
    rig.install()
    installed = rig.path("var/lib/frigate/config/model_cache/yolov6n.hef")
    assert rig.run("--hef", str(installed)).returncode == 0
    assert installed.read_bytes() == b"model"


# --- never erases a saved broker -----------------------------------------------


def test_a_saved_broker_survives_a_rerun(rig):
    rig.install()
    rig.path("etc/black-relay/central-hosts.nft").write_text(SAVED_BROKER)
    assert rig.install().returncode == 0
    assert rig.path("etc/black-relay/central-hosts.nft").read_text() == SAVED_BROKER


def test_a_second_run_changes_no_file(rig):
    rig.install()
    second = rig.install()
    assert second.returncode == 0
    assert "Files changed: 0" in second.stdout


# --- what it never does ---------------------------------------------------------


@pytest.mark.parametrize("flags", [(), ("--purge-tailscale",)])
def test_never_reboots_starts_loads_the_firewall_or_installs_hailo_all(rig, flags):
    rig.set("wired", "table")
    rig.install(*flags)
    for call in rig.calls():
        word = call.split()
        assert word[0] not in ("reboot", "shutdown", "poweroff"), call
        assert "hailo-all" not in call or call == "dpkg -s hailo-all", call
        assert not (word[0] == "apt-get" and "install" in word), call
        assert not (word[0] == "nft" and "-f" in word and "-c" not in word), call
        if word[0] == "systemctl":
            assert word[1] in ("daemon-reload", "enable", "mask"), call


def test_frigate_is_not_enabled_and_wait_online_is_masked_after_the_enable(rig):
    rig.install()
    calls = rig.calls()
    enable = next(c for c in calls if c.startswith("systemctl enable"))
    for unit in (
        "nftables.service",
        "NetworkManager.service",
        "mosquitto.service",
        "systemd-timesyncd.service",
        "camera-provision.service",
        "camerabridge.service",
        "camerabridge-config.path",
        "docker.service",
        "camera-link.service",
        "camera-link-dhcp.service",
        "black-relay-first-boot.service",
    ):
        assert unit in enable.split()
    assert "frigate.service" not in enable
    mask = "systemctl mask NetworkManager-wait-online.service"
    assert calls.index(mask) > calls.index(enable)


def test_start_is_only_on_request_and_never_touches_firewall_or_frigate(rig):
    rig.set("table")
    assert rig.install("--start").returncode == 0
    starts = [c for c in rig.calls() if c.startswith("systemctl start")]
    assert starts
    for call in starts:
        assert "nftables" not in call and "frigate" not in call
    assert not any(" restart " in f" {c} " or " stop " in f" {c} " for c in rig.calls())


def test_start_refuses_with_no_live_firewall(rig):
    result = rig.install("--start")
    assert result.returncode == 1
    assert "reboot" in result.stderr
    assert not any(c.startswith("systemctl start") for c in rig.calls())


# --- Wired connection 1 and open sessions ----------------------------------------


def session_on(rig, local_address):
    (rig.state / "ss").write_text(f"0 0 {local_address}:22 10.0.0.9:50000\n")


def test_deletes_the_wired_profile(rig):
    rig.set("wired")
    session_on(rig, "192.168.1.206")
    assert rig.install().returncode == 0
    assert "nmcli connection delete Wired connection 1" in rig.calls()


def test_a_missing_wired_profile_is_fine(rig):
    assert rig.install().returncode == 0
    assert not any(c.startswith("nmcli connection delete") for c in rig.calls())


def test_a_session_on_eth0_is_not_cut(rig):
    rig.set("wired")
    session_on(rig, "10.0.0.5")
    keyfile = rig.path("etc/NetworkManager/system-connections/Wired connection 1.nmconnection")
    keyfile.write_text("[connection]\nid=Wired connection 1\n")
    result = rig.install()
    assert result.returncode == 0
    assert not any(c.startswith("nmcli connection") and "show" not in c for c in rig.calls())
    assert not keyfile.exists()
    assert "eth0" in result.stdout and "reboot" in result.stdout


def test_the_camera_profile_is_not_loaded_so_nothing_moves_eth0(rig):
    rig.install()
    assert not any(c.startswith("nmcli connection reload") for c in rig.calls())
    assert not any(c.startswith("nmcli connection up") for c in rig.calls())


def test_with_networkmanager_down_the_keyfile_goes(rig):
    rig.set("nm-down")
    keyfile = rig.path("etc/NetworkManager/system-connections/Wired connection 1.nmconnection")
    keyfile.write_text("[connection]\nid=Wired connection 1\n")
    other = rig.path("etc/NetworkManager/system-connections/other.nmconnection")
    other.write_text("[connection]\nid=other\n")
    assert rig.install().returncode == 0
    assert not keyfile.exists()
    assert other.exists()


# --- failing loudly ----------------------------------------------------------------


@pytest.mark.parametrize(
    "flag, step",
    [("pip", "Python packages"), ("enable", "Enabling"), ("nm-delete", "Wired connection 1")],
)
def test_a_failing_step_is_named_and_stops_the_run(rig, flag, step):
    rig.set(f"fail-{flag}", "wired")
    result = rig.install()
    assert result.returncode == 1
    assert "INSTALL FAILED" in result.stderr
    assert step in result.stderr


def test_a_firewall_that_does_not_parse_is_not_installed(rig):
    rig.set("fail-nft-check")
    rig.path("etc/nftables.conf").write_text("the old one\n")
    result = rig.install()
    assert result.returncode == 1
    assert "firewall" in result.stderr.lower()
    assert rig.path("etc/nftables.conf").read_text() == "the old one\n"


def test_a_failed_unit_check_is_reported_as_not_ready(rig):
    rig.set("fail-verify")
    result = rig.install()
    assert result.returncode == 3
    assert "systemd-analyze verify" in result.stdout


def test_refuses_an_unknown_argument(rig):
    assert rig.run("--reboot").returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="needs a non-root user")
def test_a_real_run_needs_root():
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "root" in result.stderr


def test_a_missing_tool_is_named_before_anything_is_installed(rig):
    (rig.bin / "nft").unlink()
    env_path = f"{rig.bin}"
    tools = rig.bin / "real"
    tools.mkdir()
    for tool in (
        "bash cut grep sha256sum install mktemp cmp mv rm mkdir dirname basename readlink awk sed "
        "timeout cat stat"
    ).split():
        os.symlink(shutil.which(tool), tools / tool)
    env = {
        "PATH": f"{env_path}:{tools}",
        "BLACK_RELAY_ROOT": str(rig.root),
        "STUB_LOG": str(rig.log),
        "STUB_STATE": str(rig.state),
    }
    result = subprocess.run(
        [str(tools / "bash"), str(SCRIPT), "--hef", str(rig.hef)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "nft" in result.stderr
    assert not rig.path("etc/nftables.conf").exists()


# --- readiness and the summary -------------------------------------------------------


@pytest.mark.parametrize(
    "flag, word",
    [
        ("no-driver", "hailo_pci"),
        ("no-image", "0.18.2"),
        ("docker-down", "docker"),
        ("hailo-all", "hailo-all"),
    ],
)
def test_what_the_installer_cannot_supply_means_not_ready(rig, flag, word):
    rig.set(flag)
    result = rig.install()
    assert result.returncode == 3
    assert word in result.stdout
    assert "NOT READY" in result.stdout


def test_ready_is_said_only_when_every_check_passed(rig):
    result = rig.install()
    assert result.returncode == 0
    assert "READY" in result.stdout and "NOT READY" not in result.stdout


def test_the_summary_warns_of_the_reboot_with_nobody_admitted(rig):
    output = rig.install().stdout
    assert "reboot" in output.lower()
    assert "NOBODY" in output


def test_the_summary_names_the_admitted_address(rig):
    rig.install()
    rig.path("etc/black-relay/central-hosts.nft").write_text(SAVED_BROKER)
    output = rig.install().stdout
    assert "192.168.1.22" in output and "NOBODY" not in output


def test_the_image_reference_comes_from_the_unit(rig):
    rig.install()
    unit = (ROOT / "cameralink/ops/frigate.service").read_text()
    reference = next(w for w in unit.split() if w.startswith("ghcr.io/"))
    assert f"docker image inspect {reference}" in rig.calls()


# --- persistent journal ------------------------------------------------------------------


def test_the_journal_directory_exists_and_is_applied_at_reboot(rig):
    rig.install()
    assert rig.path("var/log/journal").is_dir()
    assert not any("systemd-journald" in c for c in rig.calls())


# --- --purge-tailscale (DESIGN 4.2: nodes do not run Tailscale) ----------------------------


def test_tailscale_is_left_alone_without_the_flag(rig):
    rig.set("tailscale")
    rig.install()
    assert not any(c.endswith(" tailscale") or c.startswith("tailscale") for c in rig.calls())
    assert not any("tailscale0" in c or "tailscaled" in c for c in rig.calls())


def test_purge_removes_the_package_login_and_files(rig):
    rig.set("tailscale")
    rig.path("var/lib/tailscale").mkdir(parents=True)
    rig.path("etc/apt/sources.list.d").mkdir(parents=True)
    rig.path("etc/apt/sources.list.d/tailscale.list").write_text("deb x\n")
    rig.path("usr/share/keyrings").mkdir(parents=True)
    rig.path("usr/share/keyrings/tailscale-archive-keyring.gpg").write_text("key\n")
    rig.path("var/lib/camera-provision").mkdir(parents=True)
    rig.path("var/lib/camera-provision/tailscale.key").write_text("old\n")
    assert rig.install("--purge-tailscale").returncode == 0
    calls = rig.calls()
    assert calls.index("tailscale logout") < calls.index("systemctl disable --now tailscaled.service")
    assert any(c.startswith("apt-get") and "purge" in c and "tailscale" in c for c in calls)
    for gone in (
        "var/lib/tailscale",
        "etc/apt/sources.list.d/tailscale.list",
        "usr/share/keyrings/tailscale-archive-keyring.gpg",
        "var/lib/camera-provision/tailscale.key",
    ):
        assert not rig.path(gone).exists(), gone


def test_purge_is_safe_to_repeat(rig):
    assert rig.install("--purge-tailscale").returncode == 0
    assert not any(c.startswith("apt-get") for c in rig.calls())
    assert not any(c.startswith("tailscale") for c in rig.calls())


def test_purge_refuses_while_a_session_runs_over_tailscale(rig):
    rig.set("tailscale", "ts-if")
    session_on(rig, "100.64.0.9")
    result = rig.install("--purge-tailscale")
    assert result.returncode == 1
    assert "tailscale0" in result.stderr
    assert not any(c.startswith("apt-get") for c in rig.calls())


def test_a_failed_ss_counts_as_a_session_on_eth0(rig):
    rig.set("wired")
    (rig.bin / "ss").write_text("#!/bin/bash\nexit 1\n")
    assert rig.install().returncode == 0
    assert not any(c.startswith("nmcli connection delete") for c in rig.calls())


# --- shellcheck, where it is installed --------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck_passes():
    subprocess.run(["shellcheck", str(SCRIPT)], check=True)


def test_install_node_is_executable():
    assert os.access(SCRIPT, os.X_OK)
