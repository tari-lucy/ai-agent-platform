# M0-06 configuration/startup probe

Native: current core/worker/adapters src in PYTHONPATH, then pytest test_configuration.py.
Linux: ./tests/probes/m0_06/run.ps1 reuses pinned M0-02 check image read-only,
binds only current source/probe paths, network none, no actual .env/profile mounts.
Unit test bindings are explicitly public synthetic values on a .invalid endpoint;
no requests are made. Core/worker are guarded against sockets/process spawning.
Two separate processes receive a simulated persisted ACTIVE but produce fresh
epochs and STOPPED/effects false. This verifies bootstrap, not M1 STOP semantics.
No model download/dependency install/Compose/public output. Containers retained stopped.

Actual images: build deploy/Dockerfile targets core/worker with m0-06 tags,
then check-images.ps1 (installed module/entrypoint LF-normalized source hashes,
invalid boot exit 78, static HTTP200/idle heartbeat, stop own containers).
