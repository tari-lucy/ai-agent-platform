"""M0 idle process: liveness only, no broker jobs, model or owner DB."""

import signal
import sys
import threading
import time
from pathlib import Path

from ai_agent_adapters.configuration import ConfigurationError

from ai_agent_worker.bootstrap import create_worker_bootstrap

STOP = threading.Event()


def stop(signum: int, frame: object) -> None:
    STOP.set()


def main() -> None:
    try:
        receipt = create_worker_bootstrap()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(78) from None
    assert receipt.state == "STOPPED" and not receipt.effects_enabled
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    heartbeat = Path("/tmp/worker-heartbeat")
    print("Worker scaffold idle; agent runtime is not started.", flush=True)
    while not STOP.is_set():
        heartbeat.write_text(str(time.monotonic()), encoding="ascii")
        STOP.wait(2)


if __name__ == "__main__":
    main()
