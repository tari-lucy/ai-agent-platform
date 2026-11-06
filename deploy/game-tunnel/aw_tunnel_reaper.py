"""Close the tunnel's dead SSH sessions so a returning computer can take its ports back.

When the operator's computer vanishes without a goodbye (power cut, sleep, a dropped network),
the server keeps its half of the SSH connection for up to a quarter of an hour: the kernel keeps
retransmitting into the void and sshd's own keepalive cannot end it. The dead session still owns
the reverse ports (VTube Studio, OBS, the audio bridge, the game), so the computer's new
connection finds them taken and gives up.

The computer's tunnel sends a keepalive every 15 seconds, so a healthy session never stays
silent for long. This program runs every few seconds (a systemd timer, as root), finds sessions of
the tunnel account that have received nothing for ``SILENT`` seconds and ends them. Only
processes whose command line says ``sshd: aw-game-tunnel`` are ever touched; nothing is logged but
the process number and the silence.
"""

import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable

SILENT = 75.0  # seconds without a byte from the computer (its keepalive comes every 15)
ACCOUNT = b"sshd: aw-game-tunnel"
PIDS = re.compile(r"pid=(\d+)")
LAST_RECEIVED = re.compile(r"lastrcv:(\d+)")


def stale(
    ss_output: str, limit: float, is_tunnel: Callable[[int], bool]
) -> list[tuple[int, float]]:
    """(pid, silent seconds) of tunnel processes whose connection has been silent too long.

    ``ss_output`` is ``ss -H -tnpi``: one line per connection with its processes, then an
    indented line with the socket details, which carry ``lastrcv`` in milliseconds.
    """
    found: list[tuple[int, float]] = []
    pids: list[int] = []
    for line in ss_output.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            pids = [int(p) for p in PIDS.findall(line)]
            continue
        match = LAST_RECEIVED.search(line)
        if match is None:
            continue
        silent = int(match.group(1)) / 1000.0
        if silent > limit:
            found += [(pid, silent) for pid in pids if is_tunnel(pid)]
    return found


def is_tunnel_process(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            return stream.read().startswith(ACCOUNT)
    except OSError:
        return False


def main() -> int:
    try:
        output = subprocess.run(
            ["ss", "-H", "-tnpi", "state", "established", "( sport = :22 )"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # fmt: skip
        print("ss failed", file=sys.stderr)
        return 1
    for pid, silent in stale(output, SILENT, is_tunnel_process):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        print(f"closed a tunnel session (process {pid}) silent for {silent:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
