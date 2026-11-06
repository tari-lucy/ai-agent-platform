#!/usr/bin/env bash
# Start the persona's local speech recognition (GigaAM v2) as a small container.
#
# It is made from the operator's own `transcriber` image (sherpa-onnx, numpy and ffmpeg are in it)
# and reads that service's model volume read-only; the transcriber itself is not touched.
# Published on the host's loopback only: 127.0.0.1:8090. No root needed, only docker.
#
#   bash install-asr.sh            start (or restart) it
#   docker rm -f aw-asr            remove it
set -euo pipefail
image=${ASR_IMAGE:-n8n-setup-transcriber}
volume=${ASR_VOLUME:-n8n-setup_transcriber_data}
port=${ASR_PORT:-8090}
here="$(dirname -- "$(readlink -f -- "$0")")"
target="$HOME/ai-platform/asr"
docker image inspect "$image" >/dev/null 2>&1 || { echo "No docker image $image."; exit 1; }
docker volume inspect "$volume" >/dev/null 2>&1 || { echo "No docker volume $volume."; exit 1; }
mkdir -p "$target"
install -m 644 "$here/gigaam_server.py" "$target/gigaam_server.py"
docker rm -f aw-asr >/dev/null 2>&1 || true
docker run -d --name aw-asr --restart unless-stopped \
    --memory 1500m --cpus 2 \
    --read-only --tmpfs /tmp:size=256m \
    --cap-drop ALL --security-opt no-new-privileges \
    -p "127.0.0.1:${port}:8090" \
    -v "$volume":/data:ro \
    -v "$target/gigaam_server.py":/srv/gigaam_server.py:ro \
    -e THREADS=2 -e MODEL_DIR=/data/models \
    --entrypoint python "$image" /srv/gigaam_server.py >/dev/null
for _ in $(seq 1 20); do
    if curl -fsS -m 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        echo "ASR_READY http://127.0.0.1:${port}"
        exit 0
    fi
    sleep 1
done
echo "The container started but does not answer yet: docker logs aw-asr"
exit 1
