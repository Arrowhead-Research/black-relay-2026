"""The node firewall file: who it admits, and that nft accepts it with its include."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cameraprovision.config import render_central_hosts
from cameraprovision.store import Paths

OPS = Path(__file__).parents[1] / "ops"
NFT = OPS / "black-relay.nft"
EMPTY_SET = OPS / "central-hosts.nft"


def rules() -> list[str]:
    """The file's statements, without comments."""
    return [
        line.strip()
        for line in NFT.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_set_file_is_included_inside_the_table_from_where_the_store_writes_it() -> None:
    lines = rules()
    assert f'include "{Paths().central_hosts}"' in lines
    assert lines.index(f'include "{Paths().central_hosts}"') > lines.index("table inet black_relay {")


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


def test_the_image_starts_with_an_empty_set() -> None:
    text = EMPTY_SET.read_text()
    assert "set central_hosts {" in text
    assert "type ipv4_addr" in text
    assert "elements" not in text


@pytest.mark.parametrize(
    "set_file",
    [EMPTY_SET.read_text(), render_central_hosts("10.0.0.2")],
    ids=["empty", "saved"],
)
def test_nft_accepts_the_rules_with_the_include(tmp_path: Path, set_file: str) -> None:
    if not shutil.which("nft") and not Path("/usr/sbin/nft").exists():
        pytest.skip("nft is not installed")
    nft = shutil.which("nft") or "/usr/sbin/nft"
    included = tmp_path / "central-hosts.nft"
    included.write_text(set_file)
    ruleset = tmp_path / "black-relay.nft"
    ruleset.write_text(NFT.read_text().replace(str(Paths().central_hosts), str(included)))

    # A private network namespace: -c checks the rules against the kernel and applies nothing.
    result = subprocess.run(
        ["unshare", "-rn", nft, "-c", "-f", str(ruleset)],
        capture_output=True,
        text=True,
        check=False,
    )

    if "Operation not permitted" in result.stderr and "unshare" in result.stderr:
        pytest.skip("unprivileged user namespaces are off here")
    assert result.returncode == 0, result.stderr


