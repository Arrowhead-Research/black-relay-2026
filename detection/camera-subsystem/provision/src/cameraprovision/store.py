"""The node's saved config on disk, written all-or-nothing across several files.

What lives where:

* ``bridge.yaml`` (0600, service user): everything the bridge reads, including the
  broker password. The source of truth for those fields.
* ``mediamtx.env`` (0644): the one value go2rtc needs, so it need not read the above.
* the NetworkManager keyfile (0600): the WiFi PSK, and the only copy of it.
* ``node.json`` (0600): provisioning-only fields the bridge YAML does not hold. A WiFi
  repair needs the hostname from here.
* ``tailscale.key`` (0600): exists only while a login is owed.
* the timesyncd drop-in (0644, root): the time server, read by ``systemd-timesyncd``.

A save stages every file beside its target, then commits. A failure before the commit
changes nothing, and a failure during it is rolled back from hard-linked backups. A
journal written at the start of the commit lets :meth:`NodeStore.recover` finish the
save after a power cut, so a node never boots into half of one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml

from cameraprovision.config import (
    BrokerConfig,
    FrigateConfig,
    NodeConfig,
    OutboxConfig,
    _write_private_atomic,
    effective_ntp_server,
    validate,
    validate_wifi,
    write_bridge_config,
)
from cameraprovision.netprofile import (
    UPLINK_PROFILE_ID,
    nm_wifi_profile,
    render_nm_keyfile,
)

__all__ = ["NodeStore", "Paths"]

logger = logging.getLogger(__name__)

# Stands in for the PSK when validating, because the PSK stays in the keyfile.
_PSK_PLACEHOLDER = "held-by-networkmanager"
_LOAD_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError)


@dataclass(frozen=True, kw_only=True)
class Paths:
    bridge_config: Path = Path("/etc/camerabridge/bridge.yaml")
    mediamtx_env: Path = Path("/etc/camerabridge/mediamtx.env")
    nm_connections_dir: Path = Path("/etc/NetworkManager/system-connections")
    state_dir: Path = Path("/var/lib/camera-provision")
    ntp_dropin: Path = Path("/etc/systemd/timesyncd.conf.d/black-relay.conf")

    @property
    def nm_keyfile(self) -> Path:
        return self.nm_connections_dir / f"{UPLINK_PROFILE_ID}.nmconnection"

    @property
    def node_file(self) -> Path:
        return self.state_dir / "node.json"

    @property
    def tailscale_key(self) -> Path:
        return self.state_dir / "tailscale.key"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def journal(self) -> Path:
        return self.state_dir / "apply.journal"

    def targets(self) -> tuple[Path, ...]:
        """Every file a save can change. The node file is last: it completes a config."""
        return (
            self.bridge_config,
            self.mediamtx_env,
            self.tailscale_key,
            self.ntp_dropin,
            self.nm_keyfile,
            self.node_file,
        )


# Hidden and ending in .tmp, so NetworkManager's keyfile plugin never loads one.
def _staged(target: Path) -> Path:
    return target.with_name(f".{target.name}.staged.tmp")


def _backup(target: Path) -> Path:
    return target.with_name(f".{target.name}.prev.tmp")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _restore(target: Path) -> None:
    """Put ``target``'s backup back, or remove it if it had none."""
    backup = _backup(target)
    if backup.exists():
        os.replace(backup, target)
        # rename(2) does nothing when both names are links to one file, which they are
        # if this target had not been replaced yet.
        backup.unlink(missing_ok=True)
    else:
        target.unlink(missing_ok=True)


