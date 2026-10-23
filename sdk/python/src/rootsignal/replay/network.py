"""Outbound-network guard for the replay child process.

Replaces ``socket.socket.connect``/``connect_ex`` so that only allowlisted
(host, port) pairs can be reached: the Rootsignal API (to export the replay
trace) and, in live-model mode, the model host. Anything else raises
:class:`ReplayNetworkBlocked` and is recorded.

This is a guard against accidental side effects, not a sandbox: native
extensions, subprocesses or raw syscalls can bypass it (documented in
docs/REPLAY.md and the threat model).
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from typing import Any

from rootsignal.replay.session import ReplayNetworkBlocked

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_installed = False


def _resolve(host: str, port: int) -> set[tuple[str, int]]:
    out = {(host, port)}
    try:
        for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
            out.add((str(info[4][0]), port))
    except OSError:
        pass
    return out


def install(allowed: Iterable[tuple[str, int]],
            on_block: Callable[[str], None] | None = None) -> None:
    """Allow only ``allowed`` (host, port) pairs; idempotent per process."""
    global _installed
    allow: set[tuple[str, int]] = set()
    for host, port in allowed:
        allow |= _resolve(host, port)

    def check(sock: socket.socket, address: Any) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return  # unix sockets etc. are local
        host, port = str(address[0]), int(address[1])
        if (host, port) not in allow:
            target = f"{host}:{port}"
            if on_block:
                on_block(target)
            raise ReplayNetworkBlocked(f"replay blocked an outbound connection to {target}")

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        check(sock, address)
        _original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        check(sock, address)
        return _original_connect_ex(sock, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign,assignment]
    _installed = True


def uninstall() -> None:
    global _installed
    socket.socket.connect = _original_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _original_connect_ex  # type: ignore[method-assign]
    _installed = False
