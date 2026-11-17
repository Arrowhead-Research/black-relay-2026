"""Mosquitto process management (apt-installed /usr/sbin/mosquitto 2.0.x) + helpers."""
from __future__ import annotations
import os, shutil, signal, socket, subprocess, time
from pathlib import Path
from lib import procs

MOSQUITTO = shutil.which("mosquitto") or "/usr/sbin/mosquitto"
MOSQUITTO_PASSWD = shutil.which("mosquitto_passwd") or "/usr/bin/mosquitto_passwd"


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait_port(port: int, timeout=10.0, host="127.0.0.1") -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection((host, port), 0.3).close(); return True
        except OSError:
            time.sleep(0.05)
    return False


class MosqProc:
    """One mosquitto process driven by a config file. kill/stall/cont semantics for outage scenarios."""

    def __init__(self, conf_path: Path, log_path: Path):
        self.conf_path, self.log_path = Path(conf_path), Path(log_path)
        self.proc: subprocess.Popen | None = None
        self.stopped = False

    def start(self, probe_ports=()):
        assert self.proc is None or self.proc.poll() is not None
        with open(str(self.log_path) + ".stderr", "ab") as err:
            # registered with lib.procs (own session + tracked), so every exit path can reap it
            self.proc = procs.spawn([MOSQUITTO, "-c", str(self.conf_path)], stdout=subprocess.DEVNULL, stderr=err)
        self.stopped = False
        for p in probe_ports:
            if not wait_port(p, 10):
                raise RuntimeError(f"mosquitto did not open port {p}; see {self.log_path}(.stderr)")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.proc is not None:
            procs.stop(self.proc, signal.SIGKILL, grace=5)   # SIGKILL also ends a SIGSTOPped group
        self.stopped = False

    def stop_term(self):
        if self.proc is not None:
            if self.stopped:
                procs.signal_group(self.proc, signal.SIGCONT)   # a stopped group ignores TERM until continued
            procs.stop(self.proc, signal.SIGTERM, grace=5)

    def freeze(self):  # TCP stays open (kernel accepts), no data ever flows
        procs.signal_group(self.proc, signal.SIGSTOP); self.stopped = True

    def thaw(self):
        procs.signal_group(self.proc, signal.SIGCONT); self.stopped = False


class Broker:
    """The harness's consumer-facing broker: TLS only, passwords, ACL (only `blackrelay` may write)."""
    USERS = {"blackrelay": "bridge-secret-pw", "consumer": "consumer-pw", "ha": "ha-pw"}
    ACL = """\
# bridge may write (and read nothing it doesn't need); consumers are read-only
user blackrelay
topic readwrite blackrelay/#
topic readwrite homeassistant/#

user consumer
topic read blackrelay/#
topic read homeassistant/#

user ha
topic read blackrelay/#
topic read homeassistant/#
# pattern rules intentionally absent: everything not listed is denied
"""

    def __init__(self, workdir: Path, pki, port: int | None = None):
        self.dir = Path(workdir) / "broker"; self.dir.mkdir(parents=True, exist_ok=True)
        self.pki, self.port = pki, port or free_port()
        self.ca_file = pki.root / "harness-ca.crt"
        self.password_file = self.dir / "passwd"
        self.bridge_password_file = self.dir / "bridge_password"
        self.log = self.dir / "mosquitto.log"
        (self.dir / "acl").write_text(self.ACL)
        if self.password_file.exists(): self.password_file.unlink()
        first = True
        for u, pw in self.USERS.items():
            subprocess.run([MOSQUITTO_PASSWD, "-b"] + (["-c"] if first else []) + [str(self.password_file), u, pw],
                           check=True, capture_output=True)
            first = False
        self.bridge_password_file.write_text(self.USERS["blackrelay"])
        (self.dir / "mosquitto.conf").write_text(f"""\
user root
persistence false
log_dest file {self.log}
log_type all
connection_messages true
allow_anonymous false
password_file {self.password_file}
acl_file {self.dir / 'acl'}
listener {self.port} 127.0.0.1
cafile {self.ca_file}
certfile {pki.root / 'broker.crt'}
keyfile {pki.root / 'broker.key'}
tls_version tlsv1.2
""")
        self.proc = MosqProc(self.dir / "mosquitto.conf", self.log)

    def start(self): self.proc.start([self.port]); return self
    def stop(self): self.proc.kill()

    def log_text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def subscriptions_by(self, client_id: str) -> list[str]:
        """Topics a given client id subscribed to (parsed from `log_type subscribe` lines:
        '<ts>: <client_id> <qos> <topic>')."""
        out = []
        for ln in self.log_text().splitlines():
            parts = ln.split(": ", 1)
            if len(parts) == 2:
                f = parts[1].split()
                if len(f) == 3 and f[0] == client_id and f[1] in "012":
                    out.append(f[2])
        return out
