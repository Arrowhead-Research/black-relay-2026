"""`br2 pair`: the one-time IQ Remote pairing, with every panel write behind an allow-list.

What pairing is (as implemented by the open qolsys_controller library; not verified on a real IQ5 by this project):
  1. We create an identity: a random MAC `f2:16:3e:xx:xx:xx`, an RSA key, a self-signed certificate and a CSR, in
     `<pki_dir>/<12 hex>/` as `<id>.key|.cer|.csr`.
  2. We run a TLS server on the LAN and announce it over mDNS (`NsdPairService._http._tcp.local.`). The operator
     taps Pair on the panel; the panel connects to us and sends its MAC.
  3. We answer with exactly three frames: our random MAC (length-prefixed), the CSR (PEM), the literal `sent`.
  4. The panel sends back our CSR signed by the panel CA (`<id>.secure`) and the panel CA certificate
     (`<id>.qolsys`). That is the whole pairing exchange; both files are validated and stored 0600.
  5. We then open the normal IQ Remote MQTT session (TLS, client certificate) through the adapter's GuardedPanel,
     to prove the new identity works.

Write fence: nothing in this module sends to the panel except through
  * PairGuard / GuardedPairWriter: the pairing socket accepts exactly the three frames of step 3, in order, with
    the exact bytes (our MAC frame, our CSR, `sent`); anything else raises OneWayViolation before bytes leave;
  * PairGuard.check_mdns: the only mDNS record we register is the pairing service above, with our address/port;
  * iq_remote.GuardedPanel (step 5): the adapter's exact-key allow-list (connect_v204, pingevent,
    pair_status_request, syncdatabase). No new MQTT message type was needed for pairing.
No key material is printed or logged: output names files and sizes, never contents.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import random
import re
import ssl
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import field as FLD
from .adapters import iq_remote as IQ
from .adapters.base import OneWayViolation

log = logging.getLogger("br2.pairing")

MDNS_TYPE = "_http._tcp.local."
MDNS_NAME = "NsdPairService._http._tcp.local."
CSR_BEGIN = b"-----BEGIN CERTIFICATE REQUEST-----"
CERT_BEGIN, CERT_END = b"-----BEGIN CERTIFICATE-----\n", b"-----END CERTIFICATE-----\n"
RANDOM_MAC_RE = re.compile(r"^f2:16:3e:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")
PEER_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
DEFAULT_TIMEOUT_S = 300
MAX_EXCHANGES = 5                    # connection attempts by the panel we are willing to answer


class PairingError(FLD.FieldError):
    """Pairing failed in a way the operator can act on."""


# ------------------------------------------------------------------ the write fence
class PairGuard:
    """Allow-list for everything the pairing step may emit toward the panel (outside the MQTT session)."""

    def __init__(self, random_mac: str, csr_pem: bytes, plugin_ip: str | None = None, port: int | None = None,
                 on_violation=None):
        if not RANDOM_MAC_RE.match(random_mac):
            raise ValueError("random MAC must look like f2:16:3e:xx:xx:xx")
        if not csr_pem.startswith(CSR_BEGIN):
            raise ValueError("CSR must be a PEM certificate request")
        self.random_mac, self.csr_pem = random_mac, csr_pem
        self.plugin_ip, self.port = plugin_ip, port
        self._on_violation = on_violation or (lambda: None)
        self._order = ("mac", "csr", "sent")
        self._i = 0
        self.exchanges = 0
        self.sent: list[str] = []                      # audit trail of frame kinds that went out

    def _violation(self, why: str) -> None:
        self._on_violation()
        log.error("ONE-WAY GUARD blocked panel write: %s", why)
        raise OneWayViolation(why)

    def new_exchange(self) -> None:
        """The panel connected (again): the three frames may be sent once more, in the same order."""
        self.exchanges += 1
        if self.exchanges > MAX_EXCHANGES:
            self._violation("too many pairing exchanges")
        self._i = 0

    def check_frame(self, data: bytes) -> str:
        want = self._order[self._i] if self._i < len(self._order) else None
        if want is None:
            self._violation("pairing socket: nothing more may be sent after `sent`")
        if not isinstance(data, (bytes, bytearray)):
            self._violation("pairing socket: frame is not bytes")
        data = bytes(data)
        expected = {"mac": len(self.random_mac).to_bytes(2, "big") + self.random_mac.encode(),
                    "csr": self.csr_pem, "sent": b"sent"}[want]
        if data != expected:
            self._violation(f"pairing socket: frame is not the expected `{want}` frame")
        self._i += 1
        self.sent.append(want)
        return want

    def check_mdns(self, svc_type: str, name: str, address: str, port: int) -> None:
        if svc_type != MDNS_TYPE or name != MDNS_NAME:
            self._violation(f"mDNS: only {MDNS_NAME} may be announced, not {name!r}")
        if (self.plugin_ip is not None and address != self.plugin_ip) or (self.port is not None and port != self.port):
            self._violation("mDNS: the announcement must name our own address and pairing port")


class GuardedPairWriter:
    """Sole holder of the pairing socket's StreamWriter."""

    def __init__(self, writer: Any, guard: PairGuard):
        self._w = writer
        self._guard = guard

    async def send(self, data: bytes) -> None:
        self._guard.check_frame(data)
        self._w.write(data)
        await self._w.drain()


