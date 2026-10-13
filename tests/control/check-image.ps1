#requires -Version 7.3
param([string]$WheelPath = 'runtime/m1-03/dist/ai_agent_platform-0.1.0-py3-none-any.whl')
$ErrorActionPreference = 'Stop'
$PSNativeCommandArgumentPassing = 'Standard'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$taskWheel = (Resolve-Path (Join-Path $taskRoot $WheelPath)).Path
if (-not $taskWheel.StartsWith($taskRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw 'Wheel must be inside this task checkout'
}
$taskName = 'ai-platform-m1-03-image-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskContext = Join-Path $taskRoot ('runtime/m1-03/' + $taskName)
New-Item -ItemType Directory -Force -Path (Join-Path $taskContext 'migrations/versions') | Out-Null
Copy-Item -LiteralPath $taskWheel -Destination (Join-Path $taskContext 'package.whl')
foreach ($taskFile in @('env.py','versions/0001_scopes.py','versions/0002_records.py')) {
    Copy-Item -LiteralPath (Join-Path $taskRoot ('migrations/' + $taskFile)) -Destination (Join-Path $taskContext ('migrations/' + $taskFile))
}
$taskDeps = 'sha256:95c9140de2ad85b389be609be48d69ab6a4a73235211c84ab8cf068a53937abf'
$taskChecks = 'sha256:a35928bd28aab333eed5a98e165ff8402f3b6c7f7fcba776c0f1e459f8cd0d97'
$taskRuntime = 'sha256:417e6ed0177900efddbe4fc7d35446e1faeb9174d8031e626794e308f487e83a'
foreach ($taskBase in @($taskDeps,$taskRuntime,$taskChecks)) {
    docker image inspect $taskBase --format '{{.Id}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Required immutable base absent; no pull' }
}
$taskActualDeps = docker image inspect ai-agent-platform/core-deps:m0-06 --format '{{.Id}}'
$taskActualRuntime = docker image inspect ai-agent-platform/core:m0-06 --format '{{.Id}}'
$taskActualChecks = docker image inspect ai-agent-platform:m0-02-check --format '{{.Id}}'
if ($taskActualChecks -ne $taskChecks -or $taskActualDeps -ne $taskDeps -or $taskActualRuntime -ne $taskRuntime) {
    throw 'Local base tags do not match required immutable IDs'
}
$taskDockerfile = @"
FROM ai-agent-platform:m0-02-check AS cached-runtime-dependency
FROM ai-agent-platform/core-deps:m0-06 AS dependencies
COPY --from=cached-runtime-dependency /check/.venv/lib/python3.14/site-packages/greenlet /opt/platform/.venv/lib/python3.14/site-packages/greenlet
COPY --from=cached-runtime-dependency /check/.venv/lib/python3.14/site-packages/greenlet-3.5.6.dist-info /opt/platform/.venv/lib/python3.14/site-packages/greenlet-3.5.6.dist-info
COPY package.whl /tmp/ai_agent_platform-0.1.0-py3-none-any.whl
RUN --network=none uv pip install --offline --no-index --no-deps --reinstall --python /opt/platform/.venv/bin/python /tmp/ai_agent_platform-0.1.0-py3-none-any.whl
FROM ai-agent-platform/core:m0-06 AS proof
COPY --from=dependencies /opt/platform/.venv /opt/platform/.venv
COPY migrations /opt/platform/migrations
"@
[IO.File]::WriteAllText((Join-Path $taskContext 'Dockerfile'), $taskDockerfile.Replace("`r`n","`n"), [Text.UTF8Encoding]::new($false))
$taskTag = 'ai-agent-platform/core:m1-03-offline-proof'
docker build --pull=false --network=none --tag $taskTag $taskContext
if ($LASTEXITCODE -ne 0) { throw 'Offline packaging build failed' }
$taskImage = docker image inspect $taskTag --format '{{.Id}}'
$taskVolume = $taskName + '-sqlite'
docker volume create --label ai-platform.task=M1-03 $taskVolume | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Volume creation failed' }
docker run --name ($taskName + '-volume-init') --user 0:0 --network none --cpus 0.5 --memory 128m --memory-swap 128m --read-only --pids-limit 64 --mount "type=volume,source=$taskVolume,target=/var/lib/platform/core" --entrypoint python $taskImage -c "import os; os.chown('/var/lib/platform/core',10001,10001)"
if ($LASTEXITCODE -ne 0) { throw 'Volume ownership initialization failed' }
$taskOutput = docker run --name $taskName --network none --cpus 2 --memory 768m --memory-swap 768m --read-only --pids-limit 128 --tmpfs /tmp:rw,size=64m --mount "type=volume,source=$taskVolume,target=/var/lib/platform/core" --mount "type=bind,source=$taskRoot/tests/control/image_probe.py,target=/tmp/image_probe.py,readonly" --entrypoint python $taskImage /tmp/image_probe.py
if ($LASTEXITCODE -ne 0) { throw 'Installed image acceptance failed' }
$taskProbe = $taskOutput | ConvertFrom-Json
$taskExpected = @{
    'ai_agent_application.control.ports'='packages/application/src/ai_agent_application/control/ports.py'
    'ai_agent_application.control.supervisor'='packages/application/src/ai_agent_application/control/supervisor.py'
    'ai_agent_adapters.control.journal'='packages/adapters/src/ai_agent_adapters/control/journal.py'
    'ai_agent_core.bootstrap'='apps/core/src/ai_agent_core/bootstrap.py'
}
foreach ($taskModule in $taskExpected.Keys) {
    $taskBytes = [Text.Encoding]::UTF8.GetBytes([IO.File]::ReadAllText((Join-Path $taskRoot $taskExpected[$taskModule])).Replace("`r`n","`n"))
    $taskHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($taskBytes)).ToLowerInvariant()
    if ($taskProbe.source_sha256_lf.$taskModule -ne $taskHash) { throw 'Installed source hash mismatch' }
}
foreach ($taskFile in @('env.py','versions/0001_scopes.py','versions/0002_records.py')) {
    $taskBytes = [Text.Encoding]::UTF8.GetBytes([IO.File]::ReadAllText((Join-Path $taskRoot ('migrations/' + $taskFile))).Replace("`r`n","`n"))
    $taskHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($taskBytes)).ToLowerInvariant()
    if ($taskProbe.migration_sha256_lf.$taskFile -ne $taskHash) { throw 'Installed migration hash mismatch' }
}
$taskState = docker inspect $taskName --format '{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},"network":"{{.HostConfig.NetworkMode}}","user":"{{.Config.User}}"}' | ConvertFrom-Json
if ($taskState.exit -ne 0 -or $taskState.oom -or $taskState.network -ne 'none' -or $taskState.user -ne '10001:10001') {
    throw 'Unexpected image probe isolation or result'
}
$taskReceipt = @{image=$taskImage; container=$taskName; state=$taskState; probe=$taskProbe}
[IO.File]::WriteAllText((Join-Path $taskContext 'image-receipt.json'), ($taskReceipt | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
Write-Output ($taskReceipt | ConvertTo-Json -Depth 8)