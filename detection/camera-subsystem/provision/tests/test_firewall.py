"""The node firewall file: who it admits, and that nft accepts it with its include."""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

import pytest

from cameraprovision.config import render_central_hosts
from cameraprovision.store import Paths

OPS = Path(__file__).parents[1] / "ops"
NFT = OPS / "black-relay.nft"
EMPTY_SET = OPS / "central-hosts.nft"
SSH_GUARD = OPS / "ssh-needs-firewall.conf"
PROVISION_UNIT = OPS / "camera-provision.service"


def rules() -> list[str]:
    """The file's statements, without comments."""
    return [
        line.strip()
        for line in NFT.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_set_is_declared_in_the_rule_file_itself() -> None:
    lines = rules()
    start = lines.index("set central_hosts {")
    assert lines[start + 1:start + 3] == ["type ipv4_addr", "}"]
    assert start > lines.index("table inet black_relay {")


def test_the_elements_come_through_a_glob_after_the_table() -> None:
    lines = rules()
    include = 'include "/etc/black-relay/central-hosts*.nft"'
    assert lines.count(include) == 1
    assert lines[-1] == include
    assert fnmatch.fnmatch(str(Paths().central_hosts), include.split('"')[1])


@pytest.mark.parametrize("port", ["$SSH_PORT", "$FRIGATE_UI_PORTS", "$RTSP_PORT"])
def test_each_central_service_is_open_only_to_central_hosts_on_wlan0(port: str) -> None:
    accepting = [line for line in rules() if f"tcp dport {port}" in line and "accept" in line]
    assert accepting == [f"iifname $WIFI_IF ip saddr @central_hosts tcp dport {port} counter accept"]


def test_the_defined_ports_are_the_real_ones() -> None:
    lines = rules()
    assert "define SSH_PORT = 22" in lines
    assert "define FRIGATE_UI_PORTS = { 8971 }" in lines
    assert "define RTSP_PORT = 8554" in lines


def test_link_local_ipv6_cannot_reach_the_central_services() -> None:
    assert "iifname $WIFI_IF ip6 daddr fe80::/10 tcp dport { 22, 8554, 8971 } drop" in rules()


def test_nothing_about_tailscale_or_port_5000_remains() -> None:
    text = NFT.read_text().lower()
    for word in ("tailscale", "tailnet", "41641", "5000"):
        assert word not in text


def test_the_setup_ap_rules_stay() -> None:
    lines = rules()
    assert "iifname $WIFI_IF ip saddr $AP_NET ip daddr $AP_IP tcp dport 80 counter accept" in lines
    assert "iifname $WIFI_IF ip saddr $AP_NET counter drop" in lines


def test_every_accept_rule_is_exactly_these() -> None:
    # Any new way in has to be added here, on purpose.
    assert [line for line in rules() if line.split()[-1] == "accept"] == [
        'iifname "lo" accept',
        "iifname $WIFI_IF icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, "
        "nd-router-advert, mld-listener-query } accept",
        "ct state established,related accept",
        "iifname $WIFI_IF ip saddr $AP_NET ip daddr $AP_IP tcp dport 80 counter accept",
        "iifname $WIFI_IF ip saddr $AP_NET ip daddr $AP_IP meta l4proto { tcp, udp } th dport 53 "
        "counter accept",
        "iifname $WIFI_IF ip saddr { 0.0.0.0, $AP_NET } ip daddr { 255.255.255.255, $AP_IP } "
        "udp sport 68 udp dport 67 counter accept",
        "iifname $WIFI_IF udp sport 67 udp dport 68 counter accept",
        "iifname $WIFI_IF ip6 saddr fe80::/10 udp sport 547 udp dport 546 accept",
        "iifname $WIFI_IF ip daddr 224.0.0.251 udp dport 5353 accept",
        "iifname $WIFI_IF ip6 daddr ff02::fb udp dport 5353 accept",
        "iifname $WIFI_IF icmp type echo-request accept",
        "iifname $WIFI_IF icmpv6 type echo-request accept",
        "iifname $WIFI_IF ip saddr @central_hosts tcp dport $SSH_PORT counter accept",
        "iifname $WIFI_IF ip saddr @central_hosts tcp dport $FRIGATE_UI_PORTS counter accept",
        "iifname $WIFI_IF ip saddr @central_hosts tcp dport $RTSP_PORT counter accept",
        "iifname $CAMERA_IF udp sport 68 udp dport 67 counter accept",
    ]


def test_both_chains_default_to_drop() -> None:
    lines = rules()
    assert "type filter hook input priority filter; policy drop;" in lines
    assert "type filter hook forward priority filter; policy drop;" in lines
    assert sum("policy" in line for line in lines) == 2


def test_each_interface_ends_in_a_drop_after_its_accepts() -> None:
    lines = rules()
    for interface in ("$WIFI_IF", "$CAMERA_IF"):
        drop = lines.index(f"iifname {interface} counter drop")
        accepts = [
            at
            for at, line in enumerate(lines)
            if line.startswith(f"iifname {interface}") and line.endswith("accept")
        ]
        assert drop > max(accepts)


def test_the_broker_and_the_ap_are_dropped_ahead_of_the_site_lan_rules() -> None:
    lines = rules()
    site = lines.index(
        "iifname $WIFI_IF ip saddr @central_hosts tcp dport $SSH_PORT counter accept"
    )
    assert lines.index("meta l4proto { tcp, udp } th dport 1883 counter drop") < site
    assert lines.index("iifname $WIFI_IF ip saddr $AP_NET counter drop") < site
    assert lines.index("iifname $WIFI_IF ip daddr $AP_IP counter drop") < site


def test_the_image_starts_with_no_elements() -> None:
    assert [
        line for line in EMPTY_SET.read_text().splitlines() if line and not line.startswith("#")
    ] == []


# --- the boot paths, in a private network namespace ---------------------------------


def nft_in_namespace(directory: Path, script: str) -> subprocess.CompletedProcess[str]:
    nft = shutil.which("nft") or "/usr/sbin/nft"
    if not Path(nft).exists():
        pytest.skip("nft is not installed")
    ruleset = directory.parent / "black-relay.nft"
    ruleset.write_text(NFT.read_text().replace("/etc/black-relay/", f"{directory}/"))
    result = subprocess.run(
        ["unshare", "-rn", "sh", "-c", script.format(nft=nft, ruleset=ruleset)],
        capture_output=True,
        text=True,
        check=False,
    )
    if "unshare" in result.stderr and "not permitted" in result.stderr:
        pytest.skip("unprivileged user namespaces are off here")
    return result


LOAD_AND_LIST = (
    "{nft} -c -f {ruleset} && {nft} -f {ruleset} && {nft} list set inet black_relay central_hosts"
)


@pytest.fixture
def elements_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "black-relay"
    directory.mkdir()
    return directory


def test_a_missing_elements_file_loads_the_firewall_and_admits_nobody(tmp_path: Path) -> None:
    result = nft_in_namespace(tmp_path / "no-such-directory", LOAD_AND_LIST)
    assert result.returncode == 0, result.stderr
    assert "elements" not in result.stdout


def test_a_zero_byte_elements_file_loads_the_firewall_and_admits_nobody(elements_dir: Path) -> None:
    (elements_dir / "central-hosts.nft").write_text("")
    result = nft_in_namespace(elements_dir, LOAD_AND_LIST)
    assert result.returncode == 0, result.stderr
    assert "elements" not in result.stdout


def test_the_image_file_loads_the_firewall_and_admits_nobody(elements_dir: Path) -> None:
    (elements_dir / "central-hosts.nft").write_text(EMPTY_SET.read_text())
    result = nft_in_namespace(elements_dir, LOAD_AND_LIST)
    assert result.returncode == 0, result.stderr
    assert "elements" not in result.stdout


def test_a_saved_file_loads_the_firewall_and_admits_the_central_node(elements_dir: Path) -> None:
    (elements_dir / "central-hosts.nft").write_text(render_central_hosts("10.0.0.2"))
    result = nft_in_namespace(elements_dir, LOAD_AND_LIST)
    assert result.returncode == 0, result.stderr
    assert "10.0.0.2" in result.stdout


def test_the_stores_working_files_are_not_included(elements_dir: Path) -> None:
    for name in (".central-hosts.nft.staged.tmp", ".central-hosts.nft.prev.tmp"):
        (elements_dir / name).write_text("this is not nft {")
    result = nft_in_namespace(elements_dir, LOAD_AND_LIST)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "damaged",
    ["add element inet black_relay central_hosts { 10.0", "garbage", "\x00\x00\x00"],
    ids=["truncated", "garbage", "nul-blocks"],
)
def test_a_damaged_elements_file_fails_the_load_so_the_guards_must_hold(
    elements_dir: Path, damaged: str
) -> None:
    """nft loads all or nothing, so this boot has no table. sshd and Frigate must refuse."""
    (elements_dir / "central-hosts.nft").write_text(damaged)
    result = nft_in_namespace(elements_dir, "{nft} -c -f {ruleset}")
    assert result.returncode != 0
    table_check = "/bin/sh -c '/usr/sbin/nft list table inet black_relay >/dev/null'"
    assert f"ExecStartPre={table_check}" in SSH_GUARD.read_text().splitlines()
    assert f"ExecStartPre={table_check}" in PROVISION_UNIT.read_text().splitlines()


