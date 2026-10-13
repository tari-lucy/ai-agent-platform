"""Installed wheel/entrypoint provenance and stopped state; stdlib + locked runtime only."""

import argparse
import hashlib
import json
from pathlib import Path

import ai_agent_adapters.configuration as configuration

import ai_agent_core.bootstrap as core
import ai_agent_worker.bootstrap as worker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("core", "worker"), required=True)
    args = parser.parse_args()
    expected = json.loads(Path("/probe/source-hashes.json").read_text())
    paths = {
        "configuration": configuration.__file__,
        "core": core.__file__,
        "worker": worker.__file__,
    }
    for name, path in paths.items():
        assert path is not None
        assert (
            hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            == expected[name]
        ), name
    entrypoint = Path("/opt/platform/" + args.role + ".py")
    assert (
        hashlib.sha256(entrypoint.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        == expected["deploy_" + args.role]
    )
    settings = configuration.load_configuration(environ={})
    receipt = core.create_app(settings, previous_state="ACTIVE").state.bootstrap
    worker_receipt = worker.create_worker_bootstrap(settings, previous_state="ACTIVE")
    assert receipt.state == worker_receipt.state == "STOPPED"
    assert not receipt.effects_enabled and not worker_receipt.effects_enabled
    assert receipt.epoch != worker_receipt.epoch
    assert not Path("/opt/platform/.env").exists()
    print(
        json.dumps(
            {
                "role": args.role,
                "installed_source_hashes_match": True,
                "state": "STOPPED",
                "effects_enabled": False,
            }
        )
    )


if __name__ == "__main__":
    main()
