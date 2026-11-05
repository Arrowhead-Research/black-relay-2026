"""Frigate's config and env file, rendered from the node's saved settings.

Pinned to Frigate 0.18.2 and the go2rtc 1.9.14 it ships. No secret reaches the YAML. Each
is a reference to the 0600 env file, in the syntax of whatever expands that field:

- ``proxy.auth_secret`` and camera input paths: ``{FRIGATE_*}``. Both are Frigate
  ``EnvString`` fields.
- ``go2rtc.streams`` and ``go2rtc.rtsp.password``: ``{FRIGATE_*}``. Frigate's
  ``create_config.py`` expands them when it writes go2rtc's own config.

Live view is unverified on hardware. go2rtc serves the camera's main stream on RTSP port
8554 as ``<node_id>``, and the central node pulls it while someone watches. go2rtc dials
the camera for its first consumer and drops it after the last, so nothing in this config
may hold a consumer open: no ``publish``, no ``preload``. go2rtc skips the login check for
loopback clients and when no username is set, so with no relay user it listens on loopback.
Its API serves the same stream with no login, so that stays on loopback in every case, behind
the firewall as well. Frigate's nginx reaches it there.

Rendering is deterministic, so that a changed output means a changed config. A changed
secret changes only the env file.
"""

import hashlib
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

import yaml

CAMERA_PASSWORD_ENV = "FRIGATE_CAMERA_PASSWORD"
RELAY_PASSWORD_ENV = "FRIGATE_RELAY_PASSWORD"
PROXY_SECRET_ENV = "FRIGATE_PROXY_SECRET"

# Relative corners. Frigate compares each point as a string against "1.0" to spot legacy
# pixel coordinates, and "1" sorts below it.
FULL_FRAME = "0,0,1,0,1,1,0,1"

# Frigate's REGEX_CAMERA_NAME.
_NODE_ID_RE = re.compile(r"[a-zA-Z0-9_-]+")
# Frigate's loader takes any key, but its zone editor needs two characters and refuses a
# period, and a zone name is an MQTT topic level. Outside this set, a zone could not have
# its polygon drawn or could break a topic. Names are never rewritten, because fusion
# matches them against the RF side's site vocabulary.
_ZONE_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{2,}")
_COORDINATE_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
# Characters that mean the same with or without URL encoding, and pass unchanged through
# env files, which differ on quotes, backslashes and whitespace.
# The proxy secret is compared byte for byte with a header. The camera password goes
# through Frigate's quote_plus on ffmpeg inputs but not to go2rtc, so no encoded value
# could serve both.
_LITERAL_SECRET_RE = re.compile(r"[a-zA-Z0-9._~-]+")
# go2rtc compares the relay login as it is, so neither can be encoded. A user goes through
# Frigate's brace substitution, and ":" would split Basic auth. The password may be typed by
# an operator, so it allows what env files carry literally. "$" could become a go2rtc ${VAR}.
_RELAY_USER_RE = re.compile(r"[a-zA-Z0-9._~@+-]+")
_RELAY_PASSWORD_RE = re.compile(r"[!#%&(-\[\]-_a-~]*")


class FrigateConfigError(Exception):
    """The settings cannot give a config that Frigate accepts, or the env file cannot be
    written. The message never holds a secret."""


def proxy_secret(broker_password: str) -> str:
    """The secret Frigate shares with the central API. Both ends hold the broker password, and
    the central API must derive this same value, so changing it changes the secret on both."""
    return hashlib.sha256(b"black-relay-frigate-proxy:" + broker_password.encode()).hexdigest()


def render(
    node_id: str,
    covered_zones: Sequence[str],
    relay_user: str,
    existing: Mapping | None,
    proxy_secret: str,
) -> str:
    """Return Frigate's YAML.

    ``existing`` is the previously loaded config: zones still covered keep its coordinates,
    even if ``node_id`` has changed. ``proxy_secret`` is only checked here, because the YAML
    refers to it by name. An empty ``relay_user`` means an anonymous broker: live view then
    stays on loopback, because go2rtc would otherwise serve it with no login.
    """
    _check_node_id(node_id)
    _check_proxy_secret(proxy_secret)
    rtsp = _rtsp_section(relay_user)
    zones = {
        name: {"coordinates": _coordinates(name, node_id, existing)}
        for name in _zone_names(covered_zones, node_id)
    }
    return _template().substitute(
        node_id=node_id,
        rtsp=_indented_yaml(rtsp, 4),
        zones=_indented_yaml(zones, 6),
    )


