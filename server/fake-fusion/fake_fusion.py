"""A stand-in for Fusion while the real one is down: the same three read-only routes the http monitor polls
(`/api/v1/events`, `/nodes`, `/health`, Fusion JSON/REST contract v1), fed by hand from a page at `/`.

Dev only. It runs in the gateway image (FastAPI + uvicorn are already there) and keeps its nodes and events in
one JSON file, so a restart keeps them. Cursors are milliseconds since the epoch, so they only ever grow: the
gateway's saved cursor is never ahead of a restarted fake.

    FUSION_URL=http://fake-fusion:8090   (in .env, then recreate gateway-http-monitor)
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

TOKEN = os.getenv("FUSION_TOKEN", "")
STATE = Path(os.getenv("FAKE_FUSION_STATE", "/state/fake-fusion.json"))
KEEP_EVENTS = 1000
MAX_LIMIT = 500

STATUS_TYPE = {"rf": "rf.node_status", "camera": "camera.node_status", "physical": "physical.panel_status"}

lock = threading.Lock()
state: dict = {"nodes": {}, "events": [], "last_cursor": 0}


def load() -> None:
    if STATE.exists():
        state.update(json.loads(STATE.read_text()))


def save() -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def key(site_id: str, subsystem: str, node_id: str) -> str:
    return f"{site_id}|{subsystem}|{node_id}"


def status_data(subsystem: str, up: bool) -> dict:
    if subsystem == "camera":
        return {"camera_online": up, "link_online": up}
    return {"state": "online" if up else "offline"}


def trip_data(subsystem: str, kind: str) -> tuple[str, dict]:
    """The event type and data that trip a node of this subsystem, shaped as the real bridges send them."""
    if subsystem == "rf":
        return "rf.alert", {
            "rule_id": f"fake-{kind}",
            "rule_type": kind,  # loiter | repeat_visitor | anything else reads "flagged"
            "severity": "high",
            "emitter": {"id": "fa:ke:00:00:00:01", "type": "wifi_client"},
            "context": {"dwell_s": 300} if kind == "loiter" else {"visits": 3, "window_s": 3600},
        }
    if subsystem == "camera":
        return "camera.detection", {"event": "object_start", "label": kind, "confidence": 0.91}
    if kind == "tamper":
        return "physical.event", {"event_type": "tamper", "tamper": True}
    # contact | motion | glass_break: the zone leaves its resting state
    return "physical.event", {
        "event_type": "zone_changed",
        "zone": {"id": "fake-zone", "kind": kind},
        "state": "open" if kind == "contact" else "active",
    }


def emit(site_id: str, subsystem: str, node_id: str | None, type_: str, data: dict) -> dict:
    cursor = max(int(time.time() * 1000), state["last_cursor"] + 1)
    state["last_cursor"] = cursor
    t = now_iso()
    ev = {
        "schema": "fusion.event.v0",
        "cursor": cursor,
        "id": f"fake-{uuid.uuid4()}",
        "source_id": "fake-fusion",
        "source_schema": f"{type_}.v0",
        "timestamp": t,
        "received_at": t,
        "site_id": site_id,
        "subsystem": subsystem,
        "node_id": node_id,
        "type": type_,
        "data": data,
        "source": {},
    }
    state["events"] = (state["events"] + [ev])[-KEEP_EVENTS:]
    return ev


def check(authorization: str | None) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, detail={"error": {"code": "unauthorized"}})


app = FastAPI(title="fake fusion")


# --- the contract the gateway polls ----------------------------------------------------------------------------


@app.get("/api/v1/health")
def health(authorization: str | None = Header(None)):
    check(authorization)
    return {"status": "ok", "storage": {"ok": True}, "brokers": [{"name": "fake", "connected": True}]}


@app.get("/api/v1/events")
def events(
    authorization: str | None = Header(None),
    after: int | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    site_id: str | None = None,
    subsystem: str | None = None,
    type: str | None = None,
    node_id: str | None = None,
):
    check(authorization)
    with lock:
        match = [
            e
            for e in state["events"]
            if (after is None or e["cursor"] > after)
            and (site_id is None or e["site_id"] == site_id)
            and (subsystem is None or e["subsystem"] == subsystem)
            and (type is None or e["type"] == type)
            and (node_id is None or e["node_id"] == node_id)
        ]
    page = match[:limit]
    next_cursor = page[-1]["cursor"] if page else (after or 0)
    return {"events": page, "next_cursor": next_cursor, "has_more": len(match) > limit}


@app.get("/api/v1/nodes")
def nodes(authorization: str | None = Header(None), site_id: str | None = None, subsystem: str | None = None):
    check(authorization)
    t, out = now_iso(), []
    with lock:
        for n in state["nodes"].values():
            if (site_id and n["site_id"] != site_id) or (subsystem and n["subsystem"] != subsystem):
                continue
            out.append(
                {
                    "site_id": n["site_id"],
                    "subsystem": n["subsystem"],
                    "node_id": n["node_id"],
                    "event_id": n["event_id"],
                    "timestamp": t,
                    "received_at": t,
                    "source_age_s": 1.0,
                    "receipt_age_s": 1.0,
                    "clock_ahead": False,
                    "stale": False,
                    "data": status_data(n["subsystem"], n["up"]),
                }
            )
    return {"nodes": out}


# --- the page that feeds it --------------------------------------------------------------------------------------


class NodeIn(BaseModel):
    site_id: str
    subsystem: str  # rf | camera | physical
    node_id: str


class Up(NodeIn):
    up: bool


class Trip(NodeIn):
    kind: str


def _need(n: NodeIn) -> None:
    if n.subsystem not in STATUS_TYPE or not n.site_id.strip() or not n.node_id.strip():
        raise HTTPException(422, "site_id, node_id and a subsystem of rf, camera or physical")


@app.post("/fake/status")
def set_status(n: Up):
    """Add a node (or set it online/offline): a status event now, and in /nodes from now on."""
    _need(n)
    with lock:
        ev = emit(n.site_id, n.subsystem, n.node_id, STATUS_TYPE[n.subsystem], status_data(n.subsystem, n.up))
        state["nodes"][key(n.site_id, n.subsystem, n.node_id)] = {
            "site_id": n.site_id, "subsystem": n.subsystem, "node_id": n.node_id, "up": n.up, "event_id": ev["id"],
        }
        save()
    return ev


@app.post("/fake/trip")
def trip(t: Trip):
    _need(t)
    with lock:
        ev = emit(t.site_id, t.subsystem, t.node_id, *trip_data(t.subsystem, t.kind))
        save()
    return ev


@app.post("/fake/remove")
def remove(n: NodeIn):
    """Fusion stops listing the node (no event: the real one would just go quiet)."""
    with lock:
        state["nodes"].pop(key(n.site_id, n.subsystem, n.node_id), None)
        save()
    return {"ok": True}


@app.get("/fake/state")
def show():
    with lock:
        return {"nodes": list(state["nodes"].values()), "events": state["events"][-25:][::-1]}


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Fake Fusion</title>
<style>
 body{font:14px system-ui,sans-serif;max-width:900px;margin:20px auto;padding:0 16px;background:#f6f6f4;color:#222}
 h1{font-size:20px} h2{font-size:15px;margin-top:28px} input,select,button{font:inherit;padding:5px 8px}
 table{border-collapse:collapse;width:100%;background:#fff} td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}
 .off{color:#b00} .on{color:#070} code{font-size:12px} .row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
 .note{color:#666}
</style>
<h1>Fake Fusion</h1>
<p class=note>Stand-in for Fusion. The gateway's http monitor polls this like the real one. Site id is the house's Fusion id.</p>
<h2>Add a node</h2>
<div class=row>
 <input id=site placeholder="site id" value="">
 <select id=sub><option>rf<option>camera<option>physical</select>
 <input id=node placeholder="node id">
 <button onclick="add()">Add (online)</button>
</div>
<h2>Nodes</h2><table id=nodes></table>
<h2>Last events</h2><table id=events></table>
<script>
const KINDS={rf:["loiter","repeat_visitor"],camera:["person","car","truck","dog"],physical:["contact","motion","glass_break","tamper"]};
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(r=>{if(!r.ok)r.text().then(alert)}).then(refresh);
function add(){const s=site.value.trim(),n=node.value.trim();try{localStorage.ffSite=s}catch(e){}post("/fake/status",{site_id:s,subsystem:sub.value,node_id:n,up:true})}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function refresh(){
 const st=await (await fetch("/fake/state")).json();
 nodes.innerHTML="<tr><th>site<th>subsystem<th>node<th>status<th>actions</tr>"+st.nodes.map((n,i)=>`<tr><td>${esc(n.site_id)}<td>${n.subsystem}<td>${esc(n.node_id)}
  <td class=${n.up?"on":"off"}>${n.up?"online":"offline"}
  <td class=row><select id=k${i}>${KINDS[n.subsystem].map(k=>`<option>${k}`).join("")}</select>
  <button onclick='act("trip",${i})'>Trip</button>
  <button onclick='act("${n.up?"down":"up"}",${i})'>${n.up?"Set offline":"Set online"}</button>
  <button onclick='act("remove",${i})'>Remove</button></tr>`).join("");
 window.N=st.nodes;
 events.innerHTML="<tr><th>cursor<th>time<th>node<th>type<th>data</tr>"+st.events.map(e=>`<tr><td>${e.cursor}<td>${e.timestamp.slice(11,19)}
  <td>${esc(e.site_id)} / ${e.subsystem} / ${esc(e.node_id)}<td>${e.type}<td><code>${esc(JSON.stringify(e.data))}</code></tr>`).join("");
}
function act(a,i){const n=N[i],b={site_id:n.site_id,subsystem:n.subsystem,node_id:n.node_id};
 if(a=="trip")post("/fake/trip",{...b,kind:document.getElementById("k"+i).value});
 else if(a=="remove")post("/fake/remove",b); else post("/fake/status",{...b,up:a=="up"});}
try{site.value=localStorage.ffSite||""}catch(e){}
refresh();setInterval(refresh,3000);
</script>"""


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


if __name__ == "__main__":
    load()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8090")))
