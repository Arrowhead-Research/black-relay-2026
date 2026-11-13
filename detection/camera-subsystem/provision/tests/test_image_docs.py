"""What the image build and the field run tell an operator to do cannot leak into a clone.

The rsync commands are taken from the documents and really run, on a scratch tree.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

OPS = Path(__file__).parents[1] / "ops"
README = (OPS / "README.md").read_text()
RUNBOOK = (OPS / "field-run-kit1.md").read_text()

NOT_FOR_A_NODE = [
    "relay/.env",
    "relay/compose.yaml",
    "docs/plans/x.md",
    "bridge/.env",
    "cameralink/.env.old",
    "field-run.local.env",
]
FOR_A_NODE = ["bridge/pyproject.toml", "cameralink/ops/install-node.sh", "provision/ops/README.md"]


def _build_copies() -> list[str]:
    """Each rsync that copies the source tree to a Pi, with its line continuations joined."""
    commands = []
    for text in (README, RUNBOOK):
        joined = text.replace("\\\n", " ")
        commands += [
            line.strip()
            for line in joined.splitlines()
            if re.match(r"\s*rsync -a\b", line) and "--exclude" in line
        ]
    return commands


def _step_10() -> str:
    start = README.index("10. **Remove anything site-specific")
    return README[start : README.index("11. **Capture the image once", start)]


def _clean_block() -> str:
    return _step_10().split("Then confirm, before powering off")[0]


def _verify_block() -> str:
    return _step_10().split("Then confirm, before powering off")[1]


def test_both_documents_have_a_build_copy() -> None:
    assert len(_build_copies()) == 2


@pytest.mark.parametrize("command", _build_copies())
def test_a_build_copy_leaves_every_secret_and_non_node_tree_behind(
    tmp_path: Path, command: str
) -> None:
    rsync = shutil.which("rsync")
    if rsync is None:
        pytest.skip("rsync is not on PATH")
    source, target = tmp_path / "source", tmp_path / "target"
    for name in NOT_FOR_A_NODE + FOR_A_NODE:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    excludes = [
        value.strip("'\"") for value in re.findall(r"--exclude\s+(\S+)", command)
    ]
    args = [rsync, "-a", *[f"--exclude={value}" for value in excludes], f"{source}/", f"{target}/"]
    subprocess.run(args, check=True)
    for name in NOT_FOR_A_NODE:
        assert not (target / name).exists(), name
    for name in FOR_A_NODE:
        assert (target / name).exists(), name


@pytest.mark.parametrize("path", ["/opt/black-relay/src/relay", "/opt/black-relay/src/docs"])
def test_step_10_removes_and_checks_the_non_node_trees(path: str) -> None:
    assert path in _clean_block()
    assert path in _verify_block()


def test_step_10_finds_any_secrets_file_left_in_the_source() -> None:
    for block in (_clean_block(), _verify_block()):
        assert "find /opt/black-relay/src" in block
        for pattern in ("-name .env", "-name '.env.*'", "-name '*.local.env'"):
            assert pattern in block


def _left_by_the_field_run() -> set[str]:
    """Every place outside the node's own trees that the runbook writes to, on the node."""
    found = set(re.findall(r"/root/[\w.-]+", RUNBOOK))
    found |= set(re.findall(r"/var/tmp/[\w.-]+", RUNBOOK))
    found |= set(re.findall(r"/etc/NetworkManager/dnsmasq-shared\.d/zz-[\w.-]+", RUNBOOK))
    found |= {"/root/hailo_pci.conf", "/tmp/adopt.log", "/tmp/ports.log", "/tmp/one-attempt.log"}
    return found


def test_the_field_run_leaves_something_this_test_can_see() -> None:
    left = _left_by_the_field_run()
    for path in ("/root/kit1-camera-saved", "/root/kit1-uplink-saved", "/root/central-hosts.saved"):
        assert path in left


@pytest.mark.parametrize("path", sorted(_left_by_the_field_run()))
def test_step_10_removes_and_checks_each_file_the_field_run_leaves(path: str) -> None:
    assert path in _clean_block()
    assert path in _verify_block()


def test_step_10_clears_a_kit_number_so_a_clone_takes_only_the_one_written_to_its_card() -> None:
    for name in ("black-relay-kit", "black-relay-label.txt", "black-relay-first-boot.log"):
        assert f"/boot/firmware/{name}" in _clean_block()
        assert f"/boot/firmware/{name}" in _verify_block()


