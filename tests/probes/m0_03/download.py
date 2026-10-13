"""Download only the public, revision-pinned M0-03 ONNX fixture; no inference API."""

import hashlib
import json
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REPO = "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
FILES = (
    "README.md",
    "config.json",
    "model_optimized.onnx",
    "ort_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "unigram.json",
)


def checksums(root: Path) -> dict[str, dict[str, str | int]]:
    result = {}
    for name in FILES:
        path = root / name
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result[name] = {"sha256": digest, "bytes": path.stat().st_size}
    return result


def main() -> None:
    root = Path("/cache/model")
    info = HfApi(token=False).model_info(REPO, revision=REVISION)
    assert info.sha == REVISION
    assert info.card_data is not None and info.card_data.license == "apache-2.0"
    snapshot_download(
        repo_id=REPO,
        revision=REVISION,
        local_dir=root,
        cache_dir="/cache/hub",
        allow_patterns=list(FILES),
        token=False,
        max_workers=2,
    )
    manifest = {
        "model": MODEL,
        "repo": REPO,
        "revision": REVISION,
        "license": "apache-2.0",
        "files": checksums(root),
    }
    expected = json.loads(Path(__file__).with_name("model-manifest.json").read_text())
    assert manifest == expected, "Pinned artifact hashes differ"
    Path("/cache/model-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
