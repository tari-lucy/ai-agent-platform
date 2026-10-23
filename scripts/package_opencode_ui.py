"""Seal an already built upstream OpenCode adapter for `serve --opencode-ui-dir`."""

import argparse
from pathlib import Path

from ai_agent_core.local_host.layout import HostError
from ai_agent_core.opencode_ui.assets import prepare


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.build_dir)
    except (HostError, OSError) as error:
        print(
            "OpenCode bundle refused: "
            + (error.code if isinstance(error, HostError) else "LOCAL_IO_FAILED")
        )
        return 78
    print("OpenCode bundle packaged; no agent started, no model or native API connected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
