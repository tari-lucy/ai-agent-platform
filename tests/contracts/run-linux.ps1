param([string]$BaseImage = 'sha256:a35928bd28aab333eed5a98e165ff8402f3b6c7f7fcba776c0f1e459f8cd0d97')
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$taskRun = 'ai-platform-m1-01-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskResults = Join-Path $taskRoot ('runtime/m1-01/' + $taskRun)
New-Item -ItemType Directory -Path $taskResults -Force | Out-Null
$taskImage = docker image inspect $BaseImage --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Pinned check image missing; no implicit pull' }
$taskArgs = @(
    '--name',$taskRun,'--network','none','--cpus','2','--memory','1536m',
    '--memory-swap','1536m','--read-only','--pids-limit','256','--tmpfs','/tmp:rw,size=128m',
    '--workdir','/check',
    '--mount',"type=bind,source=$taskRoot/pyproject.toml,target=/check/pyproject.toml,readonly",
    '--mount',"type=bind,source=$taskRoot/scripts,target=/check/scripts,readonly",
    '--mount',"type=bind,source=$taskRoot/tests,target=/check/tests,readonly",
    '--mount',"type=bind,source=$taskRoot/config/defaults,target=/check/config/defaults,readonly",
    '--mount',"type=bind,source=$taskRoot/deploy/core.py,target=/check/deploy/core.py,readonly",
    '--mount',"type=bind,source=$taskRoot/deploy/worker.py,target=/check/deploy/worker.py,readonly",
    '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPYCACHEPREFIX=/tmp/pycache',
    '-e','MYPY_CACHE_DIR=/tmp/mypy','-e','RUFF_CACHE_DIR=/tmp/ruff'
)
$taskSources = @(
    'apps/core/src','apps/worker/src','packages/contracts/src','packages/domain/src',
    'packages/application/src','packages/adapters/src'
)
foreach ($taskSource in $taskSources) {
    $taskArgs += @('--mount',"type=bind,source=$taskRoot/$taskSource,target=/check/$taskSource,readonly")
}
$taskArgs += @(
    '--mount',"type=bind,source=$taskRoot/packages/contracts/generated,target=/check/packages/contracts/generated,readonly",
    '-e',('PYTHONPATH=' + (($taskSources | ForEach-Object { '/check/' + $_ }) -join ':'))
)
$taskCommand = @'
set -e
/check/.venv/bin/python -m ai_agent_contracts.export --output packages/contracts/generated --check
/check/.venv/bin/python scripts/check_imports.py
/check/.venv/bin/python -m ruff check packages/contracts tests/contracts
/check/.venv/bin/python -m mypy apps/core/src apps/worker/src packages scripts
/check/.venv/bin/python -m pytest tests/tooling tests/contracts tests/probes/m0_03 tests/probes/m0_05 tests/probes/m0_06/test_configuration.py -q -p no:cacheprovider --tb=short
'@
docker run @taskArgs $taskImage /bin/sh -c $taskCommand
if ($LASTEXITCODE -ne 0) { throw 'M1-01 Linux combined checks failed' }
$taskReceipt = @{
    image=$taskImage
    container=(docker inspect $taskRun --format '{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},"network":"{{.HostConfig.NetworkMode}}"}' | ConvertFrom-Json)
    uv_lock_sha256=(Get-FileHash (Join-Path $taskRoot 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
    contract_export_sha256=@{}
}
foreach($taskFile in @('contracts.schema.json','openapi.json','contracts.ts')){
    $taskReceipt.contract_export_sha256[$taskFile]=(Get-FileHash (Join-Path $taskRoot ('packages/contracts/generated/'+$taskFile)) -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText(
    (Join-Path $taskResults 'run-receipt.json'),
    ($taskReceipt | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false)
)
Write-Output "M1-01 passed. Results: runtime/m1-01/$taskRun"
