"""Shared helpers for the fusion (team output) tests."""
from __future__ import annotations
import json, pathlib, re, subprocess, time
from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMAS = {}
for _n, _s in (("physical.event.v0", "event"), ("physical.panel-status.v0", "panel-status"),
               ("physical.zone-status.v0", "zone-status")):
    SCHEMAS[_n] = Draft202012Validator(json.loads((ROOT / "schemas" / f"{_s}.v0.schema.json").read_text()),
                                       format_checker=FormatChecker())
TOPIC_FOR = {"physical.event.v0": "detection/physical/events", "physical.panel-status.v0": "detection/physical/status/panel",
             "physical.zone-status.v0": "detection/physical/status/zones"}


def validate(payload: bytes | str | dict) -> dict:
    """Raise if the message is not one UTF-8 JSON object valid against its own schema; returns the object."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    obj = json.loads(payload) if isinstance(payload, str) else payload
    assert isinstance(obj, dict) and next(iter(obj)) == "schema", "schema must be the first key"
    SCHEMAS[obj["schema"]].validate(obj)
    return obj


def zone(n=12, kind="contact", state="closed", stale=False, source="iq_remote", name=None, **kw):
    z = {"id": f"zone-{n}", "panel_zone_id": n, "name": name or f"Zone {n}", "kind": kind, "partition_id": 0,
         "state": state, "tamper": False, "battery_low": False, "online": True, "last_changed": "2026-01-01T00:00:00.000Z",
         "last_seen": None, "source": source, "stale": stale, "raw": {}}
    z.update(kw)
    return z


def log_event(seq, typ, subject, frm, to, source="iq_remote", synthetic=False, ts="2026-01-01T12:00:00.123Z"):
    return {"seq": seq, "id": f"evt-{seq}", "ts": ts, "type": typ, "subject": subject, "from": frm, "to": to,
            "source": source, "synthetic": synthetic}


def dump(zones, status="online", last_seq=0, links=None, active="iq_remote"):
    return {"t": "dump", "zones": zones, "partitions": [], "status": status, "last_seq": last_seq,
            "health": {"active_source": active, "links": links or {"iq_remote": "up", "control4": "disabled"},
                       "last_panel_message_epoch": 1767268800.5}}


def free_port():
    from lib.mosq import free_port as fp
    return fp()


def plain_broker_conf(dirp: pathlib.Path, port: int) -> pathlib.Path:
    """Plain TCP, anonymous, like the team broker today. `log_type subscribe` lets tests prove who subscribed."""
    dirp.mkdir(parents=True, exist_ok=True)
    conf = dirp / f"mosq-{port}.conf"
    conf.write_text(f"user root\npersistence false\nlog_dest file {dirp / 'mosq.log'}\nlog_type subscribe\n"
                    f"log_type notice\nconnection_messages true\nallow_anonymous true\nlistener {port} 127.0.0.1\n")
    return conf


def wait_until(fn, timeout=8.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()
