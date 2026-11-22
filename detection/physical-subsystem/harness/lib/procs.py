"""Process registry for the conformance harness.

Every child the harness starts (the harness's mosquitto brokers, the simulator's IQ broker, the bridge under test) goes
through spawn(): it gets its own session, hence its own process group (pgid == pid), and is remembered here.

kill_all() ends every group this process created: SIGTERM, wait, SIGKILL. It only ever signals groups this process
spawned, so another harness run on the same host is never touched.

Pid safety: a live leader pins its pgid. A reaped leader's pgid stays pinned by Linux as long as the group still has
members, so a group is signalled only while /proc shows live members in it. Empty groups are never signalled.
"""
from __future__ import annotations
import atexit, os, signal, subprocess, threading, time

GRACE_S = 3.0
_LOCK = threading.RLock()
_CHILDREN: dict[int, subprocess.Popen] = {}   # pgid (== leader pid) -> Popen
_INSTALLED = False


def spawn(argv, **kw) -> subprocess.Popen:
    """Popen with start_new_session=True, registered for kill_all()."""
    kw["start_new_session"] = True
    with _LOCK:
        _prune()
        p = subprocess.Popen(argv, **kw)
        _CHILDREN[p.pid] = p
    return p


def _prune():
    """forget groups whose leader is reaped and that have no live members left"""
    for pid, p in list(_CHILDREN.items()):
        if p.returncode is not None and not _members(pid):
            del _CHILDREN[pid]


def _members(pgid: int) -> list[int]:
    """Live (non-zombie) pids in process group pgid, read from /proc. Reaps nothing."""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                st = f.read()
        except OSError:
            continue
        fields = st[st.rfind(")") + 2:].split()     # state ppid pgrp ...
        if len(fields) > 2 and fields[2].isdigit() and int(fields[2]) == pgid and fields[0] != "Z":
            out.append(int(name))
    return out


def _killpg(pgid: int, sig: int):
    if pgid <= 1 or pgid == os.getpgrp():           # never our own group
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _leader_exited(p: subprocess.Popen, timeout: float) -> bool:
    """Wait until p's leader has exited WITHOUT reaping it (WNOWAIT keeps it a zombie, pinning its pgid)."""
    if p.returncode is not None:
        return True
    end = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:                    # already reaped elsewhere
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _drain(pgid: int, grace: float):
    """SIGTERM the group's live members, wait up to `grace`, SIGKILL whatever is still there."""
    if not _members(pgid):
        return
    _killpg(pgid, signal.SIGTERM)
    end = time.monotonic() + grace
    while time.monotonic() < end and _members(pgid):
        time.sleep(0.05)
    if _members(pgid):
        _killpg(pgid, signal.SIGKILL)


def _reap(p: subprocess.Popen, timeout: float = 5):
    try:
        p.wait(timeout)
    except subprocess.TimeoutExpired:
        pass


def signal_group(p: subprocess.Popen, sig: int):
    """Send sig to the whole group of a registered child whose leader is still alive (freeze/thaw/continue)."""
    with _LOCK:
        if p.returncode is None:
            _killpg(p.pid, sig)


def stop(p: subprocess.Popen, sig: int = signal.SIGTERM, grace: float = 10.0):
    """Stop one child and its whole group. Returns with the leader reaped and the group empty."""
    with _LOCK:
        if p.returncode is None:
            _killpg(p.pid, sig)
            if not _leader_exited(p, grace):
                _killpg(p.pid, signal.SIGKILL)
                _leader_exited(p, 5)
        _drain(p.pid, grace=1.0)                     # stragglers: leader gone, group not
        _reap(p)
        _prune()


def kill_all(grace: float = GRACE_S):
    """SIGTERM every group this process spawned that still has live members, wait up to `grace` seconds,
    SIGKILL what is left, reap the leaders."""
    with _LOCK:
        groups = [p for p in _CHILDREN.values() if p.returncode is None or _members(p.pid)]
        for p in groups:
            _killpg(p.pid, signal.SIGTERM)
        end = time.monotonic() + grace
        while time.monotonic() < end and any(_members(p.pid) for p in groups):
            time.sleep(0.05)
        for p in groups:
            if _members(p.pid):
                _killpg(p.pid, signal.SIGKILL)
        for p in groups:
            _reap(p)
        _prune()


def install_exit_handlers():
    """kill_all() at interpreter exit and on SIGTERM/SIGINT. The signal path then dies with that signal (status preserved)."""
    global _INSTALLED
    if _INSTALLED or threading.current_thread() is not threading.main_thread():
        return
    _INSTALLED = True
    atexit.register(kill_all)

    def _on_signal(signum, _frame):
        kill_all()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _on_signal)
