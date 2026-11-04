"""Offline `avatar-probe`: read what the loaded model offers and draft the mapping profile.

It connects to VTube Studio through the same loopback port the platform uses (the tunnel puts
the program there), lists hotkeys, expressions and parameters, and with ``--draft NAME.json``
writes a first guess into data/harness/avatar/. An existing profile is never overwritten.
The first run waits for the operator to click "Allow" in VTube Studio.
"""

import asyncio
import json
from pathlib import Path

from ai_agent_adapters.avatar.vts import VtsConfig, VtsPort
from ai_agent_application.avatar import AvatarError, draft_profile, profile_from_json

from .layout import HostError, paths


async def probe(root: Path, port: int, draft: str | None) -> int:
    host = paths(root)
    folder = host.root / "data" / "harness" / "avatar"
    folder.mkdir(parents=True, exist_ok=True)
    target = None
    if draft is not None:
        target = folder / draft
        if not draft.endswith(".json") or "/" in draft or "\\" in draft or target.exists():
            raise HostError("INVALID_OPTION")  # a plain new .json name; nothing is overwritten
    link = VtsPort(VtsConfig(url=f"ws://127.0.0.1:{port}", token_path=folder / "vts-token"))
    print("Connecting to VTube Studio; allow the plugin there if it asks (60 s)...")
    try:
        info = await link.connect()
    except AvatarError as error:
        raise HostError(error.code.upper().replace(":", "_").replace("-", "_")) from None
    finally:
        await link.close()
    print(f"Model: {info.name}")
    print("Hotkeys:     " + (", ".join(sorted(info.hotkeys)) or "-"))
    print("Expressions: " + (", ".join(sorted(info.expressions)) or "-"))
    print("Parameters:  " + (", ".join(sorted(info.parameters)) or "-"))
    profile, notes = draft_profile(info)
    profile_from_json(profile)  # a draft that does not load is a bug here, not the operator's
    for note in notes:
        print("  ! " + note)
    if target is not None:
        target.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"Draft written: {target}. Read it, fix it, then: configure-harness "
            f"--avatar-port {port} --avatar-profile {draft}"
        )
    else:
        print("Add --draft NAME.json to write a first guess of the profile.")
    return 0


def run(root: Path, port: int, draft: str | None) -> int:
    return asyncio.run(probe(root, port, draft))