class NodeStore:
    def __init__(
        self, paths: Paths, *, chown: Callable[[Path], None], interface: str = "wlan0"
    ) -> None:
        self._paths = paths
        self._chown = chown
        self._interface = interface

    # --- reading ------------------------------------------------------------------

    def _node(self) -> dict[str, Any]:
        """Return ``node.json``, raising unless it holds a hostname."""
        node = json.loads(self._paths.node_file.read_text(encoding="utf-8"))
        hostname = node.get("hostname") if isinstance(node, dict) else None
        if not hostname or not isinstance(hostname, str):
            raise ValueError("node file without a hostname")
        return node

    def hostname(self) -> str | None:
        """Return the saved hostname, whatever state the rest of the config is in."""
        try:
            return self._node()["hostname"]
        except _LOAD_ERRORS:
            return None

    def has_config(self) -> bool:
        """True if this node can attempt a join: a WiFi profile and a hostname.

        Deliberately not ``load() is not None``. A damaged bridge config must not send a
        configured node to the setup AP, since the WiFi it needs is intact. That is
        reported separately, using :meth:`load`.
        """
        return self._paths.nm_keyfile.exists() and self.hostname() is not None

    def pending_login_hostname(self) -> str | None:
        """Return the hostname to log in to Tailscale with, if a login is owed."""
        try:
            node = self._node()
        except _LOAD_ERRORS:
            return None
        if node.get("vpn_requested") and self._paths.tailscale_key.exists():
            return node["hostname"]
        return None

    def load(self) -> NodeConfig | None:
        """Return the full saved config, or ``None`` if it is missing, incomplete or invalid.

        ``wifi_psk`` is always empty, since only the keyfile holds it.
        ``vpn_requested`` means a Tailscale login is still owed: it is false once the key
        is gone, so a node that has logged in still has a valid config.
        """
        try:
            cfg = self._read()
        except _LOAD_ERRORS as exc:
            # The type only: YAML errors quote the text they could not parse.
            logger.debug("no usable saved config: %s", type(exc).__name__)
            return None
        return cfg if not validate(replace(cfg, wifi_psk=_PSK_PLACEHOLDER)) else None

    def _read(self) -> NodeConfig:
        paths = self._paths
        if not paths.nm_keyfile.exists():
            raise FileNotFoundError("no WiFi profile")
        bridge = yaml.safe_load(paths.bridge_config.read_text(encoding="utf-8"))
        node = json.loads(paths.node_file.read_text(encoding="utf-8"))
        key = (
            paths.tailscale_key.read_text(encoding="utf-8")
            if node["vpn_requested"] and paths.tailscale_key.exists()
            else ""
        )
        return NodeConfig(
            node_id=bridge["node_id"],
            broker=BrokerConfig(**bridge["broker"]),
            covered_zones=bridge["covered_zones"],
            cue_window_s=bridge["cue_window_s"],
            outbox=OutboxConfig(**bridge["outbox"]),
            status_interval_s=bridge["status_interval_s"],
            frigate=FrigateConfig(**bridge["frigate"]),
            mediamtx_address=bridge["mediamtx_address"],
            hostname=node["hostname"],
            wifi_ssid=node["wifi_ssid"],
            wifi_psk="",
            tailscale_auth_key=key,
            vpn_requested=bool(key),
            broker_anonymous=node["broker_anonymous"],
            ntp_server=node.get("ntp_server", ""),
        )

    # --- writing ------------------------------------------------------------------

    def save(self, cfg: NodeConfig) -> None:
        """Validate ``cfg`` and replace the whole saved config, or change nothing."""
        errors = validate(cfg)
        if errors:
            raise ValueError("invalid node config: " + "; ".join(errors))
        paths = self._paths
        ntp_server = effective_ntp_server(cfg)
        node = {
            "hostname": cfg.hostname,
            "wifi_ssid": cfg.wifi_ssid,
            "vpn_requested": cfg.vpn_requested,
            "broker_anonymous": cfg.broker_anonymous,
            "ntp_server": ntp_server,
        }

        def stage_bridge(staged: Path) -> None:
            write_bridge_config(cfg, staged)
            self._chown(staged)

        def stage_mediamtx(staged: Path) -> None:
            _write_private_atomic(staged, f"MEDIAMTX_ADDRESS={cfg.mediamtx_address}\n")
            staged.chmod(0o644)

        def stage_ntp(staged: Path) -> None:
            _write_private_atomic(staged, f"[Time]\nNTP={ntp_server}\n")
            staged.chmod(0o644)

        key = cfg.tailscale_auth_key if cfg.vpn_requested else None
        self._transact(
            [
                (paths.bridge_config, stage_bridge),
                (paths.mediamtx_env, stage_mediamtx),
                (paths.tailscale_key, None if key is None else self._text(key)),
                (paths.ntp_dropin, stage_ntp),
                (
                    paths.nm_keyfile,
                    self._text(self._keyfile(cfg.wifi_ssid, cfg.wifi_psk, cfg.hostname)),
                ),
                (paths.node_file, self._text(json.dumps(node))),
            ]
        )

    def save_wifi(self, ssid: str, psk: str) -> None:
        """Repair the WiFi alone: re-render the keyfile from these and the saved hostname.

        Reads ``node.json`` directly and never builds a ``NodeConfig``, so a repair works
        with the bridge config missing or invalid. That file, the time server and any
        pending Tailscale key are not touched.
        """
        errors = validate_wifi(ssid, psk)
        if errors:
            raise ValueError("invalid WiFi settings: " + "; ".join(errors))
        try:
            node = self._node()
        except _LOAD_ERRORS:
            raise ValueError("there is no saved hostname to repair with") from None
        node["wifi_ssid"] = ssid
        self._transact(
            [
                (self._paths.nm_keyfile, self._text(self._keyfile(ssid, psk, node["hostname"]))),
                (self._paths.node_file, self._text(json.dumps(node))),
            ]
        )

    def delete_tailscale_key(self) -> None:
        self._paths.tailscale_key.unlink(missing_ok=True)
        _fsync_dir(self._paths.tailscale_key.parent)

    def _keyfile(self, ssid: str, psk: str, hostname: str) -> str:
        return render_nm_keyfile(nm_wifi_profile(ssid, psk, hostname, interface=self._interface))

    @staticmethod
    def _text(text: str) -> Callable[[Path], None]:
        return lambda staged: _write_private_atomic(staged, text)

    def _transact(self, plan: Sequence[tuple[Path, Callable[[Path], None] | None]]) -> None:
        """Apply ``plan``: (target, how to stage its new content, or None to delete it)."""
        for target, _ in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
        try:
            for target, stage in plan:
                if stage is not None:
                    stage(_staged(target))
            self._commit(plan)
        except Exception:
            for target, _ in plan:
                _staged(target).unlink(missing_ok=True)
            raise

    def _commit(self, plan: Sequence[tuple[Path, Callable[[Path], None] | None]]) -> None:
        targets = [target for target, _ in plan]
        for target in targets:
            _backup(target).unlink(missing_ok=True)
            if target.exists():
                os.link(target, _backup(target))
        for directory in {target.parent for target in targets}:
            _fsync_dir(directory)
        entries = [{"target": str(t), "write": stage is not None} for t, stage in plan]
        _write_private_atomic(self._paths.journal, json.dumps(entries))  # the commit point
        try:
            self._roll_forward(entries)
        except Exception:
            # Journal first: a power cut from here on must finish the rollback instead.
            self._drop_journal()
            for target in reversed(targets):
                _restore(target)
            raise
        try:
            self._finish_commit(entries)
        except OSError as exc:
            # The new config is in force. The journal, if still there, settles it at startup.
            logger.error("tidying up after a save failed: %s", type(exc).__name__)

    @staticmethod
    def _roll_forward(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            target = Path(entry["target"])
            if not entry["write"]:
                target.unlink(missing_ok=True)
            elif _staged(target).exists():
                os.replace(_staged(target), target)

    def _drop_journal(self) -> None:
        self._paths.journal.unlink(missing_ok=True)
        _fsync_dir(self._paths.state_dir)

    def _finish_commit(self, entries: list[dict[str, Any]]) -> None:
        """Make the renames durable, then drop the backups and only then the journal.

        Backups with no journal mean an unfinished rollback, so if the journal's removal
        reached the disk before a backup's, a power cut would undo a finished save. Hence
        the second round of syncs.
        """
        targets = [Path(entry["target"]) for entry in entries]
        directories = {target.parent for target in targets}
        for directory in directories:
            _fsync_dir(directory)
        for target in targets:
            _backup(target).unlink(missing_ok=True)
        for directory in directories:
            _fsync_dir(directory)
        self._drop_journal()

    def _read_journal(self) -> list[dict[str, Any]] | None:
        """Return the journal's entries, or ``None`` if it is not one this store wrote."""
        try:
            entries = json.loads(self._paths.journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        known = {str(target) for target in self._paths.targets()}
        valid = (
            isinstance(entries, list)
            and entries
            and all(
                isinstance(entry, dict)
                and isinstance(entry.get("target"), str)
                and entry.get("target") in known
                and isinstance(entry.get("write"), bool)
                for entry in entries
            )
        )
        return entries if valid else None

    def _settle_interrupted_save(self) -> None:
        paths = self._paths
        entries = self._read_journal() if paths.journal.exists() else None
        if entries is not None:
            self._roll_forward(entries)
            self._finish_commit(entries)
            return
        if paths.journal.exists():
            logger.error("ignoring an unreadable save journal")
            self._drop_journal()
        for target in paths.targets():
            if _backup(target).exists():
                _restore(target)

    def _remove_stray_bridge_config(self) -> None:
        """Delete a bridge config left with no node file, so a blank node's bridge stays off.

        A power cut during the rollback of a first save can leave one. Skipped while a
        journal remains, because a later start needs the file to finish that commit.
        """
        paths = self._paths
        if paths.node_file.exists() or paths.journal.exists() or not paths.bridge_config.exists():
            return
        paths.bridge_config.unlink()
        logger.warning("removed a bridge config found on a blank node, so the bridge stays off")

    def _sweep(self) -> None:
        paths = self._paths
        if paths.journal.exists():
            return  # a commit is still unfinished, and needs its staged files next time
        for target in (*paths.targets(), paths.state_file, paths.journal):
            for pattern in (f".{target.name}.*.tmp", f"..{target.name}.*.tmp"):
                for stale in target.parent.glob(pattern):
                    stale.unlink(missing_ok=True)

    def recover(self) -> None:
        """Run at startup: settle an interrupted save and remove stale working files.

        Never raises for a disk problem. The node has to keep joining or serving the AP,
        and exiting would only repeat this on the same disk. The sweep is skipped if settling
        failed, because a later start needs the backups to finish that rollback.
        """
        paths = self._paths
        self._recovery_step(lambda: paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True))
        settled = self._recovery_step(self._settle_interrupted_save)
        self._recovery_step(self._remove_stray_bridge_config)
        if settled:
            self._recovery_step(self._sweep)

    @staticmethod
    def _recovery_step(step: Callable[[], None]) -> bool:
        try:
            step()
        except OSError as exc:
            logger.error("recovering the saved config failed: %s", type(exc).__name__)
            return False
        return True
