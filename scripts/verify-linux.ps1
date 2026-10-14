$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskContext = Join-Path $taskRoot ("runtime/linux-build/" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskContext -Force | Out-Null
# An allowlist prevents .env, local tools/caches/data and unrelated files entering the image.
$taskAllowlist = @(
    'pyproject.toml', 'uv.lock', '.python-version', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'apps/core/src', 'apps/worker/src',
    'packages/contracts/src', 'packages/contracts/generated', 'packages/domain/src',
    'packages/application/src', 'packages/adapters/src',
    'scripts/check_imports.py', 'tests/tooling',
    'apps/panel/package.json', 'apps/panel/package-lock.json', 'apps/panel/.npmrc',
    'apps/panel/index.html', 'apps/panel/src', 'apps/panel/tests', 'apps/panel/tsconfig.json',
    'apps/panel/vite.config.ts', 'apps/panel/eslint.config.js'
)
foreach ($taskItem in $taskAllowlist) {
    $taskSource = Join-Path $taskRoot $taskItem
    $taskTarget = Join-Path $taskContext $taskItem
    New-Item -ItemType Directory -Path (Split-Path -Parent $taskTarget) -Force | Out-Null
    Copy-Item -LiteralPath $taskSource -Destination $taskTarget -Recurse
}
$taskRecipe = @'
FROM node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df AS panel
WORKDIR /check/apps/panel
COPY apps/panel/package.json apps/panel/package-lock.json apps/panel/.npmrc ./
RUN npm ci
COPY apps/panel/ ./
COPY packages/contracts/generated /check/packages/contracts/generated
RUN --network=none npm run lint && npm run typecheck && npm test && npm run build
FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS check
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_NO_PROGRESS=1 HF_HUB_OFFLINE=1
WORKDIR /check
COPY pyproject.toml uv.lock .python-version LICENSE THIRD_PARTY_NOTICES.md ./
COPY apps/core/src ./apps/core/src
COPY apps/worker/src ./apps/worker/src
COPY packages ./packages
RUN uv sync --locked --all-extras --no-editable
COPY scripts ./scripts
COPY tests ./tests
RUN --network=none uv run --locked --no-sync ruff check apps packages scripts tests  && uv run --locked --no-sync ruff format --check apps packages scripts tests  && uv run --locked --no-sync mypy  && uv run --locked --no-sync python scripts/check_imports.py  && uv run --locked --no-sync pytest  && uv run --locked --no-sync ai-platform-core-check  && uv run --locked --no-sync ai-platform-worker-check
RUN uv build --wheel
RUN --network=none uv pip install --python .venv/bin/python --no-deps --reinstall dist/*.whl  && .venv/bin/python -c "from pathlib import Path; import ai_agent_core, ai_agent_worker, ai_agent_contracts, ai_agent_domain, ai_agent_application, ai_agent_adapters; assert all('site-packages' in Path(m.__file__).parts for m in [ai_agent_core,ai_agent_worker,ai_agent_contracts,ai_agent_domain,ai_agent_application,ai_agent_adapters]); print('Installed wheel packages OK')"  && .venv/bin/ai-platform-core-check && .venv/bin/ai-platform-worker-check
COPY --from=panel /check/apps/panel/dist ./panel-dist
CMD [".venv/bin/ai-platform-core-check"]
'@
[IO.File]::WriteAllText((Join-Path $taskContext 'Dockerfile'), $taskRecipe, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $taskContext '.dockerignore'), "**/__pycache__/`n**/*.pyc`n**/*.pyo`n", [Text.UTF8Encoding]::new($false))
docker build --platform linux/amd64 --progress plain -t ai-agent-platform:m0-02-check $taskContext
if ($LASTEXITCODE -ne 0) { throw 'Linux bootstrap build failed' }
