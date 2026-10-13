param([string]$BaseImage = 'sha256:a35928bd28aab333eed5a98e165ff8402f3b6c7f7fcba776c0f1e459f8cd0d97')
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$taskRun = 'ai-platform-m0-06-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskResults = Join-Path $taskRoot ('runtime/m0-06/' + $taskRun)
New-Item -ItemType Directory -Path $taskResults -Force | Out-Null
$taskImage = docker image inspect $BaseImage --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'M0-02 check image missing; no implicit pull' }
$taskCommon = @(
    '--network','none','--cpus','2','--memory','1536m','--memory-swap','1536m',
    '--read-only','--pids-limit','256','--tmpfs','/tmp:rw,size=128m',
    '--mount',"type=bind,source=$taskRoot/apps/core/src,target=/src/core,readonly",
    '--mount',"type=bind,source=$taskRoot/apps/worker/src,target=/src/worker,readonly",
    '--mount',"type=bind,source=$taskRoot/packages/adapters/src,target=/src/adapters,readonly",
    '--mount',"type=bind,source=$PSScriptRoot,target=/probe,readonly",
    '--mount',"type=bind,source=$taskRoot/deploy,target=/entrypoints,readonly",
    '-e','PYTHONPATH=/src/core:/src/worker:/src/adapters','-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPYCACHEPREFIX=/tmp/pycache'
)
docker run --name "$taskRun-tests" @taskCommon $taskImage `
    /check/.venv/bin/python -m pytest /probe/test_configuration.py -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw 'M0-06 Linux configuration tests failed' }
# Fresh processes simulate restart with no persisted active capabilities.
foreach ($taskBoot in @(1,2)) {
    docker run --name "$taskRun-bootstrap-$taskBoot" @taskCommon $taskImage `
        /check/.venv/bin/python -c 'from ai_agent_core.bootstrap import create_app; a=create_app(previous_state="ACTIVE"); import json; print(json.dumps(a.state.bootstrap_manifest)); assert a.state.bootstrap.state=="STOPPED" and not a.state.bootstrap.effects_enabled'
    if ($LASTEXITCODE -ne 0) { throw 'Fresh-process stopped bootstrap failed' }
}
$taskReceipt = @{
    image=$taskImage
    tests_container=(docker inspect "$taskRun-tests" --format '{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},"network":"{{.HostConfig.NetworkMode}}"}' | ConvertFrom-Json)
    bootstrap_processes=2
    uv_lock_sha256=(Get-FileHash (Join-Path $taskRoot 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText(
    (Join-Path $taskResults 'run-receipt.json'),
    ($taskReceipt | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false)
)
Write-Output "M0-06 passed. Results: runtime/m0-06/$taskRun"
