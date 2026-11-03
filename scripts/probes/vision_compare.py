"""Compare vision models on the persona's own frames (operator-run, real provider calls).

    python scripts/probes/vision_compare.py --root HOST --env-file .env \
        --frames /path/to/shots --models am/a,am/b [--limit 3] [--question "..."]

The key comes from the host's own explicit binding (never printed). For each model and frame it
prints the seconds, the tokens the provider reports and the description, so quality, price and
speed can be judged side by side. Frames are only sent to the configured provider.
"""

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

from ai_agent_core.local_host.models import _binding
from ai_agent_core.m3_composition.harness_look import FOCUS, PROMPT

DEFAULT_QUESTION = "что на кадре, кто рядом и что написано на табличках или надписях"


def ask(
    base: str, key: str, model: str, prompt: str, image: bytes, max_tokens: int
) -> tuple[float, dict, str]:
    url = "data:image/jpeg;base64," + base64.b64encode(image).decode()
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "ai-agent-platform/1.0",
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.loads(response.read(4 * 1024 * 1024))
    except Exception as error:  # noqa: BLE001 - a probe prints the failure kind only
        return time.monotonic() - started, {}, f"ERROR {type(error).__name__}: {str(error)[:120]}"
    text = (reply.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return time.monotonic() - started, reply.get("usage") or {}, text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--models", required=True, help="comma separated model ids")
    parser.add_argument("--limit", type=int, default=3, help="newest N frames")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--max-tokens", type=int, default=600)
    parser.add_argument("--repeat", type=int, default=1, help="calls per model and frame")
    arguments = parser.parse_args()
    base, key, _ = _binding(arguments.root, arguments.env_file)
    frames = sorted(
        (p for p in arguments.frames.glob("*.jp*g") if p.is_file() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: arguments.limit]
    prompt = PROMPT.format(question=FOCUS.format(question=arguments.question))
    for frame in frames:
        data = frame.read_bytes()
        print(f"=== {frame.name} ({len(data) // 1024} KB)")
        for model in arguments.models.split(","):
            for _ in range(arguments.repeat):
                seconds, usage, text = ask(
                    base, key, model.strip(), prompt, data, arguments.max_tokens
                )
                tokens = f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}"
                print(f"--- {model.strip()}  {seconds:.1f}s  {tokens}")
                print(" ".join(text.split())[:700])
    return 0


if __name__ == "__main__":
    sys.exit(main())
