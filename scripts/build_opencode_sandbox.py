"""Linux/macOS counterpart of build_opencode_sandbox.ps1: the same pinned archive and context.

Explicit operator step, never run by serve. Downloads the exact release archive only when
--archive is not given, checks its SHA-256 and contents, prepares a private build context under
runtime/opencode-build/<id> and builds without network. The pinned Python base must already be
present locally (`docker pull` of the digest in deploy/opencode/Dockerfile) or is pulled once
here when --pull-base is given. Prints the image ID to put into the launcher configuration.
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = (
    "https://github.com/anomalyco/opencode/releases/download/v1.19.3/"
    "opencode-linux-x64-baseline.tar.gz"
)
EXPECTED = "531cff2441129c30f0edd5878af494108ad07f5feb46d7ee29a2a5b830a5765d"
HARNESS = ROOT / "packages/adapters/src/ai_agent_adapters/agent_harness"
CONTEXT = (
    ROOT / "deploy/opencode/Dockerfile",
    ROOT / "deploy/opencode/LICENSE.txt",
    HARNESS / "worker.py",
    HARNESS / "workspace_mcp.py",
    HARNESS / "helper_mcp.py",
)


def base_image() -> str:
    for line in (ROOT / "deploy/opencode/Dockerfile").read_text().splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    raise SystemExit("OPENCODE_DOCKERFILE_INVALID")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="an already downloaded release archive")
    parser.add_argument("--tag", default="ai-agent-platform:opencode-1.19.3")
    parser.add_argument("--pull-base", action="store_true", help="pull the pinned base digest")
    args = parser.parse_args(argv)
    build = ROOT / "runtime" / "opencode-build" / uuid.uuid4().hex
    build.mkdir(parents=True)
    archive = args.archive
    if archive is None:
        archive = build / "opencode.tar.gz"
        urllib.request.urlretrieve(URL, archive)  # noqa: S310 - fixed https release URL
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED:
        raise SystemExit("OPENCODE_ARCHIVE_DIGEST_MISMATCH")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if [m.name for m in members] != ["opencode"] or not members[0].isfile():
            raise SystemExit("OPENCODE_ARCHIVE_CONTENTS_INVALID")
        bundle.extract(members[0], build, filter="data")
    for source in CONTEXT:
        shutil.copy2(source, build / source.name)
    if args.pull_base:
        subprocess.run(["docker", "pull", base_image()], check=True)
    built = subprocess.run(
        ["docker", "build", "--pull=false", "--network=none", "--tag", args.tag, str(build)]
    )
    if built.returncode:
        raise SystemExit("OPENCODE_IMAGE_BUILD_FAILED")
    image = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.tag],
        capture_output=True,
        text=True,
    )
    if image.returncode:
        raise SystemExit("OPENCODE_IMAGE_INSPECT_FAILED")
    print(image.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
