"""Test PKI helpers (cryptography). RSA keys everywhere (the real panel PKI is RSA/PKCS8)."""
from __future__ import annotations
import datetime, hashlib, ipaddress
from dataclasses import dataclass
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

_NOW = lambda: datetime.datetime.now(datetime.timezone.utc)


def _key(bits=2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def key_pem(k) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def cert_pem(c) -> bytes:
    return c.public_bytes(serialization.Encoding.PEM)


def fingerprint_sha256(cert_or_der) -> str:
    der = cert_or_der if isinstance(cert_or_der, bytes) else cert_or_der.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


@dataclass
class CA:
    cert: x509.Certificate
    key: object

    @property
    def pem(self) -> bytes:
        return cert_pem(self.cert)


def make_ca(cn: str, days=3650, bc_critical=True, key_usage=True) -> CA:
    k = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(_NOW() - datetime.timedelta(days=1))
         .not_valid_after(_NOW() + datetime.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=bc_critical)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False))
    if key_usage:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                            encipher_only=False, decipher_only=False), critical=True)
    return CA(b.sign(k, hashes.SHA256()), k)


def issue(ca: CA, cn: str, *, server=True, client=False, ips=("127.0.0.1",), dns=("localhost",), days=365):
    """Return (cert, key) signed by ca."""
    k = _key()
    san = [x509.IPAddress(ipaddress.ip_address(i)) for i in ips] + [x509.DNSName(d) for d in dns]
    eku = []
    if server: eku.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if client: eku.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
         .issuer_name(ca.cert.subject).public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(_NOW() - datetime.timedelta(days=1)).not_valid_after(_NOW() + datetime.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.cert.public_key()), critical=False)
         .add_extension(x509.SubjectAlternativeName(san), critical=False))
    if eku: b = b.add_extension(x509.ExtendedKeyUsage(eku), critical=False)
    return b.sign(ca.key, hashes.SHA256()), k


def self_signed(cn: str, ips=("127.0.0.1",), days=365):
    """Self-signed leaf (Control4 panel style). No BasicConstraints, SAN only."""
    k = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    san = [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    b = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(_NOW() - datetime.timedelta(days=1))
         .not_valid_after(_NOW() + datetime.timedelta(days=days))
         .add_extension(x509.SubjectAlternativeName(san), critical=False))
    return b.sign(k, hashes.SHA256()), k


def write_pair(dirpath: Path, stem: str, cert, key) -> tuple[Path, Path]:
    dirpath.mkdir(parents=True, exist_ok=True)
    c, k = dirpath / f"{stem}.crt", dirpath / f"{stem}.key"
    c.write_bytes(cert_pem(cert)); k.write_bytes(key_pem(key)); k.chmod(0o600)
    return c, k


@dataclass
class Pki:
    """Everything a test session needs."""
    root: Path
    harness_ca: CA          # signs broker + API server certs (bridge's mqtt.ca_file, harness trust for HTTPS)
    sim_ca: CA              # the simulated panel's CA (bridge's <id>.qolsys / iq_remote.ca_file)
    rogue_ca: CA            # untrusted CA (panel cert from this must be refused)
    pki_id: str = "f2163e1a2b3c"  # 12 hex, as the library's random_mac without colons

    @classmethod
    def create(cls, root: Path) -> "Pki":
        root.mkdir(parents=True, exist_ok=True)
        p = cls(root, make_ca("BlackRelay Harness CA"), make_ca("Qolsys Sim Panel CA", bc_critical=False, key_usage=False),
                make_ca("Rogue CA"))
        # files
        (root / "harness-ca.crt").write_bytes(p.harness_ca.pem)
        (root / "sim-ca.crt").write_bytes(p.sim_ca.pem)
        (root / "rogue-ca.crt").write_bytes(p.rogue_ca.pem)
        c, k = issue(p.harness_ca, "mosquitto"); write_pair(root, "broker", c, k)
        c, k = issue(p.harness_ca, "bridge-api"); write_pair(root, "api", c, k)
        # pairing PKI exactly as the library lays it out: <pki_dir>/<id>/<id>.{key,secure,qolsys}
        d = root / "pki" / p.pki_id
        c, k = issue(p.sim_ca, "www.qolsys.com ", server=False, client=True, ips=(), dns=())
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{p.pki_id}.key").write_bytes(key_pem(k))
        (d / f"{p.pki_id}.secure").write_bytes(cert_pem(c))
        (d / f"{p.pki_id}.cer").write_bytes(cert_pem(c))
        (d / f"{p.pki_id}.qolsys").write_bytes(p.sim_ca.pem)
        for f in d.iterdir(): f.chmod(0o600)
        # a client cert from the rogue CA (negative test for panel-side client cert requirement)
        c, k = issue(p.rogue_ca, "rogue-client", server=False, client=True, ips=(), dns=())
        write_pair(root, "rogue-client", c, k)
        return p

    @property
    def mac(self) -> str:
        return ":".join(self.pki_id[i:i + 2] for i in range(0, 12, 2))