def test_a_damaged_file_on_reload_keeps_the_rules_in_force(elements_dir: Path) -> None:
    good = elements_dir / "central-hosts.nft"
    good.write_text(render_central_hosts("10.0.0.2"))
    script = (
        "{nft} -f {ruleset} && "
        f"printf 'garbage' > {good}; "
        "{nft} -f {ruleset}; "
        "{nft} list set inet black_relay central_hosts"
    )
    result = nft_in_namespace(elements_dir, script)
    assert "10.0.0.2" in result.stdout


# --- what stops sshd and the daemon running without the firewall ---------------------


def test_sshd_needs_the_firewall() -> None:
    assert [
        line for line in SSH_GUARD.read_text().splitlines() if line and not line.startswith("#")
    ] == [
        "[Unit]",
        "After=nftables.service",
        "[Service]",
        "ExecStartPre=/bin/sh -c '/usr/sbin/nft list table inet black_relay >/dev/null'",
    ]


def test_the_daemon_needs_the_firewall() -> None:
    lines = PROVISION_UNIT.read_text().splitlines()
    assert "ExecStartPre=/bin/sh -c '/usr/sbin/nft list table inet black_relay >/dev/null'" in lines
    assert "After=NetworkManager.service nftables.service" in lines


# --- the image steps -----------------------------------------------------------------


def test_the_image_build_installs_every_firewall_file() -> None:
    readme = (OPS / "README.md").read_text()
    build = readme[readme.index("## Build the image") : readme.index("## Seed each kit")]
    for step in (
        "sudo install -m 0644 black-relay.nft /etc/nftables.conf",
        "sudo install -m 0644 central-hosts.nft /etc/black-relay/central-hosts.nft",
        "sudo install -m 0644 ssh-needs-firewall.conf "
        "/etc/systemd/system/ssh.service.d/black-relay.conf",
        "sudo install -m 0644 /opt/black-relay/ops/central-hosts.nft "
        "/etc/black-relay/central-hosts.nft",
    ):
        assert step in build, step
    assert "/etc/black-relay" in build and "ssh.service.d" in build


def test_the_readme_says_how_to_recover_a_damaged_file() -> None:
    readme = (OPS / "README.md").read_text()
    recovery = readme[readme.index("**If nftables fails at boot:**") :]
    assert (
        "sudo install -m 0644 /opt/black-relay/ops/central-hosts.nft "
        "/etc/black-relay/central-hosts.nft" in recovery
    )
    assert "sudo systemctl start nftables ssh" in recovery
