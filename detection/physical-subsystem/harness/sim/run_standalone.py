#!/usr/bin/env python3
"""Run the panel simulator by hand so you can point a bridge at it.
  .venv/bin/python -m sim.run_standalone [workdir]
Prints ports/paths, then reads commands from stdin:
  zone <id> <Status>      e.g. zone 12 Open        arm <pid> <IQ status>   e.g. arm 0 ARM-AWAY
  add <id> <name> <sensortype> <status>            remove <id>
  iq outage|restore|drop|stall|unstall|mute|unmute|rogue|trusted      c4 outage|restore|drop|stall|unstall|rotate
  audit   (print violations)   quit
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.certs import Pki
from lib import procs
procs.install_exit_handlers()   # brokers started here are killed on quit, EOF, Ctrl-C or SIGTERM
from sim.panel_sim import PanelSim

wd = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="brsim-")).resolve()
pki = Pki.create(wd / "pki-root"); sim = PanelSim(wd / "sim", pki).start()
print(f"pki_dir={pki.root/'pki'}  ca_file={pki.root/'sim-ca.crt'}\niq_remote.panel_ip=127.0.0.1 panel_port={sim.iq.port}\n"
      f"control4 host=127.0.0.1 port={sim.c4.port} token_file={sim.c4.token_file} cert_sha256={sim.c4.fingerprint}\naudit={wd/'sim'/'audit.jsonl'}", flush=True)
for line in sys.stdin:
    a = line.split()
    if not a: continue
    try:
        if a[0] == "zone": sim.set_zone(int(a[1]), " ".join(a[2:]))
        elif a[0] == "arm": sim.set_arming(int(a[1]), a[2])
        elif a[0] == "add": sim.add_zone(int(a[1]), a[2], a[3], a[4])
        elif a[0] == "remove": sim.remove_zone(int(a[1]))
        elif a[0] == "iq": 
            m = a[1]; (sim.iq.mute(m == "mute") if m in ("mute", "unmute") else sim.iq.set_cert(m) if m in ("rogue", "trusted") else getattr(sim.iq, m)())
        elif a[0] == "c4": getattr(sim.c4, "rotate_cert" if a[1] == "rotate" else a[1])(); print("fp", sim.c4.fingerprint)
        elif a[0] == "audit": print(sim.violations or "no violations")
        elif a[0] == "quit": break
    except Exception as e: print("error:", repr(e))
sim.close()
