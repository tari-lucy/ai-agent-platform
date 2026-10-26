"""Refresh trusted harness modules over an installed pinned image; no downloads or RUN."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--base-image", required=True)
    args.add_argument("--tag", default="ai-agent-platform:opencode-helpers")
    options = args.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", options.base_image):
        raise ValueError("IMMUTABLE_IMAGE_REQUIRED")
    original = json.loads(
        subprocess.check_output(["docker", "image", "inspect", options.base_image])
    )[0]
    if (
        original["Id"] != options.base_image
        or original["Os"] != "linux"
        or (original["Config"].get("Labels") or {}).get("ai-agent-platform.opencode.version")
        != "1.19.3"
    ):
        raise ValueError("OPENCODE_IMAGE_UNEXPECTED")
    root = Path(__file__).resolve().parent.parent
    context = root / "runtime/harness-build" / uuid.uuid4().hex
    context.mkdir(parents=True)
    # BuildKit parses a bare sha256:ID as a repository name. Give the already
    # inspected local image a unique build-only alias and verify its layer prefix.
    alias = "ai-agent-platform:harness-base-" + uuid.uuid4().hex
    subprocess.run(["docker", "image", "tag", options.base_image, alias], check=True)
    digest = hashlib.sha256()
    for name in ("worker.py", "workspace_mcp.py", "helper_mcp.py"):
        source = root / "packages/adapters/src/ai_agent_adapters/agent_harness" / name
        if source.is_symlink():
            raise ValueError("HARNESS_LINK_REFUSED")
        data = source.read_bytes()
        digest.update(name.encode() + b"\0" + data)
        (context / name).write_bytes(data)
    shutil.copyfile(root / "deploy/opencode/Dockerfile.harness-refresh", context / "Dockerfile")
    subprocess.run(
        [
            "docker",
            "build",
            "--pull=false",
            "--network=none",
            "--build-arg",
            "BASE_IMAGE=" + alias,
            "--label",
            "ai-agent-platform.harness.sha256=" + digest.hexdigest(),
            "-t",
            options.tag,
            str(context),
        ],
        check=True,
    )
    result = json.loads(subprocess.check_output(["docker", "image", "inspect", options.tag]))[0]
    base_layers = original["RootFS"]["Layers"]
    if result["RootFS"]["Layers"][: len(base_layers)] != base_layers:
        raise ValueError("BASE_IMAGE_CHANGED")
    if result["Config"]["Labels"].get("ai-agent-platform.helper.protocol") != "1":
        raise ValueError("HELPER_IMAGE_UNEXPECTED")
    if result["Config"]["Labels"].get("ai-agent-platform.workspace.protocol") != "1":
        raise ValueError("WORKSPACE_IMAGE_UNEXPECTED")
    print("Prepared helper- and workspace-capable image: " + result["Id"])
    print("No host prepared, no permissions granted, no agent started.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError, ValueError, subprocess.CalledProcessError:
        print("Harness refresh failed; existing image and host remain available.", file=sys.stderr)
        raise SystemExit(1) from None
