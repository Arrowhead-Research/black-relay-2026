#!/usr/bin/env python3
"""pack-source.py <source dir> <out.tar.gz>: tar the camera-subsystem tree the node needs and print
a SHA-256 of its paths and contents (so the playbook only copies it when it changed). Skips relay/,
docs/ and provision/ at the top, and .venv, __pycache__, .pytest_cache, .env* anywhere."""
import hashlib, os, sys, tarfile

src, out = sys.argv[1], sys.argv[2]
TOP_SKIP = {"relay", "docs", "provision"}
SKIP = {".venv", "__pycache__", ".pytest_cache"}


def keep(rel):
    parts = rel.split("/")
    if parts[0] in TOP_SKIP or any(p in SKIP or p.startswith(".env") for p in parts):
        return False
    return not rel.endswith((".pyc", ".pyo"))


digest = hashlib.sha256()
with tarfile.open(out, "w:gz") as tar:
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, src).replace(os.sep, "/")
            if not keep(rel) or os.path.islink(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            digest.update(rel.encode() + b"\0" + str(os.stat(path).st_mode & 0o111).encode() + b"\0" + hashlib.sha256(data).digest())
            tar.add(path, arcname=rel, recursive=False)
print(digest.hexdigest())
