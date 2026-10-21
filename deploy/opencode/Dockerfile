# Context is prepared by scripts/build_opencode_sandbox.ps1; never the private host.
FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
COPY opencode /usr/local/bin/opencode
COPY worker.py /opt/agent-harness/worker.py
COPY LICENSE.txt /opt/agent-harness/OPENCODE-LICENSE.txt
RUN --network=none python -c "import hashlib; from pathlib import Path; p=Path('/usr/local/bin/opencode'); assert hashlib.sha256(p.read_bytes()).hexdigest() == '179ca421d61eefeadfa0c50e084a6e79c1623734732f0f4593f81ccfd3ab7ee9'; p.chmod(0o555); c=Path('/opt/agent-harness/config/opencode'); c.mkdir(parents=True); (c/'.gitignore').touch(); c.chmod(0o555)"
LABEL ai-agent-platform.opencode.version="1.19.3"
LABEL ai-agent-platform.opencode.archive-sha256="531cff2441129c30f0edd5878af494108ad07f5feb46d7ee29a2a5b830a5765d"
# The trusted DockerTaskRunner supplies its PID 1 entrypoint and resource ceilings.
# No entrypoint starts OpenCode or the agent automatically.
