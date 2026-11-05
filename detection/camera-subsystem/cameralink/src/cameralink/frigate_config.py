"""Frigate's config and env file, rendered from the node's saved settings.

Pinned to Frigate 0.18.2 and the go2rtc 1.9.14 it ships. No secret reaches the YAML. Each
is a reference to the 0600 env file, in the syntax of whatever expands that field:

- ``proxy.auth_secret`` and camera input paths: ``{FRIGATE_*}``. Both are Frigate
  ``EnvString`` fields.
- ``go2rtc.streams``: ``{FRIGATE_*}``. Frigate's ``create_config.py`` runs ``str.format``
  over them.
- ``go2rtc.publish``: ``${FRIGATE_*}``. ``create_config.py`` does not touch this section,
  and go2rtc's own config loader expands ``${VAR}`` from its environment.

The restream is unverified on hardware. go2rtc's ``publish`` only takes rtmp, rtmps and
rtmpx destinations (``internal/streams/handlers.go``, ``GetConsumer``), and an rtsp one
only logs "unsupported scheme". So the stream goes to MediaMTX's RTMP port, on the same
path, and MediaMTX still serves it to readers over RTSP at ``<host>:8554/<node_id>``.

Rendering is deterministic, so that a changed output means a changed config. A changed
secret changes only the env file.
"""

import ipaddress
import os
import re
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any
from urllib.parse import quote

import yaml

CAMERA_PASSWORD_ENV = "FRIGATE_CAMERA_PASSWORD"
RELAY_PASSWORD_ENV = "FRIGATE_RELAY_PASSWORD"
PROXY_SECRET_ENV = "FRIGATE_PROXY_SECRET"

# Relative corners. Frigate compares each point as a string against "1.0" to spot legacy
# pixel coordinates, and "1" sorts below it.
FULL_FRAME = "0,0,1,0,1,1,0,1"

_RELAY_RTMP_PORT = 1935

# Frigate's REGEX_CAMERA_NAME.
_NODE_ID_RE = re.compile(r"[a-zA-Z0-9_-]+")
# Frigate's loader takes any key, but its zone editor needs two characters and refuses a
# period, and a zone name is an MQTT topic level. Outside this set, a zone could not have
# its polygon drawn or could break a topic. Names are never rewritten, because fusion
# matches them against the RF side's site vocabulary.
_ZONE_NAME_RE = re.compile(r"[a-zA-Z0-9_-]{2,}")
_ADDRESS_RE = re.compile(r"(\[[^\]]*\]|[^:\[\]]*)(?::([0-9]{1,5}))?")
_HOST_LABEL_RE = re.compile(r"[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")
_COORDINATE_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
# Characters that mean the same with or without URL encoding, and pass unchanged through
# env files, which differ on quotes, backslashes and whitespace.
# The proxy secret is compared byte for byte with a header. The camera password goes
# through Frigate's quote_plus on ffmpeg inputs but not to go2rtc, so no encoded value
# could serve both.
_LITERAL_SECRET_RE = re.compile(r"[a-zA-Z0-9._~-]+")


class FrigateConfigError(Exception):
    """The settings cannot give a config that Frigate accepts, or the env file cannot be
    written. The message never holds a secret."""


def render(
    node_id: str,
    covered_zones: Sequence[str],
    mediamtx_address: str,
    relay_user: str,
    existing: Mapping | None,
    proxy_secret: str,
) -> str:
    """Return Frigate's YAML.

    ``existing`` is the previously loaded config: zones still covered keep its coordinates,
    even if ``node_id`` has changed. ``proxy_secret`` is only checked here, because the YAML
    refers to it by name. An empty ``relay_user`` means an anonymous broker, and publishes
    with no login.
    """
    _check_node_id(node_id)
    _check_proxy_secret(proxy_secret)
    if not isinstance(relay_user, str):
        raise FrigateConfigError("relay_user must be text")
    encoded_user = _url_encoded(relay_user)
    if encoded_user is None:
        raise FrigateConfigError("relay_user cannot be encoded for a URL")
    zones = {
        name: {"coordinates": _coordinates(name, node_id, existing)}
        for name in _zone_names(covered_zones, node_id)
    }
    return _template().substitute(
        node_id=node_id,
        publish_url=_publish_url(node_id, mediamtx_address, encoded_user),
        zones=_indented_yaml(zones),
    )


