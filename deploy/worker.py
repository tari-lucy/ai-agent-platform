"""M0 idle process: liveness only, no broker jobs, model or owner DB."""

import signal
import threading
import time
from pathlib import Path

STOP = threading.Event()


def stop(signum: int, frame: object) -> None:
    STOP.set()


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    heartbeat = Path("/tmp/worker-heartbeat")
    print("Worker scaffold idle; agent runtime is not started.", flush=True)
    while not STOP.is_set():
        heartbeat.write_text(str(time.monotonic()), encoding="ascii")
        STOP.wait(2)


if __name__ == "__main__":
    main()
