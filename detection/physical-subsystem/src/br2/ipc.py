"""One-way IPC: Unix-domain stream sockets carrying length-prefixed JSON frames.

IPC choice and justification
  * Unix sockets keep every hop local (no IP surface), are filesystem-permissioned, preserve order
    and give prompt EOF on peer death (the normalizer treats EOF as "adapter down").
  * Direction is enforced structurally, not by convention. The *receiving* end calls
    shutdown(SHUT_WR) (it can never write) and the *sending* end calls shutdown(SHUT_RD)
    (it can never read). On AF_UNIX each of those also marks the peer's opposite half closed, so a
    reverse write fails with EPIPE at the kernel level. There is no reader object on the sender
    class and no writer object on the receiver class.
Topology (data only ever moves left to right):
    adapters --adapters.sock--> normalizer --out.sock--> outbound
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import time
from typing import AsyncIterator, Awaitable, Callable

log = logging.getLogger("br2.ipc")
MAX_FRAME = 16 * 1024 * 1024
_HDR = struct.Struct(">I")


def encode(msg: dict) -> bytes:
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode()
    if len(body) > MAX_FRAME:
        raise ValueError("frame too large")
    return _HDR.pack(len(body)) + body


def decode_frames(buf: bytearray) -> list[dict]:
    """Pop complete frames from buf (mutates it)."""
    out = []
    while len(buf) >= 4:
        (n,) = _HDR.unpack_from(buf)
        if n > MAX_FRAME:
            raise ValueError("oversize frame")
        if len(buf) < 4 + n:
            break
        out.append(json.loads(bytes(buf[4:4 + n])))
        del buf[:4 + n]
    return out


class OneWayViolation(Exception):
    pass


class RecvOnlyConn:
    """Server-side accepted connection: can only receive. No send method exists."""

    def __init__(self, sock: socket.socket):
        sock.setblocking(False)
        sock.shutdown(socket.SHUT_WR)  # we will never write; peer's reads now see EOF
        self._sock = sock
        self._loop = asyncio.get_running_loop()
        self._buf = bytearray()

    def peer_can_receive(self) -> bool:
        """Test hook: attempt a reverse write; returns True only if the kernel accepted it."""
        try:
            self._sock.send(b"x")
            return True
        except OSError:
            return False

    async def frames(self, timeout: float | None = None) -> AsyncIterator[dict]:
        """Yield decoded frames. With `timeout`, raises asyncio.TimeoutError if nothing at all arrives for that
        long (a silent/hung peer): the caller treats that exactly like EOF."""
        while True:
            if timeout is None:
                chunk = await self._loop.sock_recv(self._sock, 65536)
            else:
                chunk = await asyncio.wait_for(self._loop.sock_recv(self._sock, 65536), timeout)
            if not chunk:
                return
            self._buf += chunk
            for m in decode_frames(self._buf):
                yield m

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class SendOnlyConn:
    """Accepted connection that can only send (normalizer -> outbound fan-out)."""

    def __init__(self, sock: socket.socket):
        sock.setblocking(False)
        sock.shutdown(socket.SHUT_RD)  # we never read; peer's writes now fail EPIPE
        self._sock = sock
        self._loop = asyncio.get_running_loop()

    def peer_can_send(self) -> bool:
        try:
            return self._sock.recv(1) != b""
        except BlockingIOError:
            return True
        except OSError:
            return False

    async def send(self, msg: dict) -> None:
        await self._loop.sock_sendall(self._sock, encode(msg))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _listen(path: str) -> socket.socket:
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o660)
    s.listen(16)
    s.setblocking(False)
    return s


class Listener:
    """Accepts connections on a unix path and hands each raw socket to `on_conn`."""

    def __init__(self, path: str, on_conn: Callable[[socket.socket], Awaitable[None]]):
        self.path = path
        self._on_conn = on_conn
        self._sock: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._children: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._sock = _listen(self.path)
        self._task = asyncio.create_task(self._accept_loop())

    async def _accept_loop(self) -> None:
        loop = asyncio.get_running_loop()
        assert self._sock
        while True:
            conn, _ = await loop.sock_accept(self._sock)
            t = asyncio.create_task(self._on_conn(conn))
            self._children.add(t)
            t.add_done_callback(self._children.discard)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._children):
            t.cancel()
        if self._sock:
            self._sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class SendOnlyClient:
    """Adapter-side (and anything upstream) sender. Reconnects forever; exposes only send()."""

    GAP_S = 2.5     # own-loop stall longer than this (SIGSTOP, swap storm): peer has presumably timed us out

    def __init__(self, path: str, hello: dict, on_connect: Callable[[], Awaitable[None]] | None = None,
                 retry_s: float = 0.5, on_gap: Callable[[], Awaitable[None]] | None = None,
                 ping_extra: Callable[[], dict] | None = None):
        self.path = path
        self.hello = hello
        self.on_connect = on_connect
        self.on_gap = on_gap
        self.ping_extra = ping_extra
        self.retry_s = retry_s
        self._sock: socket.socket | None = None
        self._lock = asyncio.Lock()
        self.connected = asyncio.Event()

    async def _connect(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.setblocking(False)
            try:
                await loop.sock_connect(s, self.path)
            except OSError:
                s.close()
                await asyncio.sleep(self.retry_s)
                continue
            s.shutdown(socket.SHUT_RD)  # structural: this process can never read from the normalizer
            self._sock = s
            await loop.sock_sendall(s, encode(self.hello))
            self.connected.set()
            return

    def recv_possible(self) -> bool:
        """Test hook: True if the socket would accept a read (it must not)."""
        if not self._sock:
            return False
        try:
            return self._sock.recv(1) != b""
        except BlockingIOError:
            return True
        except OSError:
            return False

    async def send(self, msg: dict) -> bool:
        """Best-effort send. Returns False if the normalizer is unreachable (message dropped; the
        owner re-sends a full snapshot from on_connect after reconnect, so nothing stale is replayed)."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._sock is None:
                self.connected.clear()
                return False
            try:
                data = encode(msg)
            except (ValueError, TypeError):
                return False                       # unencodable/oversize frame: drop it, keep the connection
            try:
                await loop.sock_sendall(self._sock, data)
                return True
            except OSError:
                self._drop()
                return False

    def _drop(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.connected.clear()

    async def maintain(self) -> None:
        """Run forever: (re)connect, invoke on_connect after each connect, send a 1 s keepalive. The keepalive is
        also what the normalizer's per-source liveness deadline listens to; a gap in our own loop triggers on_gap
        (the adapter re-sends a full snapshot, because the normalizer may have declared us dead meanwhile)."""
        last = time.monotonic()
        while True:
            try:
                if self._sock is None:
                    async with self._lock:
                        await self._connect()
                    if self.on_connect:
                        await self.on_connect()
                    last = time.monotonic()
                await asyncio.sleep(1.0)
                now = time.monotonic()
                gap = now - last
                last = now
                # keepalive frame: since this end cannot read, a write is how peer death (EPIPE) is noticed
                extra = {}
                try:
                    extra = self.ping_extra() if self.ping_extra else {}
                except Exception:  # noqa: BLE001
                    pass
                await self.send({"t": "ping", **extra})
                if gap > self.GAP_S and self.on_gap and self._sock is not None:
                    await self.on_gap()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  - the keepalive loop must never die
                log.exception("ipc maintain loop error")
                self._drop()
                await asyncio.sleep(self.retry_s)


class RecvOnlyClient:
    """Outbound-side receiver of the normalizer fan-out. Exposes only frames()."""

    def __init__(self, path: str, retry_s: float = 0.5, deadline_s: float | None = None):
        self.path = path
        self.retry_s = retry_s
        self.deadline_s = deadline_s      # no frame for this long => the producer is hung: drop + reconnect (new dump)

    async def messages(self) -> AsyncIterator[dict]:
        loop = asyncio.get_running_loop()
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.setblocking(False)
            try:
                await loop.sock_connect(s, self.path)
            except OSError:
                s.close()
                await asyncio.sleep(self.retry_s)
                continue
            conn = RecvOnlyConn(s)
            try:
                async for m in conn.frames(self.deadline_s):
                    if isinstance(m, dict):
                        yield m
                yield {"t": "ipc_closed"}
            except (OSError, ValueError, asyncio.TimeoutError):
                yield {"t": "ipc_closed"}
            finally:
                conn.close()
            await asyncio.sleep(self.retry_s)