def test_step_12_gives_each_card_its_kit_number() -> None:
    step = README[README.index("12. **Write the image") : README.index("## Seed each kit")]
    assert "/mnt/black-relay-kit" in step
    assert "first-boot.sh" in README[README.index("## Seed each kit") :]


def test_step_10_clears_the_seed_marker_so_a_clone_can_seed() -> None:
    assert "/etc/black-relay/kit-seeded" in _clean_block()
    assert "/etc/black-relay/kit-seeded" in _verify_block()


def test_identity_no_longer_depends_on_typed_commands() -> None:
    section = README[README.index("## Seed each kit") : README.index("## GPIO setup button")]
    code = "\n".join(re.findall(r"```bash\n(.*?)```", section, flags=re.S))
    assert "seed-kit.sh" in code
    for typed in ("ssh-keygen", "machine-id", "ssh_host_"):
        assert typed not in code


def _c52_block() -> str:
    section = RUNBOOK[RUNBOOK.index("## C52.") : RUNBOOK.index("## C53.")]
    return re.findall(r"```bash\n(.*?)```", section, flags=re.S)[0]


SUDO_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_LOG"
case "$1" in
  ls) [[ -e $STUB_STATE/no-profile ]] && exit 2 ;;
  nmcli) [[ -e $STUB_STATE/no-nmcli ]] && exit 10 ;;
esac
exit 0
"""


def _run_c52(tmp_path: Path, *flags: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    stub = tmp_path / "sudo"
    stub.write_text(SUDO_STUB)
    stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    for flag in flags:
        (tmp_path / flag).touch()
    result = subprocess.run(
        ["bash", "-c", _c52_block()],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "STUB_LOG": str(log), "STUB_STATE": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log.read_text().splitlines()


@pytest.mark.parametrize("flag", ["no-profile", "no-nmcli"])
def test_c52_stops_without_moving_the_uplink_if_the_ap_profile_is_missing(
    tmp_path: Path, flag: str
) -> None:
    result, calls = _run_c52(tmp_path, flag)
    assert "STOP" in result.stdout
    assert not [call for call in calls if call.startswith(("mv", "install", "reboot"))]


def test_c52_moves_the_uplink_and_reboots_when_the_ap_profile_is_there(tmp_path: Path) -> None:
    result, calls = _run_c52(tmp_path)
    assert "STOP" not in result.stdout
    assert [call.split()[0] for call in calls][-3:] == ["install", "mv", "reboot"]
    assert calls[0].startswith("ls")


def test_c52_gives_its_advice_for_a_missing_profile_before_the_block() -> None:
    section = RUNBOOK[RUNBOOK.index("## C52.") : RUNBOOK.index("## C53.")]
    assert section.index("If the AP profile is missing") < section.index("```bash")


def test_no_document_tells_anyone_to_add_a_second_laptop_address() -> None:
    assert "ip addr add" not in RUNBOOK
    assert "alias ssh" not in RUNBOOK


SECRET_IN_ARGV = [
    r"mosquitto_(sub|pub)[^\n]*-P\s",
    r"rtsp://[^\s\"']*\$[^\s\"']*@",
    r"curl[^\n]*Authorization: Bearer",
]


@pytest.mark.parametrize("pattern", SECRET_IN_ARGV)
def test_the_runbook_puts_no_secret_in_argv(pattern: str) -> None:
    code = "\n".join(re.findall(r"```bash\n(.*?)```", RUNBOOK, flags=re.S)).replace("\\\n", " ")
    # A printf into a file or stdin is the safe form of the same text.
    lines = [line for line in code.splitlines() if not re.search(r"\b(printf|echo)\b", line)]
    assert not re.search(pattern, "\n".join(lines))


SITE_VALUES = ["Lab_Admin_Net", "192.168.1.", "mitmdump", "cam-8da040", "8da040"]


@pytest.mark.parametrize("value", SITE_VALUES)
@pytest.mark.parametrize("document", [OPS / "field-run-kit1.md", OPS / "README.md"])
def test_committed_documents_name_no_site(document: Path, value: str) -> None:
    assert value not in document.read_text()


def test_design_names_no_site_address() -> None:
    assert "192.168.1." not in (OPS.parents[1] / "DESIGN.md").read_text()
