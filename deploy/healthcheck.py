"""Container liveness only; application readiness/access/STOP belongs to M1."""

import os
import socket
import sys
import time
from pathlib import Path


def main() -> None:
    if sys.argv[1] == "core":
        with socket.create_connection(
            ("127.0.0.1", int(os.environ.get("CORE_CONTAINER_PORT", "8080"))), timeout=2
        ):
            return
    elif sys.argv[1] == "worker":
        last = float(Path("/tmp/worker-heartbeat").read_text(encoding="ascii"))
        if not 0 <= time.monotonic() - last < 10:
            raise SystemExit("Worker liveness expired")
    else:
        raise SystemExit("Unknown healthcheck role")


if __name__ == "__main__":
    main()
