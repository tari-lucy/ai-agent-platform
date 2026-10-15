# Thin wrapper: .\scripts\local-host.ps1 init|serve --root C:\absolute\host ...
# Not executed on Windows yet (see docs/local-host-setup.md).
$ErrorActionPreference = 'Stop'
$python = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Create the venv first: uv sync --locked' }
& $python -m ai_agent_core.local_host @args
exit $LASTEXITCODE
