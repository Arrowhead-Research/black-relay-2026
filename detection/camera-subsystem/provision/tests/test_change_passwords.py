"""change-passwords.sh replaces one of a kit's passwords, and nothing else.

Runs the real script on a scratch tree. Only what would change this machine is stubbed:
chpasswd, id, nmcli and the provisioning Python.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "ops" / "change-passwords.sh"
NEW = "A-new_Passw0rd"
PROFILE = """[connection]
id=black-relay-setup-ap
type=wifi
autoconnect=false

[wifi]
mode=ap
ssid=BlackRelay-CAM-10000000abcdef01

[wifi-security]
key-mgmt=wpa-psk
psk=BlackRelay2026

[ipv4]
method=shared
address1=10.42.0.1/24
"""

STUB = r"""#!/usr/bin/env bash
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$STUB_LOG"
case "$name" in
  id) [[ -e $STUB_STATE/no-user ]] && exit 1 || echo 1000 ;;
  chpasswd) [[ -e $STUB_STATE/fail-chpasswd ]] && exit 9; cat >> "$STUB_STATE/chpasswd.stdin" ;;
  python) [[ -e $STUB_STATE/fail-python ]] && exit 3; cat >> "$STUB_STATE/python.stdin" ;;
  nmcli)
    [[ $* == --offline* && -e $STUB_STATE/reject-profile ]] && exit 2
    [[ $* == "connection reload" && -e $STUB_STATE/fail-reload ]] && exit 8 ;;
