"""setup-credentials.sh and change-passwords.sh, run for real on files in a scratch directory.

Never the real relay/.env. Docker is a stub, and every other command change-passwords.sh may run
is a logging wrapper, alone on PATH, so that any argument holding the password would show.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from cameraprovision.config import frigate_problems

RELAY = Path(__file__).resolve().parents[2]
SETUP = RELAY / "setup-credentials.sh"
CHANGE = RELAY / "change-passwords.sh"
BASH = shutil.which("bash") or "/bin/bash"
DEFAULT = "BlackRelay2026"
PEOPLE = ("RELAY_BROKER_PASSWORD", "RELAY_READ_PASSWORD")
RANDOM = (
    "RELAY_API_TOKEN",
    "RELAY_API_BROKER_PASSWORD",
    "RELAY_MQTT_CLIENT_ID",
    "RELAY_MEDIAMTX_API_PASSWORD",
)
NEW = "N3w-pass#word%2F:x"
KIT_COMMAND = "sudo /opt/black-relay/ops/change-passwords.sh broker"
# What change-passwords.sh may run besides builtins. Anything else is "command not found".
WRAPPED = ("dirname", "mktemp", "chmod", "chown", "mv", "rm")


def values(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            found[name] = value.strip("'")
    return found


def setup(output: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(SETUP), *args, "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )


# --- setup-credentials.sh ---


def test_the_default_is_defined_once_as_a_named_constant() -> None:
    script = SETUP.read_text()
    assert script.count(DEFAULT) == 1
    assert f"readonly DEFAULT_PASSWORD='{DEFAULT}'" in script


def test_the_default_is_a_password_a_kits_portal_accepts() -> None:
    assert frigate_problems("cam-01", [], "relay", DEFAULT) == []


def test_setup_gives_the_people_passwords_the_default_and_the_rest_random(tmp_path: Path) -> None:
    one, two = tmp_path / "one.env", tmp_path / "two.env"
    for path in (one, two):
        result = setup(path)
        assert result.returncode == 0, result.stderr
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert DEFAULT not in result.stdout + result.stderr
    first, second = values(one), values(two)
    for name in PEOPLE:
        assert first[name] == second[name] == DEFAULT
    assert first["RELAY_BROKER_USER"] == "relay"
    assert first["RELAY_READ_USER"] == "viewer"
    for name in RANDOM:
        assert len(first[name]) >= 32, name
        assert first[name] != second[name], name
        assert DEFAULT not in first[name], name


def test_force_keeps_the_people_passwords_and_replaces_the_random_ones(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    assert setup(path).returncode == 0
    text = path.read_text()
    text = re.sub(r"(?m)^RELAY_BROKER_PASSWORD=.*$", "RELAY_BROKER_PASSWORD='kits-own-1'", text)
    text = re.sub(r"(?m)^RELAY_READ_PASSWORD=.*$", "RELAY_READ_PASSWORD='viewers-own-1'", text)
    path.write_text(text)
    before = values(path)
    result = setup(path, "--force")
    assert result.returncode == 0, result.stderr
    after = values(path)
    assert after["RELAY_BROKER_PASSWORD"] == "kits-own-1"
    assert after["RELAY_READ_PASSWORD"] == "viewers-own-1"
    for name in RANDOM:
        assert after[name] != before[name], name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "kits need no change" in result.stdout


def test_force_gives_an_empty_people_password_the_default(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    assert setup(path).returncode == 0
    text = re.sub(r"(?m)^RELAY_BROKER_PASSWORD=.*$", "RELAY_BROKER_PASSWORD=''", path.read_text())
    text = re.sub(r"(?m)^RELAY_READ_PASSWORD=.*$", "RELAY_READ_PASSWORD=", text)
    path.write_text(text)
    assert setup(path, "--force").returncode == 0
    for name in PEOPLE:
        assert values(path)[name] == DEFAULT


# --- change-passwords.sh ---


class Relay:
    """A scratch .env and a PATH holding only logging wrappers and a stub docker."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.env_file = tmp_path / "relay.env"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "argv.log"
        self.bin.mkdir()
        self.log.touch()
        for name in WRAPPED:
            real = shutil.which(name)
            assert real, name
            self._write(name, f'printf "%s\\n" "{name} $*" >> "$ARGV_LOG"\nexec {real} "$@"\n')
        self._write(
            "docker",
            'printf "%s\\n" "docker $* (in $PWD)" >> "$ARGV_LOG"\nexit "${DOCKER_EXIT:-0}"\n',
        )
        result = setup(self.env_file)
        assert result.returncode == 0, result.stderr
        # A file the scripts did not write: comments, odd spacing and no newline at the end.
        with self.env_file.open("a") as handle:
            handle.write("# a comment with RELAY_BROKER_PASSWORD= in it\n  SPACED = x \nLAST=1")

    def _write(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{BASH}\n{body}")
        path.chmod(0o755)

    def run(
        self, *args: str, stdin: str, trace: bool = False, docker_exit: int = 0
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": str(self.bin),
            "RELAY_ENV_FILE": str(self.env_file),
            "ARGV_LOG": str(self.log),
            "DOCKER_EXIT": str(docker_exit),
        }
        return subprocess.run(
            [BASH, *(["-x"] if trace else []), str(CHANGE), *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def change(
        self, who: str, password: str, again: str | None = None, **kw
    ) -> subprocess.CompletedProcess:
        return self.run(who, stdin=f"{password}\n{password if again is None else again}\n", **kw)

    def argv(self) -> str:
        return self.log.read_text()


@pytest.fixture
def relay(tmp_path: Path) -> Relay:
    return Relay(tmp_path)


def _no_leak(relay: Relay, result: subprocess.CompletedProcess, password: str) -> None:
    assert "command not found" not in result.stderr
    assert password not in result.stdout
    assert password not in result.stderr
    assert password not in relay.argv()


@pytest.mark.parametrize(("who", "key"), [("broker", PEOPLE[0]), ("viewer", PEOPLE[1])])
def test_a_change_replaces_exactly_one_value(relay: Relay, who: str, key: str) -> None:
    before = relay.env_file.read_bytes()
    result = relay.change(who, NEW)
    assert result.returncode == 0, result.stderr
    after = relay.env_file.read_bytes()
    old_lines, new_lines = before.split(b"\n"), after.split(b"\n")
    assert len(old_lines) == len(new_lines)
    changed = [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b]
    assert changed == [(f"{key}='{DEFAULT}'".encode(), f"{key}='{NEW}'".encode())]
    assert after.endswith(b"\nLAST=1")
    assert stat.S_IMODE(relay.env_file.stat().st_mode) == 0o600
    assert f"docker compose --env-file {relay.env_file} up -d (in {RELAY})" in relay.argv()
    assert not [p for p in relay.tmp.iterdir() if p.name.startswith("relay.env.")]
    _no_leak(relay, result, NEW)


def test_a_broker_change_tells_the_operator_to_change_every_kit(relay: Relay) -> None:
    result = relay.change("broker", NEW)
    assert result.returncode == 0
    assert "Every kit must now get the same password" in result.stdout
    assert KIT_COMMAND in result.stdout


def test_a_viewer_change_does_not_send_anyone_to_the_kits(relay: Relay) -> None:
    result = relay.change("viewer", NEW)
    assert result.returncode == 0
    assert KIT_COMMAND not in result.stdout


def test_a_trace_shows_no_password(relay: Relay) -> None:
    result = relay.change("broker", NEW, trace=True)
    assert result.returncode == 0, result.stderr
    _no_leak(relay, result, NEW)


@pytest.mark.parametrize("password", ["Abcdef1!", "a" * 63, "!#%&()*+,-./:;<=>?@[]^_{|}~0"])
def test_the_edges_of_the_rule_are_accepted(relay: Relay, password: str) -> None:
    result = relay.change("viewer", password)
    assert result.returncode == 0, result.stderr
    assert values(relay.env_file)["RELAY_READ_PASSWORD"] == password


def _unchanged(
    relay: Relay, result: subprocess.CompletedProcess, before: bytes, password: str
) -> None:
    assert result.returncode != 0
    assert "Nothing changed" in result.stderr
    assert relay.env_file.read_bytes() == before
    assert stat.S_IMODE(relay.env_file.stat().st_mode) == 0o600
    assert "docker" not in relay.argv()
    assert not [p for p in relay.tmp.iterdir() if p.name.startswith("relay.env.")]
    _no_leak(relay, result, password)


@pytest.mark.parametrize("who", ["broker", "viewer"])
def test_a_mismatch_changes_nothing(relay: Relay, who: str) -> None:
    before = relay.env_file.read_bytes()
    result = relay.change(who, NEW, again=NEW + "x")
    _unchanged(relay, result, before, NEW)
    assert "differ" in result.stderr


@pytest.mark.parametrize("password", ["Short1!", "a" * 64, ""])
def test_a_bad_length_changes_nothing(relay: Relay, password: str) -> None:
    before = relay.env_file.read_bytes()
    result = relay.change("broker", password)
    assert result.returncode != 0
    assert relay.env_file.read_bytes() == before
    assert "docker" not in relay.argv()
    if password:
        _unchanged(relay, result, before, password)


@pytest.mark.parametrize("character", [" ", "'", '"', "\\", "$", "`", "\t", "é"])
@pytest.mark.parametrize("who", ["broker", "viewer"])
def test_each_forbidden_character_changes_nothing(relay: Relay, who: str, character: str) -> None:
    password = f"Good{character}pass99"
    before = relay.env_file.read_bytes()
    _unchanged(relay, relay.change(who, password), before, password)


@pytest.mark.parametrize("prefix", ["sha256:", "argon2:"])
def test_a_viewer_password_mediamtx_would_read_as_a_hash_changes_nothing(
    relay: Relay, prefix: str
) -> None:
    password = prefix + "abcdefgh"
    before = relay.env_file.read_bytes()
    _unchanged(relay, relay.change("viewer", password), before, password)


def test_no_input_changes_nothing(relay: Relay) -> None:
    before = relay.env_file.read_bytes()
    result = relay.run("broker", stdin="")
    assert result.returncode != 0
    assert relay.env_file.read_bytes() == before
    assert "docker" not in relay.argv()


@pytest.mark.parametrize("args", [(), ("both",), ("broker", "viewer"), ("BROKER",)])
def test_anything_but_one_known_name_prints_the_usage(relay: Relay, args: tuple[str, ...]) -> None:
    before = relay.env_file.read_bytes()
    result = relay.run(*args, stdin=f"{NEW}\n{NEW}\n")
    assert result.returncode == 2
    assert "usage:" in result.stderr and "broker|viewer" in result.stderr
    assert relay.env_file.read_bytes() == before


def test_it_refuses_to_run_without_an_env_file(relay: Relay) -> None:
    relay.env_file.unlink()
    result = relay.change("broker", NEW)
    assert result.returncode == 1
    assert "setup-credentials.sh first" in result.stderr
    assert not relay.env_file.exists()
    assert "docker" not in relay.argv()


def test_it_refuses_a_file_with_two_lines_for_the_password(relay: Relay) -> None:
    with relay.env_file.open("a") as handle:
        handle.write(f"\nRELAY_READ_PASSWORD='{DEFAULT}'\n")
    before = relay.env_file.read_bytes()
    result = relay.change("viewer", NEW)
    assert result.returncode == 1
    assert "Nothing changed" in result.stderr
    assert relay.env_file.read_bytes() == before


def test_a_failed_compose_is_reported_and_the_file_keeps_the_new_password(relay: Relay) -> None:
    result = relay.change("broker", NEW, docker_exit=3)
    assert result.returncode == 1
    assert "docker compose up -d failed" in result.stderr
    assert values(relay.env_file)["RELAY_BROKER_PASSWORD"] == NEW
    _no_leak(relay, result, NEW)


def test_the_script_is_executable() -> None:
    assert os.access(CHANGE, os.X_OK)
    assert os.access(SETUP, os.X_OK)