# ------------------------------------------------------------------ PKI files
@dataclass
class Identity:
    id12: str
    folder: Path

    @property
    def mac(self) -> str:
        return IQ.fmt_mac(self.id12)

    def path(self, ext: str) -> Path:
        return self.folder / f"{self.id12}{ext}"


def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as e:  # pragma: no cover
        raise PairingError("pairing needs the `cryptography` package: pip install -e '.[pair]'") from e
    return x509, hashes, serialization, rsa, NameOID


def write_private(path: Path, data: bytes) -> None:
    """Write `data` to `path` with mode 0600 from the first byte (no window with a wider mode), atomically."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ensure_dir_0700(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def list_identities(pki_dir: Path) -> list[Path]:
    if not pki_dir.is_dir():
        return []
    return sorted(p for p in pki_dir.iterdir() if p.is_dir() and ID_RE.fullmatch(p.name.lower()))


def is_complete(folder: Path) -> bool:
    return all((folder / f"{folder.name}{e}").is_file() for e in (".key", ".secure", ".qolsys"))


def has_local_identity(folder: Path) -> bool:
    return all((folder / f"{folder.name}{e}").is_file() for e in (".key", ".cer", ".csr"))


def generate_random_mac() -> str:
    b = [0xF2, 0x16, 0x3E, random.randint(0x00, 0x7F), random.randint(0x00, 0xFF), random.randint(0x00, 0xFF)]
    return ":".join("%02x" % x for x in b)


def create_identity(pki_dir: Path, key_size: int = 2048) -> Identity:
    """Generate key + self-signed certificate + CSR (what the library's QolsysPKI.create does)."""
    import datetime
    x509, hashes, serialization, rsa, NameOID = _crypto()
    ensure_dir_0700(pki_dir)
    for _ in range(20):
        mac = generate_random_mac()
        id12 = mac.replace(":", "")
        if not (pki_dir / id12).exists():
            break
    else:  # pragma: no cover
        raise PairingError("could not pick an unused identity")
    folder = pki_dir / id12
    ensure_dir_0700(folder)
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                      x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "SanJose"),
                      x509.NameAttribute(NameOID.LOCALITY_NAME, ""),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Qolsys Inc."),
                      x509.NameAttribute(NameOID.COMMON_NAME, "www.qolsys.com ")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    csr = (x509.CertificateSigningRequestBuilder().subject_name(name)
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .sign(key, hashes.SHA256()))
    ident = Identity(id12, folder)
    write_private(ident.path(".key"), key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    write_private(ident.path(".cer"), cert.public_bytes(serialization.Encoding.PEM))
    write_private(ident.path(".csr"), csr.public_bytes(serialization.Encoding.PEM))
    return ident


def preflight(pki_dir: Path) -> Identity | None:
    """None = start a fresh identity. An Identity = an unfinished pairing to resume. Refuses when already paired."""
    folders = list_identities(pki_dir)
    if len(folders) > 1:
        raise PairingError(f"{len(folders)} identity folders in {pki_dir}; keep only the one you want and rerun")
    if not folders:
        return None
    f = folders[0]
    if is_complete(f):
        raise PairingError(f"already paired: {f} holds a complete pairing. Use `br2 snapshot` to check it. Do not "
                           "delete or move it: the panel issues one client certificate per virtual IQ Remote, so "
                           "re-pairing may not be possible. (To pair again deliberately, move the folder away first.)")
    if has_local_identity(f):
        return Identity(f.name.lower(), f)
    raise PairingError(f"{f} is incomplete and cannot be resumed; move it away and rerun")


def _spki(pub: Any) -> bytes:
    _, _, serialization, _, _ = _crypto()
    return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def validate_received(ident: Identity, secure_pem: bytes, qolsys_pem: bytes) -> None:
    """The signed certificate must be for OUR key; the CA certificate must parse. Nothing is stored otherwise."""
    x509, _, serialization, _, _ = _crypto()
    try:
        secure = x509.load_pem_x509_certificate(secure_pem)
        x509.load_pem_x509_certificate(qolsys_pem)
        key = serialization.load_pem_private_key(ident.path(".key").read_bytes(), password=None)
    except Exception as e:  # noqa: BLE001
        raise PairingError(f"the panel sent material that does not parse: {type(e).__name__}") from e
    if _spki(secure.public_key()) != _spki(key.public_key()):
        raise PairingError("the signed certificate from the panel is not for our key; pairing refused")


def chown_tree(folder: Path, owner: str) -> None:
    """Hand the identity folder, the pki dir, its files and the pin file to `owner` (`uid:gid`, or `dir` = whoever owns
    the pki dir): what a root-run `br2 pair` writes must belong to the uid the service runs as, or the service cannot
    read it (0600/0700, no capabilities)."""
    from .pin import PIN_FILE, parse_owner
    own = parse_owner(owner, folder.parent)
    if own is None:
        return
    extra = [folder.parent / PIN_FILE] if (folder.parent / PIN_FILE).exists() else []
    for p in (folder, folder.parent, *folder.iterdir(), *extra):
        os.chown(p, own[0], own[1])


# ------------------------------------------------------------------ the pairing server
class PairingServer:
    """TLS server the panel connects to after the operator taps Pair. One completed exchange ends it."""

    def __init__(self, ident: Identity, guard: PairGuard, plugin_ip: str, port: int, panel_ip: str | None,
                 read_timeout_s: float = 30.0):
        self.ident, self.guard, self.plugin_ip, self.port = ident, guard, plugin_ip, port
        self.panel_ip = panel_ip
        self.read_timeout_s = read_timeout_s
        self.done = asyncio.Event()
        self.error: Exception | None = None
        self.peer_mac: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._active = False

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=str(self.ident.path(".cer")), keyfile=str(self.ident.path(".key")))
        return ctx

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.plugin_ip, self.port, ssl=self._ssl_ctx())

    @property
    def bound_port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _read(self, coro):
        return await asyncio.wait_for(coro, self.read_timeout_s)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        if self._active or self.done.is_set():
            writer.close()
            return
        if self.panel_ip and peer[0] != self.panel_ip:
            print(f"Ignored a pairing connection from {peer[0]}: not the configured panel ({self.panel_ip}).",
                  file=sys.stderr, flush=True)
            writer.close()
            return
        self._active = True
        out = GuardedPairWriter(writer, self.guard)
        try:
            self.guard.new_exchange()
            length = int.from_bytes(await self._read(reader.readexactly(2)), "big")
            if length != 17:
                raise PairingError(f"unexpected pairing handshake from the panel (first frame length {length}, wanted 17)")
            mac = (await self._read(reader.readexactly(length))).decode("ascii", "replace")
            if not PEER_MAC_RE.match(mac):
                raise PairingError("the panel's identification frame is not a MAC address")
            self.peer_mac = mac.lower()
            await out.send(len(self.guard.random_mac).to_bytes(2, "big") + self.guard.random_mac.encode())
            await out.send(self.guard.csr_pem)
            await out.send(b"sent")
            await self._read(reader.readuntil(CERT_BEGIN))
            secure = CERT_BEGIN + await self._read(reader.readuntil(CERT_END))
            await self._read(reader.readuntil(CERT_BEGIN))
            qolsys = CERT_BEGIN + await self._read(reader.readuntil(CERT_END))
            validate_received(self.ident, secure, qolsys)
            write_private(self.ident.path(".secure"), secure)
            write_private(self.ident.path(".qolsys"), qolsys)
            self.done.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.error = e if isinstance(e, (PairingError, OneWayViolation)) else PairingError(
                f"the pairing exchange failed: {type(e).__name__}")
            self.done.set()
        finally:
            self._active = False
            writer.close()


