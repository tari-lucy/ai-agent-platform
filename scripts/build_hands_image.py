"""Build the W3b "hands" image over a verified harness image (network for apt/pip only).

The result keeps the base layers, the OpenCode binary and the trusted harness modules;
it adds tools the agent uses inside its sandbox. No host is prepared, nothing is enabled.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--base-image", required=True, help="sha256 ID of a W2+ harness image")
    args.add_argument("--tag", default="ai-agent-platform:opencode-hands")
    options = args.parse_args()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", options.base_image):
        raise ValueError("IMMUTABLE_IMAGE_REQUIRED")
    original = json.loads(
        subprocess.check_output(["docker", "image", "inspect", options.base_image])
    )[0]
    labels = original["Config"].get("Labels") or {}
    if (
        original["Id"] != options.base_image
        or original["Os"] != "linux"
        or labels.get("ai-agent-platform.opencode.version") != "1.19.3"
        or labels.get("ai-agent-platform.workspace.protocol") != "1"
    ):
        raise ValueError("HARNESS_IMAGE_UNEXPECTED")
    root = Path(__file__).resolve().parent.parent
    context = root / "runtime/hands-build" / uuid.uuid4().hex
    context.mkdir(parents=True)
    # BuildKit parses a bare sha256:ID as a repository name: a build-only alias.
    alias = "ai-agent-platform:hands-base-" + uuid.uuid4().hex
    subprocess.run(["docker", "image", "tag", options.base_image, alias], check=True)
    shutil.copyfile(root / "deploy/opencode/Dockerfile.hands", context / "Dockerfile")
    try:
        subprocess.run(
            [
                "docker", "build", "--pull=false",
                "--build-arg", "BASE_IMAGE=" + alias,
                "-t", options.tag,
                str(context),
            ],
            check=True,
        )  # fmt: skip
    finally:
        subprocess.run(["docker", "image", "rm", alias], check=False, capture_output=True)
        shutil.rmtree(context, ignore_errors=True)
    result = json.loads(subprocess.check_output(["docker", "image", "inspect", options.tag]))[0]
    base_layers = original["RootFS"]["Layers"]
    if result["RootFS"]["Layers"][: len(base_layers)] != base_layers:
        raise ValueError("BASE_IMAGE_CHANGED")
    built = result["Config"]["Labels"]
    if built.get("ai-agent-platform.hands") != "1" or any(
        built.get(key) != value for key, value in labels.items()
    ):
        raise ValueError("HANDS_IMAGE_UNEXPECTED")
    if result["Config"].get("User") != original["Config"].get("User"):
        raise ValueError("HANDS_IMAGE_USER")  # the runner's --user stays the only identity
    print("Prepared hands image: " + result["Id"])
    print("No host prepared, no permissions granted, no agent started.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError, ValueError, subprocess.CalledProcessError:
        print(
            "Hands image build failed; existing images and host stay as they were.", file=sys.stderr
        )
        raise SystemExit(1) from None
