#!/usr/bin/env python3
"""Идёт к точке и ждёт прибытия. Печатает одну строку: OK x,y,z | STUCK dist=N | TIMEOUT dist=N.

    python3 goto_wait.py X Y Z [--tolerance 2] [--timeout 120]

Использует gotoXYZ и pathStatus мода. Это простой вариант: опрашивает положение, при остановке
навигатора вдали от цели трижды подряд говорит STUCK.
"""

import argparse
import json
import math
import sys
import time

from mcp_client import McpClient, McpError

POLL = 2.0
STILL_LIMIT = 3  # опросов подряд «навигатор не занят, а мы не пришли»


def position(client: McpClient):
    try:
        text, error = client.call("getGameState")
        if error:
            return None
        pos = json.loads(text)["self"]["pos"].split(",")
        return [float(v) for v in pos]
    except (McpError, ValueError, KeyError, TypeError):
        return None


def distance(pos, goal) -> float:
    return math.dist(pos, goal) if pos else float("inf")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("x", type=int)
    parser.add_argument("y", type=int)
    parser.add_argument("z", type=int)
    parser.add_argument("--tolerance", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    goal = (args.x, args.y, args.z)
    client = McpClient()
    try:
        client.call("gotoXYZ", {"x": args.x, "y": args.y, "z": args.z})
        start, still = time.monotonic(), 0
        while time.monotonic() - start < args.timeout:
            time.sleep(POLL)
            here = distance(position(client), goal)
            if here <= args.tolerance:
                print(f"OK {args.x},{args.y},{args.z}")
                return 0
            text, _ = client.call("pathStatus")
            try:
                busy = bool(json.loads(text).get("busy"))
            except (ValueError, AttributeError):
                busy = True
            still = 0 if busy else still + 1
            if still >= STILL_LIMIT:
                client.call("stopPathing")
                print(f"STUCK dist={here:.1f}")
                return 2
        client.call("stopPathing")
        print(f"TIMEOUT dist={here:.1f}")
        return 3
    except McpError as error:
        print("ОШИБКА:", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