# ------------------------------------------------------------------ mDNS (zeroconf, optional dependency)
class Mdns:
    def __init__(self, guard: PairGuard, ip: str, port: int):
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError as e:  # pragma: no cover
            raise PairingError("pairing needs the `zeroconf` package for mDNS: pip install -e '.[pair]'") from e
        import socket
        guard.check_mdns(MDNS_TYPE, MDNS_NAME, ip, port)
        self._info = ServiceInfo(MDNS_TYPE, MDNS_NAME, addresses=[socket.inet_aton(ip)], port=port)
        self._azc = AsyncZeroconf()

    async def start(self) -> None:
        await self._azc.async_register_service(self._info)

    async def stop(self) -> None:
        try:
            await self._azc.async_unregister_service(self._info)
        finally:
            await self._azc.async_close()


# ------------------------------------------------------------------ the command
INSTRUCTIONS = """
Pairing this host with the panel at {panel_ip} as a virtual IQ Remote.

On the PANEL (you need the installer/dealer code, never a user code):
  (swipe down) Settings > Advanced Settings > Installation > Devices > Wi-Fi Devices > IQ Remote Devices > Pair
This host must be on the panel's access point or the same Wi-Fi. If this times out, try the other order.

This host is now listening for the panel on {plugin_ip}:{port} (announced over mDNS) and will wait up to
{timeout} s. Tap Pair on the panel now. Allow inbound TCP {port} from the panel and UDP 5353 (mDNS) on this host.
"""