esac
exit 0
"""
STUBBED = "id chpasswd python nmcli".split()


class Kit:
    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.nm_dir = tmp_path / "nm"
        self.log = tmp_path / "calls.log"
        for path in (self.bin, self.state, self.nm_dir):
            path.mkdir()
        for name in STUBBED:
            stub = self.bin / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.log.touch()
        self.profile = self.nm_dir / "black-relay-setup-ap.nmconnection"
        self.profile.write_text(PROFILE)
        self.profile.chmod(0o600)

    def run(
        self, *args: str, stdin: str | None = None, **extra: str
    ) -> subprocess.CompletedProcess:
        if stdin is None:
            stdin = f"{NEW}\n{NEW}\n"
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "NM_DIR": str(self.nm_dir),
            "PROVISION_PYTHON": str(self.bin / "python"),
            "SUDO_USER": "kit-2",
            "CHANGE_ALLOW_NON_ROOT": "1",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            **extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def given(self, name: str) -> str:
        path = self.state / name
        return path.read_text() if path.exists() else ""

    def changed_nothing(self) -> bool:
        return (
            self.profile.read_text() == PROFILE
            and self.given("chpasswd.stdin") == ""
            and self.given("python.stdin") == ""
            and not [p for p in self.nm_dir.iterdir() if p.name.startswith(".")]
        )


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


def secret_free(kit: Kit, result: subprocess.CompletedProcess, *, stdout: bool = True) -> bool:
    shown = result.stderr + (result.stdout if stdout else "")
    return NEW not in shown and NEW not in kit.log.read_text()


@pytest.mark.parametrize("args", [(), ("everything",), ("console", "broker"), ("--help",)])
def test_it_takes_exactly_one_of_three_words(kit: Kit, args: tuple) -> None:
    result = kit.run(*args)
    assert result.returncode != 0
    assert "console | setup-wifi | broker" in result.stderr
    assert kit.changed_nothing()


def test_it_needs_root(kit: Kit) -> None:
    if os.geteuid() == 0:
        pytest.skip("this test needs a non-root user")
    env = {"PATH": f"{kit.bin}:/usr/bin:/bin", "SUDO_USER": "kit-2"}
    result = subprocess.run(
        ["bash", str(SCRIPT), "console"],
        env=env,
        input="",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "run as root" in result.stderr


def test_the_console_password_goes_to_the_user_running_sudo_by_stdin_alone(kit: Kit) -> None:
    result = kit.run("console")
    assert result.returncode == 0, result.stderr
    assert kit.given("chpasswd.stdin") == f"kit-2:{NEW}\n"
    assert kit.profile.read_text() == PROFILE
    assert secret_free(kit, result)


def test_a_console_change_with_no_such_user_changes_nothing(kit: Kit) -> None:
    (kit.state / "no-user").touch()
    result = kit.run("console")
    assert result.returncode != 0
    assert "kit-2" in result.stderr
    assert kit.changed_nothing()


def test_the_setup_wifi_passphrase_is_the_only_line_of_the_profile_that_moves(kit: Kit) -> None:
    result = kit.run("setup-wifi")
    assert result.returncode == 0, result.stderr
    assert kit.profile.read_text() == PROFILE.replace("psk=BlackRelay2026", f"psk={NEW}")
    assert stat.S_IMODE(kit.profile.stat().st_mode) == 0o600
    assert "nmcli connection reload" in kit.log.read_text()
    assert not [p for p in kit.nm_dir.iterdir() if p.name.startswith(".")]


def test_a_setup_wifi_change_prints_the_new_label_and_nothing_else_shows_it(kit: Kit) -> None:
    result = kit.run("setup-wifi")
    assert result.stdout.splitlines() == [
        "BLACK RELAY CAMERA KIT",
        "Setup WiFi:  BlackRelay-CAM-10000000abcdef01",
        f"Passphrase:  {NEW}",
        "Setup page:  http://10.42.0.1/",
    ]
    assert secret_free(kit, result, stdout=False)
    assert "new label" in result.stderr


def test_the_new_label_is_shown_even_if_the_reload_fails(kit: Kit) -> None:
    (kit.state / "fail-reload").touch()
    result = kit.run("setup-wifi")
    assert result.returncode != 0
    assert f"Passphrase:  {NEW}" in result.stdout


def test_a_profile_networkmanager_rejects_is_not_installed(kit: Kit) -> None:
    (kit.state / "reject-profile").touch()
    result = kit.run("setup-wifi")
    assert result.returncode != 0
    assert kit.changed_nothing()
    assert result.stdout == ""


def test_a_kit_that_was_never_seeded_has_no_setup_wifi_to_change(kit: Kit) -> None:
    kit.profile.unlink()
    result = kit.run("setup-wifi")
    assert result.returncode != 0
    assert "seed-kit.sh" in result.stderr


def test_a_profile_with_no_passphrase_line_is_left_alone(kit: Kit) -> None:
    kit.profile.write_text(PROFILE.replace("psk=BlackRelay2026\n", ""))
    before = kit.profile.read_text()
    result = kit.run("setup-wifi")
    assert result.returncode != 0
    assert kit.profile.read_text() == before


def test_the_broker_password_reaches_the_kits_own_store_by_stdin_alone(kit: Kit) -> None:
    result = kit.run("broker")
    assert result.returncode == 0, result.stderr
    assert kit.given("python.stdin") == f"{NEW}\n"
    assert kit.profile.read_text() == PROFILE
    assert secret_free(kit, result)
    assert "relay" in result.stderr and "change-passwords.sh broker" in result.stderr


def test_a_broker_save_that_fails_says_so(kit: Kit) -> None:
    (kit.state / "fail-python").touch()
    result = kit.run("broker")
    assert result.returncode != 0
    assert "nothing was changed" in result.stderr
    assert "central node" not in result.stderr
    assert secret_free(kit, result)


def test_the_store_code_sent_to_python_holds_no_password_and_replaces_only_it() -> None:
    script = SCRIPT.read_text()
    code = script[script.index("import configparser") : script.index("' || die")]
    assert "sys.stdin.readline()" in code
    assert "replace(config.broker, password=password)" in code
    assert "store.save(" in code and "store.load()" in code
    compile(code, "change-passwords broker", "exec")


BAD = [
    "short1",
    "x" * 64,
    "has a space in it",
    "single'quote99",
    'double"quote99',
    "back\\slash999",
    "dollar$sign999",
    "back`tick99999",
    "tab\there9999",
    "café-password",
]


@pytest.mark.parametrize("what", ["console", "setup-wifi", "broker"])
@pytest.mark.parametrize("bad", BAD)
def test_a_password_the_other_places_could_not_take_changes_nothing(
    kit: Kit, what: str, bad: str
) -> None:
    result = kit.run(what, stdin=f"{bad}\n{bad}\n" * 3)
    assert result.returncode != 0
    assert kit.changed_nothing()
    assert "8 to 63 characters" in result.stderr
    assert bad not in result.stdout + result.stderr


@pytest.mark.parametrize("what", ["console", "setup-wifi", "broker"])
@pytest.mark.parametrize("stdin", ["", f"{NEW}\n", f"{NEW}\nsomething-else\n" * 3])
def test_a_missing_or_mismatched_entry_changes_nothing(kit: Kit, what: str, stdin: str) -> None:
    result = kit.run(what, stdin=stdin)
    assert result.returncode != 0
    assert kit.changed_nothing()
    assert secret_free(kit, result)


def test_a_mistyped_entry_can_be_retried(kit: Kit) -> None:
    result = kit.run("console", stdin=f"{NEW}\nwrong-entry\n{NEW}\n{NEW}\n")
    assert result.returncode == 0, result.stderr
    assert kit.given("chpasswd.stdin") == f"kit-2:{NEW}\n"


@pytest.mark.parametrize("good", ["BlackRelay2026", "8chars!!", "y" * 63, "a.b_c@d%e+f=g:h,i/j-k"])
def test_what_the_default_looks_like_is_accepted(kit: Kit, good: str) -> None:
    result = kit.run("console", stdin=f"{good}\n{good}\n")
    assert result.returncode == 0, result.stderr
    assert kit.given("chpasswd.stdin") == f"kit-2:{good}\n"


def _utf8_locale() -> str:
    names = subprocess.run(["locale", "-a"], capture_output=True, text=True, check=False).stdout
    for name in names.split():
        if name.lower().startswith("en_") and name.lower().endswith(("utf8", "utf-8")):
            return name
    pytest.skip("no English UTF-8 locale on this host")


@pytest.mark.parametrize("what", ["console", "setup-wifi", "broker"])
def test_the_default_is_accepted_in_a_utf8_locale(kit: Kit, what: str) -> None:
    """Kit 1 runs in en_GB.UTF-8, where a range of characters follows the dictionary and not
    ASCII. It refused BlackRelay2026 for every one of the three."""
    locale = _utf8_locale()
    default = "BlackRelay2026"
    result = kit.run(what, stdin=f"{default}\n{default}\n", LANG=locale, LC_ALL=locale)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("bad", ["caf\u00e9-password", "has a space in it"])
def test_the_rules_hold_in_a_utf8_locale(kit: Kit, bad: str) -> None:
    locale = _utf8_locale()
    result = kit.run("console", stdin=f"{bad}\n{bad}\n" * 3, LANG=locale, LC_ALL=locale)
    assert result.returncode != 0
    assert kit.changed_nothing()