def write_env(path: Path, camera_password: str, relay_password: str, proxy_secret: str) -> None:
    """Atomically replace the env file that Frigate's container starts with, mode 0600.

    Every value is written as it is, so a value an env file cannot carry literally is
    refused. An empty ``relay_password`` means an anonymous broker.
    """
    if not isinstance(camera_password, str) or not _LITERAL_SECRET_RE.fullmatch(camera_password):
        raise FrigateConfigError(
            "the camera password must be non-empty, using letters, digits and . _ ~ - only"
        )
    if not isinstance(relay_password, str) or not _RELAY_PASSWORD_RE.fullmatch(relay_password):
        raise FrigateConfigError(
            "the relay password must be printable ASCII, with no space, quote, backslash or $"
        )
    _check_proxy_secret(proxy_secret)
    body = (
        f"{CAMERA_PASSWORD_ENV}={camera_password}\n"
        f"{RELAY_PASSWORD_ENV}={relay_password}\n"
        f"{PROXY_SECRET_ENV}={proxy_secret}\n"
    )
    try:
        _replace_file(Path(path), body)
    except OSError as err:
        raise FrigateConfigError(f"cannot write {path}: {err.strerror}") from None


def _template() -> Template:
    source = resources.files("cameralink").joinpath("templates/frigate.yml.tmpl")
    return Template(source.read_text(encoding="utf-8"))


def _check_node_id(node_id: Any) -> None:
    if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
        raise FrigateConfigError(
            f"node_id {node_id!r} is not a valid Frigate camera name: "
            "use letters, digits, hyphens and underscores"
        )


def _check_proxy_secret(secret: Any) -> None:
    if not isinstance(secret, str) or not _LITERAL_SECRET_RE.fullmatch(secret):
        raise FrigateConfigError(
            "the proxy secret must be non-empty, using letters, digits and . _ ~ - only"
        )


def _zone_names(covered_zones: Any, node_id: str) -> list[str]:
    if isinstance(covered_zones, str):
        raise FrigateConfigError("covered_zones must be a list of names, not one string")
    try:
        names = list(covered_zones)
    except TypeError:
        names = None
    if names is None:
        raise FrigateConfigError("covered_zones must be a list of names")
    for name in names:
        if not isinstance(name, str) or not _ZONE_NAME_RE.fullmatch(name):
            raise FrigateConfigError(
                f"zone name {name!r} is not valid for Frigate: use at least two letters, "
                "digits, hyphens or underscores"
            )
        if name == node_id:
            raise FrigateConfigError(f"zone name {name!r} cannot equal the camera name {node_id!r}")
    return sorted(set(names))


def _coordinates(name: str, node_id: str, existing: Mapping | None) -> str | list[str]:
    """The zone's saved coordinates, or the full frame if no saved camera has this zone. A
    saved zone that Frigate could not load raises, because replacing it would lose a
    drawing."""
    for camera_zones in _saved_zones(node_id, existing):
        if name in camera_zones:
            zone = camera_zones[name]
            coordinates = zone.get("coordinates") if isinstance(zone, Mapping) else None
            return _checked_coordinates(name, coordinates)
    return FULL_FRAME


def _saved_zones(node_id: str, existing: Mapping | None) -> Iterator[Mapping]:
    """Each saved camera's zones: this node's first, then any other, as a changed
    ``node_id`` leaves the drawing under the old name."""
    cameras = existing.get("cameras") if isinstance(existing, Mapping) else None
    if not isinstance(cameras, Mapping):
        return
    for camera_name in sorted(cameras, key=lambda camera: (camera != node_id, str(camera))):
        camera = cameras[camera_name]
        zones = camera.get("zones") if isinstance(camera, Mapping) else None
        if isinstance(zones, Mapping):
            yield zones


def _checked_coordinates(name: str, coordinates: Any) -> str | list[str]:
    if isinstance(coordinates, str):
        result: str | list[str] = str(coordinates)
        joined = coordinates
    elif isinstance(coordinates, list) and all(isinstance(point, str) for point in coordinates):
        result = [str(point) for point in coordinates]
        joined = ",".join(coordinates)
    else:
        raise FrigateConfigError(f"zone {name!r} has saved coordinates in an unknown form")
    values = joined.split(",")
    if len(values) < 6 or len(values) % 2 or not all(_COORDINATE_RE.fullmatch(v) for v in values):
        raise FrigateConfigError(f"zone {name!r} has saved coordinates that are not a polygon")
    return result


def _rtsp_section(relay_user: Any) -> dict[str, str]:
    if relay_user == "":
        return {"listen": "127.0.0.1:8554"}
    if not isinstance(relay_user, str) or not _RELAY_USER_RE.fullmatch(relay_user):
        raise FrigateConfigError(
            f"relay_user {relay_user!r} must use only letters, digits and . _ ~ @ + -"
        )
    return {"username": relay_user, "password": f"{{{RELAY_PASSWORD_ENV}}}"}


def _indented_yaml(section: Mapping, indent: int) -> str:
    # Every scalar is quoted, because PyYAML writes YAML 1.1 and Frigate reads 1.2, where
    # plain words such as 1e3 and 0o17 are numbers.
    text = yaml.safe_dump(
        dict(section), sort_keys=True, default_flow_style=False, default_style='"', width=2**16
    )
    return "".join(" " * indent + f"{line}\n" for line in text.splitlines()).rstrip("\n")


def _replace_file(path: Path, body: str) -> None:
    # mkstemp creates 0600, so the secrets are never readable in the temporary name.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
