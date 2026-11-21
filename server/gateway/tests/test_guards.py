"""The guards the gateway rests on, each with a test that goes red when it is taken off: the admin password on
every admin route, the mission package password on an update, the client certificate, and the two rules of a
clear (it ends only a tripped node, and only the trip it names)."""

import ssl
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select

from pytak_gateway.db import DiscoveredRow, NodeRow, SiteRow
from pytak_gateway.gateway_server import GatewayServer
from pytak_gateway.models import ClearRequest, DetectionEvent, Status
from pytak_gateway.registry import Registry
from pytak_gateway.state import GatewayState
from pytak_gateway.tls import TLSConfig, TLSReloader

from .conftest import ADMIN_HASH, ADMIN_PASSWORD
from .test_api_mission_package import FakeMissionPackages

ADMIN = ("admin", ADMIN_PASSWORD)
WRONG = ("admin", "not-the-admin-password")
MISSION = {"X-Mission-Package-Password": "mission-password-1"}
SITE = {
    "site_id": "sh1",
    "lat": 38.8895,
    "lon": -77.0353,
    "mission_package_name": "sh1-mission",
    "mission_package_password": "mission-password-1",
}
PACKAGE = {"site_id": "sh1", "lat": 38.8895, "lon": -77.0353}
NODE = {"lat": 38.8895, "lon": -77.0353}


@pytest.fixture
def server():
    registry = Registry(":memory:")
    server = GatewayServer(registry, None, ADMIN_HASH, resend=False, mission_packages=FakeMissionPackages())
    with TestClient(server.app) as client:
        server.client = client
        assert client.post("/sites", auth=ADMIN, json=SITE).status_code == 201
        with registry.begin() as s:
            row = s.scalars(select(SiteRow).where(SiteRow.site_id == "sh1")).one()
            s.add(NodeRow(site_id="sh1", site=row, subsystem="camera", node_id="cam-01", lat=38.8895, lon=-77.0353))
            now = datetime.now(timezone.utc)
            s.add(DiscoveredRow(site_id="sh9", subsystem="camera", node_id="cam-09", first_seen=now, last_seen=now))
        yield server
    registry.close()


# --- the admin password ---------------------------------------------------------------------------------------

ADMIN_ROUTES = [
    ("POST", "/sites", {"json": {**SITE, "site_id": "sh2", "mission_package_name": "sh2-mission"}}),
    ("GET", "/unregistered", {}),
    ("PUT", "/sites/sh1/nodes/camera/cam-02", {"json": NODE}),
    ("DELETE", "/sites/sh1", {"headers": MISSION}),
]


@pytest.mark.parametrize("method,path,how", ADMIN_ROUTES, ids=[f"{m} {p}" for m, p, _ in ADMIN_ROUTES])
def test_an_admin_route_without_the_admin_password_is_401_and_changes_nothing(server, method, path, how):
    before = (server.registry.sites(), len(server.gateway.state.registry.list_unregistered()))
    for auth in (None, WRONG):
        r = server.client.request(method, path, auth=auth, **how)
        assert r.status_code == 401, f"{method} {path} with {auth}"
        assert r.headers["WWW-Authenticate"] == "Basic"
    assert (server.registry.sites(), len(server.gateway.state.registry.list_unregistered())) == before
    assert server.registry.resolve("sh1", "camera", "cam-02") is None


@pytest.mark.parametrize("method,path,how", ADMIN_ROUTES, ids=[f"{m} {p}" for m, p, _ in ADMIN_ROUTES])
def test_the_same_route_with_the_admin_password_is_let_in(server, method, path, how):
    assert server.client.request(method, path, auth=ADMIN, **how).status_code in (200, 201)


def test_the_mission_package_password_is_not_the_admin_password_on_any_admin_route(server):
    for method, path, how in ADMIN_ROUTES:
        how = {**how, "headers": MISSION}
        assert server.client.request(method, path, auth=("admin", "mission-password-1"), **how).status_code == 401


def test_with_no_admin_hash_set_no_admin_route_can_be_used():
    registry = Registry(":memory:")
    server = GatewayServer(registry, None, None, resend=False, mission_packages=FakeMissionPackages())
    with TestClient(server.app) as client:
        assert client.post("/sites", auth=ADMIN, json=SITE).status_code == 401
        assert client.get("/unregistered", auth=ADMIN).status_code == 401
        assert client.get("/health").status_code == 200
    registry.close()


# --- the mission package password on an update ------------------------------------------------------------------


def test_an_update_without_the_sites_mission_package_password_is_401_and_changes_nothing(server):
    moved = {**PACKAGE, "lat": 10.0, "lon": 20.0}
    for how in ({}, {"headers": {"X-Mission-Package-Password": "not-the-password"}}, {"auth": ADMIN}):
        assert server.client.put("/sites/sh1", json=moved, **how).status_code == 401
    assert server.registry.sites() == [("sh1", 38.8895, -77.0353)]


def test_an_update_with_the_sites_mission_package_password_is_taken(server):
    moved = {**PACKAGE, "lat": 10.0, "lon": 20.0}
    assert server.client.put("/sites/sh1", json=moved, headers=MISSION).status_code == 200
    assert server.registry.sites() == [("sh1", 10.0, 20.0)]


def test_another_sites_mission_package_password_does_not_update_this_one(server):
    other = {
        **SITE,
        "site_id": "sh2",
        "mission_package_name": "sh2-mission",
        "mission_package_password": "other-password-2",
    }
    assert server.client.post("/sites", auth=ADMIN, json=other).status_code == 201
    moved = {**PACKAGE, "lat": 10.0, "lon": 20.0}
    r = server.client.put("/sites/sh1", json=moved, headers={"X-Mission-Package-Password": "other-password-2"})
    assert r.status_code == 401
    assert ("sh1", 38.8895, -77.0353) in server.registry.sites()