def pick_plugin_ip(panel_ip: str, given: str | None) -> str:
    ip = given or IQ.detect_ip(panel_ip)
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as e:
        raise PairingError(f"plugin IP {ip!r} is not an IP address") from e
    if addr.is_loopback and not ipaddress.ip_address(panel_ip).is_loopback:
        raise PairingError("could not find this host's LAN address toward the panel; pass --plugin-ip")
    return ip


async def run_pair(settings: dict, timeout_s: float = DEFAULT_TIMEOUT_S, pairing_port: int = 0,
                   owner: str | None = None, advertise: bool = True, verify: bool = True,
                   key_size: int = 2048, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    pki_dir = Path(settings["pki_dir"])
    plugin_ip = pick_plugin_ip(str(settings["panel_ip"]), settings.get("plugin_ip"))
    ident = preflight(pki_dir)
    if ident is None:
        ident = create_identity(pki_dir, key_size)
        print(f"Created a new identity {ident.mac} in {ident.folder} (mode 0700; files 0600).", file=err)
    else:
        print(f"Resuming an unfinished pairing with identity {ident.mac}.", file=err)
    guard = PairGuard(ident.mac, ident.path(".csr").read_bytes(), plugin_ip)
    server = None
    mdns = None
    port = pairing_port
    try:
        for attempt in range(20):
            port = pairing_port or random.randint(50000, 55000)
            guard.port = port
            server = PairingServer(ident, guard, plugin_ip, port, str(settings["panel_ip"]))
            try:
                await server.start()
                break
            except OSError:
                if pairing_port:
                    raise PairingError(f"cannot listen on {plugin_ip}:{port}")
        else:  # pragma: no cover
            raise PairingError("no free pairing port")
        if advertise:
            mdns = Mdns(guard, plugin_ip, port)
            await mdns.start()
        print(INSTRUCTIONS.format(panel_ip=settings["panel_ip"], plugin_ip=plugin_ip, port=port, timeout=int(timeout_s)),
              file=err, flush=True)
        try:
            await asyncio.wait_for(server.done.wait(), timeout_s)
        except asyncio.TimeoutError:
            raise PairingError(f"no panel completed the pairing within {int(timeout_s)} s. Check that Pair was tapped "
                               "on the panel, that the panel and this host share a subnet, and that inbound TCP "
                               f"{port} and mDNS (UDP 5353) are allowed") from None
        if server.error:
            raise server.error
    finally:
        if mdns:
            await mdns.stop()
        if server:
            await server.stop()
    for p in ident.folder.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)
    os.chmod(ident.folder, 0o700)
    if owner:
        chown_tree(ident.folder, owner)
    print(f"Paired as {ident.mac} (panel reported {server.peer_mac}). Identity stored in {ident.folder}: "
          f"{', '.join(sorted(p.name for p in ident.folder.iterdir() if p.is_file()))} (mode 0600, folder 0700).",
          file=err)
    print("Back this folder up and keep it out of git: the panel issues one client certificate per virtual IQ Remote.",
          file=err)
    if not verify:
        return 0
    print("Verifying the new identity with a read-only session ...", file=err, flush=True)
    try:
        async with FLD.PanelSession(settings) as sess:
            print(FLD.tls_line(sess.tls), file=err)
            print(f"Session OK: {len(sess.zones)} zone(s), {len(sess.parts)} partition(s).", file=err)
            pin = getattr(sess, "pin", None)
            if pin is not None and pin.expected():
                print(f"Panel certificate pinned (SPKI sha256 {pin.expected()}) in {pin.path if pin.stored() else 'memory only'}.",
                      file=err)
        if owner:
            chown_tree(ident.folder, owner)                # the pin file was written after the first chown
    except FLD.FieldError as e:
        print(f"Pairing material was stored, but the verification session failed: {e}\n"
              "Wait a few seconds for the panel to finish registering the remote, then run `br2 snapshot`.", file=err)
        return 3
    print(f"pki_dir={pki_dir}", file=out)
    return 0
