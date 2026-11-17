"""Static grep audit: source-level evidence that an implementation cannot write to the panel or mutate via HTTP.
Usage: python -m audit.static_audit <impl_dir>   (exit 1 on findings).  Whitelist a line with 'br-allow' in a comment,
or list 'relative/path:substring' lines in <impl_dir>/.br-static-allow."""
from __future__ import annotations
import re, sys
from pathlib import Path

SRC_EXT = {".py", ".go", ".rs", ".js", ".ts", ".sh", ".c", ".h", ".cpp"}
SKIP_DIRS = {"harness", ".git", ".venv", "venv", "node_modules", "target", "vendor", "__pycache__", "tests", "test", "testdata", "build", "dist", ".mypy_cache"}
RULES = [
    (r"ui_arm(stay|away|night)", "IQ arm command"),
    (r"disarm_from_|disarm_the_panel", "IQ disarm command"),
    (r"generate_emergency", "IQ trigger police/fire/aux"),
    (r"\bipcCall\b", "IQ ipcCall (every ipcCall is state-changing)"),
    (r"execute_scene", "IQ scene execution"),
    (r"bypass_zoneid_set", "IQ zone bypass"),
    (r"send_virtual_device_description", "IQ ADC device write"),
    (r"\b(qzwaveservice|zwaveservice|qautomationservice|qcamservice)\b", "IQ z-wave/automation/camera service"),
    (r"quick_exit_state|speak_text|changeMasterVolumeLevel|changeDoorBellVolumeLevel", "IQ panel write"),
    (r"['\"](timeSync|acStatus|dealerLogo|photoFrameImageDownloadRequest)['\"]", "IQ non-required command"),
    (r"['\"]action['\"]\s*[:=,]\s*['\"](ARMING|ALARM)['\"]|action\s*[:=]\s*['\"](ARMING|ALARM)['\"]", "Control4 ARMING/ALARM action"),
    (r"['\"]?(usercode|user_code|userID)['\"]?\s*[:=]", "user code in an outbound payload"),
    (r"['\"]blackrelay/[^'\"]*/(set|command|cmd)['\"]", "command-like MQTT topic"),
    (r"(?i)@\w+\.(post|put|patch|delete)\(|methods\s*=\s*\[[^\]]*['\"](POST|PUT|PATCH|DELETE)|\bMethod(Post|Put|Patch|Delete)\b|routing::(post|put|patch|delete)\b|Method::(POST|PUT|PATCH|DELETE)\b|\.(POST|PUT|PATCH|DELETE)\(\s*['\"/]",
     "mutating HTTP route registration"),
]


def scan(impl: Path):
    impl = Path(impl)
    allow = []
    f = impl / ".br-static-allow"
    if f.exists():
        allow = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]
    findings, files = [], 0
    for p in impl.rglob("*"):
        if not p.is_file() or p.suffix not in SRC_EXT: continue
        rel = p.relative_to(impl)
        if set(rel.parts[:-1]) & SKIP_DIRS: continue
        if p.name.startswith("test_") or p.name.endswith(("_test.go", "_test.py", "_tests.rs")): continue
        files += 1
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if "br-allow" in line: continue
            for pat, why in RULES:
                if re.search(pat, line):
                    if any(a.split(":", 1)[0] == str(rel) and a.split(":", 1)[1] in line for a in allow if ":" in a): continue
                    findings.append((str(rel), n, why, line.strip()[:160]))
    return files, findings


if __name__ == "__main__":
    files, fnd = scan(Path(sys.argv[1]))
    print(f"scanned {files} source files, {len(fnd)} findings")
    for f in fnd: print("  %s:%d  [%s]  %s" % f)
    sys.exit(1 if fnd else 0)
