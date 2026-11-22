"""Regression tests for the confirming audit (HIGH 1): the outbound lock lacked `typing_extensions`, so the outbound and
fusion images crash-looped. The locks come from a real resolver run (docker/relock.sh) and every Dockerfile imports its
service modules at build time. These checks run offline; the real proof is `docker compose --profile '*' build`."""
import ast, pathlib, re, sys
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"
SRC = ROOT / "src"
SERVICES = {"iq-remote": "adapter", "control4": "adapter", "normalizer": "normalizer", "outbound": "outbound"}
THIRD = {"aiomqtt": "aiomqtt", "paho": "paho-mqtt", "yaml": "pyyaml", "argon2": "argon2-cffi", "aiohttp": "aiohttp"}


def locked(name):
    t = (DOCKER / f"requirements-{name}.lock.txt").read_text()
    return {m.group(1).lower().replace("_", "-"): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", t, re.M)}


def test_outbound_lock_has_typing_extensions_and_every_requirement_is_hashed():
    pk = locked("outbound")
    assert "typing-extensions" in pk
    for n in ("adapter", "normalizer", "outbound"):
        t = (DOCKER / f"requirements-{n}.lock.txt").read_text()
        entries = re.split(r"^(?=[A-Za-z0-9_.\-]+==)", t, flags=re.M)[1:]
        assert entries and all("--hash=sha256:" in e for e in entries), n
        assert "relock.sh" in t.splitlines()[0] and "Python 3.12" in t.splitlines()[0]


def test_locks_contain_the_direct_requirements_at_their_pinned_versions():
    for n in ("adapter", "normalizer", "outbound"):
        direct = {m.group(1).lower().replace("_", "-"): m.group(2) for m in
                  re.finditer(r"^([A-Za-z0-9_.\-]+)==(\S+)", (DOCKER / f"requirements-{n}.txt").read_text(), re.M)}
        pk = locked(n)
        for k, v in direct.items():
            assert pk.get(k) == v, (n, k)


def test_the_hand_maintained_lock_generator_is_gone_and_relock_uses_the_images_own_base():
    assert not (DOCKER / "lock.py").exists()
    sh = (DOCKER / "relock.sh").read_text()
    assert "pip-compile" in sh and "--generate-hashes" in sh and "PY_BASE" in sh


def _br2_imports(path: pathlib.Path):
    mods = set()
    pkg = path.relative_to(SRC).with_suffix("").parts
    top = []                                    # module-level imports only: that is what `import <module>` executes
    for st in ast.parse(path.read_text()).body:
        top.extend(ast.walk(st) if isinstance(st, (ast.Try, ast.If)) else [st])
    for n in top:
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = list(pkg[:-1])[: len(pkg) - 1 - (n.level - 1)]
                full = ".".join(base + ([n.module] if n.module else []))
                mods.add(full)
                mods |= {full + "." + a.name for a in n.names}
            elif n.module:
                mods.add(n.module)
                mods |= {n.module + "." + a.name for a in n.names}
    return mods


def _closure(entry_mods):
    seen, todo, third = set(), list(entry_mods), set()
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        if m.split(".")[0] != "br2":
            third.add(m.split(".")[0])
            continue
        f = SRC / (m.replace(".", "/") + ".py")
        if not f.exists():
            f = SRC / m.replace(".", "/") / "__init__.py"
        if f.exists():
            todo.extend(_br2_imports(f))
            if f.name != "__init__.py":
                parent = f.parent / "__init__.py"
                if parent.exists() and parent != SRC / "br2" / "__init__.py":
                    todo.append(".".join(parent.relative_to(SRC).parts[:-1]))
    return seen, third


def _copied(dockerfile: str) -> set[str]:
    out = set()
    for line in dockerfile.splitlines():
        m = re.match(r"COPY\s+(.+?)\s+(/app/\S+)\s*$", line.strip())
        if not m or not m.group(1).startswith("src/"):
            continue
        for src in m.group(1).split():
            p = ROOT / src
            if src.endswith("/"):
                out |= {str(f.relative_to(SRC)) for f in p.rglob("*.py")}
            else:
                out.add(str(p.relative_to(SRC)))
    return out


@pytest.mark.parametrize("svc,lock", sorted(SERVICES.items()))
def test_dockerfile_smoke_imports_the_entry_modules_and_everything_they_need_is_in_the_image(svc, lock):
    df = (DOCKER / f"Dockerfile.{svc}").read_text()
    m = re.search(r'^RUN python -c "import ([\w., ]+)"$', df, re.M)
    assert m, "no build-time import smoke test"
    assert df.index(m.group(0)) > df.index("COPY src/") and df.index(m.group(0)) < df.index("\nUSER ")
    assert df.index("PYTHONPATH=/app") < df.index(m.group(0))
    mods = [x.strip() for x in m.group(1).split(",")]
    entry = {"iq-remote": "br2.adapters.iq_remote", "control4": "br2.adapters.control4",
             "normalizer": "br2.normalizer.service", "outbound": "br2.outbound.service"}[svc]
    assert entry in mods
    seen, third = _closure(mods)
    copied = _copied(df)
    for mod in seen:
        if not mod.startswith("br2"):
            continue
        f = (SRC / (mod.replace(".", "/") + ".py"))
        pkgf = SRC / mod.replace(".", "/") / "__init__.py"
        assert (f.exists() and str(f.relative_to(SRC)) in copied) or (pkgf.exists() and str(pkgf.relative_to(SRC)) in copied) \
            or (not f.exists() and not pkgf.exists()), f"{mod} is imported by the smoke test but not COPY'd into the {svc} image"
    pk = locked(lock)
    for t in third:
        if t in THIRD:
            assert THIRD[t] in pk, f"{t} is imported but {THIRD[t]} is not in the {lock} lock"
    assert "typing-extensions" in pk or svc != "outbound"


def test_iq_remote_image_carries_the_field_tools_but_no_pairing_code():
    df = (DOCKER / "Dockerfile.iq-remote").read_text()
    assert "src/br2/field.py" in df and "pairing.py" not in df
    cp = (ROOT / "deploy" / "docker-compose.yml").read_text()
    assert "iq-tools:" in cp and 'user: "10001:10000"' in cp.split("iq-tools:")[1].split("control4:")[0]