# --- a clear --------------------------------------------------------------------------------------------------

T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)


@pytest.fixture
def state(server):
    return server.gateway.state


def trip(state: GatewayState, at: datetime):
    ev = DetectionEvent(
        event_id=f"ev-{at.timestamp()}",
        type="camera.detection",
        time=at,
        severity=5,
        subsystem="camera",
        node_id="cam-01",
    )
    return state.apply_event("sh1", ev)


def clear(at: datetime, tripped: datetime | None = None) -> ClearRequest:
    return ClearRequest(subsystem="camera", node_id="cam-01", time=at, tripped=tripped)


def test_a_clear_ends_only_a_tripped_node(state):
    assert state.clear("sh1", clear(T0)) is None  # never tripped: no state at all
    first = trip(state, T0)
    assert state.clear("sh1", clear(T0 + timedelta(seconds=5))).status == Status.ok
    again = state.clear("sh1", clear(T0 + timedelta(seconds=9)))
    assert again is None  # ok already: a second clear changes nothing, and `updated` does not move
    assert state.snapshot()[0].updated == first.updated + timedelta(milliseconds=1)


def test_a_clear_ends_only_the_trip_it_names(state):
    old = trip(state, T0)
    newer = trip(state, T0 + timedelta(seconds=30))
    stale = clear(T0 + timedelta(seconds=40), tripped=old.updated)  # a tap on the alert that was showing before
    assert state.clear("sh1", stale) is None
    assert state.snapshot()[0].status == Status.tripped
    assert state.clear("sh1", clear(T0 + timedelta(seconds=41), tripped=newer.updated)).status == Status.ok


def test_a_clear_dated_before_the_trip_ends_nothing(state):
    trip(state, T0)
    assert state.clear("sh1", clear(T0 - timedelta(seconds=1))) is None
    assert state.clear("sh1", clear(T0)) is None  # not later than the trip
    assert state.snapshot()[0].status == Status.tripped


# --- the client certificate -------------------------------------------------------------------------------------


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _cert(subject: str, key, issuer_name: x509.Name | None, issuer_key, *, ca: bool) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _write(path, cert=None, key=None) -> str:
    if cert is not None:
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    else:
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    return str(path)


@pytest.fixture
def pki(tmp_path):
    """The TAK CA, the gateway's certificate, a phone's certificate, and a certificate from another CA."""
    ca_key, other_key, server_key, phone_key, stranger_key = (_key() for _ in range(5))
    ca = _cert("TAK CA", ca_key, None, ca_key, ca=True)
    other = _cert("Another CA", other_key, None, other_key, ca=True)
    files = {
        "ca": _write(tmp_path / "ca.pem", cert=ca),
        "cert": _write(tmp_path / "server.pem", cert=_cert("localhost", server_key, ca.subject, ca_key, ca=False)),
        "key": _write(tmp_path / "server.key", key=server_key),
        "phone": _write(tmp_path / "phone.pem", cert=_cert("phone", phone_key, ca.subject, ca_key, ca=False)),
        "phone_key": _write(tmp_path / "phone.key", key=phone_key),
        "stranger": _write(
            tmp_path / "stranger.pem", cert=_cert("stranger", stranger_key, other.subject, other_key, ca=False)
        ),
        "stranger_key": _write(tmp_path / "stranger.key", key=stranger_key),
    }
    return files


def handshake(server_context: ssl.SSLContext, ca: str, cert: str | None = None, key: str | None = None) -> None:
    """A whole TLS handshake in memory between the gateway's context and a caller's. Raises ssl.SSLError when
    either side refuses."""
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.load_verify_locations(cafile=ca)
    if cert:
        client.load_cert_chain(cert, key)
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(c_in, c_out, server_hostname="localhost")
    s = server_context.wrap_bio(s_in, s_out, server_side=True)
    done = {id(c): False, id(s): False}
    for _ in range(20):
        for side, out, peer_in in ((c, c_out, s_in), (s, s_out, c_in)):
            if not done[id(side)]:
                try:
                    side.do_handshake()
                    done[id(side)] = True
                except ssl.SSLWantReadError:
                    pass
            peer_in.write(out.read())
        if all(done.values()):
            # TLS 1.3 tells the caller of a refused certificate after its own handshake is done.
            try:
                c.read(1)
            except ssl.SSLWantReadError:
                return
    raise AssertionError("the handshake did not finish")


def reloader(pki) -> TLSReloader:
    return TLSReloader(
        TLSConfig(cert=pki["cert"], key=pki["key"], ca=pki["ca"], key_password=None, crl=None, reload_seconds=60)
    )


def test_a_caller_with_a_certificate_the_tak_ca_signed_is_let_in(pki):
    handshake(reloader(pki).context, pki["ca"], pki["phone"], pki["phone_key"])


def test_a_caller_with_no_certificate_is_refused_in_the_handshake(pki):
    context = reloader(pki).context
    assert context.verify_mode == ssl.CERT_REQUIRED
    with pytest.raises(ssl.SSLError):
        handshake(context, pki["ca"])


def test_a_caller_whose_certificate_another_ca_signed_is_refused(pki):
    with pytest.raises(ssl.SSLError):
        handshake(reloader(pki).context, pki["ca"], pki["stranger"], pki["stranger_key"])


def test_nothing_older_than_tls_1_2_is_spoken(pki):
    assert reloader(pki).context.minimum_version == ssl.TLSVersion.TLSv1_2