def write_env(path: Path, camera_password: str, relay_password: str, proxy_secret: str) -> None:
    """Atomically replace the env file that Frigate's container starts with, mode 0600.

    The camera password is written as it is, and one that would need URL encoding is
    refused. The relay password only lands in a URL query, by plain substitution, so it is
    written percent-encoded. An empty ``relay_password`` means an anonymous broker.
    """
    if not isinstance(camera_password, str) or not _LITERAL_SECRET_RE.fullmatch(camera_password):
        raise FrigateConfigError(
            "the camera password must be non-empty, using letters, digits and . _ ~ - only"
        )
    if not isinstance(relay_password, str):
        raise FrigateConfigError("the relay password must be text")
    _check_proxy_secret(proxy_secret)
    encoded_relay_password = _url_encoded(relay_password)
    if encoded_relay_password is None:
        raise FrigateConfigError("the relay password cannot be encoded for a URL")
    body = (
        f"{CAMERA_PASSWORD_ENV}={camera_password}\n"
        f"{RELAY_PASSWORD_ENV}={encoded_relay_password}\n"
        f"{PROXY_SECRET_ENV}={proxy_secret}\n"
    )
    try:
        _replace_file(Path(path), body)
    except OSError as err:
        raise FrigateConfigError(f"cannot write {path}: {err.strerror}") from None


def _template() -> Template:
    source = resources.files("cameralink").joinpath("templates/frigate.yml.tmpl")
    return Template(source.read_text(encoding="utf-8"))


def _url_encoded(value: str) -> str | None:
    """``value`` percent-encoded, or None if it has no UTF-8 form. Callers raise outside
    any ``except``, as a UnicodeEncodeError's ``object`` holds the whole value."""
    try:
        return quote(value, safe="")
    except UnicodeEncodeError:
        return None


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


def _publish_url(node_id: str, mediamtx_address: Any, encoded_user: str) -> str:
    url = f"rtmp://{_relay_host(mediamtx_address)}:{_RELAY_RTMP_PORT}/{node_id}"
    if encoded_user:
        # MediaMTX takes RTMP logins from the query. go2rtc sends the URL as the tcUrl with
        # the query intact, as this path has one level and so an empty stream key.
        url += f"?user={encoded_user}&pass=${{{RELAY_PASSWORD_ENV}}}"
    return url


def _relay_host(mediamtx_address: Any) -> str:
    """The host of ``host[:port]``. The port is checked though only the host is used, as a
    bad one means a mistyped address, and go2rtc would retry it forever with no report."""
    match = _ADDRESS_RE.fullmatch(mediamtx_address) if isinstance(mediamtx_address, str) else None
    if match is None or not _is_host(match[1]) or not (match[2] is None or 0 < int(match[2]) < 65536):
        raise FrigateConfigError(
            f"mediamtx_address {mediamtx_address!r} must be a host, with an optional port"
        )
    return match[1]


def _is_host(host: str) -> bool:
    """A hostname, an IPv4 address, or a bracketed IPv6 address with no scope."""
    try:
        if host.startswith("["):
            return "%" not in host and bool(ipaddress.IPv6Address(host[1:-1]))
        labels = host.split(".")
        if len(host) > 253 or not all(_HOST_LABEL_RE.fullmatch(label) for label in labels):
            return False
        # A name whose last label is a number can only be a mistyped IPv4 address.
        return not labels[-1].isdigit() or bool(ipaddress.IPv4Address(host))
    except ValueError:
        return False


def _indented_yaml(zones: Mapping) -> str:
    text = yaml.safe_dump(dict(zones), sort_keys=True, default_flow_style=False, width=2**16)
    return "".join(f"      {line}\n" for line in text.splitlines()).rstrip("\n")


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
