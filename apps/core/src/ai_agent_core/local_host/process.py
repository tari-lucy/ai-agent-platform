"""One process per private host directory; acquire before opening any runtime state."""

import os
import socket
import sys
import threading
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .layout import HostError, restrict


@contextmanager
def exclusive_host(root: Path) -> Iterator[None]:
    lock = root / "host.lock"
    if lock.is_symlink():
        raise HostError("HOST_LOCK_UNSAFE")
    handle = None
    try:
        handle = lock.open("a+b")
        restrict(lock, directory=False)
    except OSError, HostError:
        if handle is not None:
            handle.close()
        raise HostError("HOST_LOCK_UNAVAILABLE") from None
    acquired = False
    try:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise HostError("HOST_ALREADY_RUNNING") from None
        yield
    finally:
        if acquired:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        # Never unlink: a waiting process may already hold the same inode/handle.


@contextmanager
def loopback_socket(port: int) -> Iterator[socket.socket]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if sys.platform == "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
        except OSError:
            raise HostError("PORT_UNAVAILABLE") from None
        yield listener


class StartedServer(Protocol):
    started: bool


def open_when_ready(server: StartedServer, port: int, stopped: threading.Event) -> None:
    for _ in range(600):
        if stopped.wait(0.1):
            return
        if server.started:
            try:
                if not webbrowser.open(f"http://127.0.0.1:{port}/", new=2):
                    print("BROWSER_NOT_OPENED: open the loopback panel manually", file=sys.stderr)
            except Exception:
                print("BROWSER_NOT_OPENED: open the loopback panel manually", file=sys.stderr)
            return
