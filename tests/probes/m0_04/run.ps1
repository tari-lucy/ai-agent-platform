param([switch]$Build)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$taskProject = 'ai-platform-m0-04-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskResults = Join-Path $taskRoot ('runtime/m0-04/' + $taskProject)
New-Item -ItemType Directory -Path $taskResults -Force | Out-Null
$env:COMPOSE_DISABLE_ENV_FILE = '1'
$taskEnvPath = Join-Path $taskRoot '.env'
if (-not (Test-Path -LiteralPath $taskEnvPath)) { throw 'Run deploy/init-local-secrets.ps1 first' }
$taskListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
$taskListener.Start()
$taskPort = $taskListener.LocalEndpoint.Port
$taskListener.Stop()
$env:AI_PLATFORM_CORE_HOST_PORT = $taskPort.ToString()
$env:AI_PLATFORM_CORE_CONTAINER_PORT = '8080'
$taskCompose = @('compose','--env-file',$taskEnvPath,'-f',(Join-Path $taskRoot 'deploy/compose.yaml'),'-p',$taskProject)
if ($Build) {
    docker @taskCompose build
    if ($LASTEXITCODE -ne 0) { throw 'Compose build failed' }
}
docker @taskCompose config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose configuration invalid' }
$taskPersistence = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'persistence.py') -Raw
$taskReceipts = @()
try {
    docker @taskCompose up -d --wait --wait-timeout 90 --no-build
    if ($LASTEXITCODE -ne 0) { throw 'Compose readiness failed' }
    $taskResponse = Invoke-WebRequest -Uri "http://127.0.0.1:$taskPort/" -TimeoutSec 5
    if ($taskResponse.StatusCode -ne 200 -or $taskResponse.Content -notmatch 'AI Agent Platform') {
        throw 'Static panel unavailable'
    }
    $taskResult = $taskPersistence | docker @taskCompose exec -T core python - write
    if ($LASTEXITCODE -ne 0) { throw 'Initial persistence fixture failed' }
    $taskReceipts += ($taskResult | ConvertFrom-Json)
    docker @taskCompose exec -T worker python -c "from pathlib import Path; import os; assert 'REDIS_PASSWORD' not in os.environ; Path('/var/lib/platform/models/m0-04-marker').write_text('synthetic-cache')"
    if ($LASTEXITCODE -ne 0) { throw 'Worker cache/isolation fixture failed' }
    foreach ($taskOperation in @('restart','recreate')) {
        if ($taskOperation -eq 'restart') {
            docker @taskCompose restart
            if ($LASTEXITCODE -ne 0) { throw 'Restart failed' }
        } else {
            docker @taskCompose up -d --force-recreate --no-build --wait --wait-timeout 90
            if ($LASTEXITCODE -ne 0) { throw 'Recreate failed' }
        }
        docker @taskCompose up -d --wait --wait-timeout 90 --no-build
        if ($LASTEXITCODE -ne 0) { throw 'Readiness after restart/recreate failed' }
        $taskResult = $taskPersistence | docker @taskCompose exec -T core python - verify
        if ($LASTEXITCODE -ne 0) { throw 'Persistence verification failed' }
        $taskReceipts += ($taskResult | ConvertFrom-Json)
        docker @taskCompose exec -T worker python -c "from pathlib import Path; assert Path('/var/lib/platform/models/m0-04-marker').read_text()=='synthetic-cache'"
        if ($LASTEXITCODE -ne 0) { throw 'Worker cache not preserved' }
    }
    $taskIds = @(docker @taskCompose ps -q)
    if ($taskIds.Count -ne 3) { throw 'Expected exactly three service containers' }
    # Select safe fields inside Docker; never return Config.Env, credentials or bind source paths.
    $taskFormat = '{"role":{{json (index .Config.Labels "com.docker.compose.service")}},"image":{{json .Image}},"health":{{json .State.Health.Status}},"oom":{{.State.OOMKilled}},"memory":{{.HostConfig.Memory}},"swap":{{.HostConfig.MemorySwap}},"cpu":{{.HostConfig.NanoCpus}},"read_only":{{.HostConfig.ReadonlyRootfs}},"restart":{{json .HostConfig.RestartPolicy.Name}},"bindings":{{json .HostConfig.PortBindings}},"network":{{json .HostConfig.NetworkMode}},"mount_targets":[{{range $i,$m := .Mounts}}{{if $i}},{{end}}{{json $m.Destination}}{{end}}]}'
    $taskRaw = docker inspect @taskIds --format $taskFormat
    if ($LASTEXITCODE -ne 0) { throw 'Safe container inspection failed' }
    $taskState = @($taskRaw | ForEach-Object { $_ | ConvertFrom-Json -AsHashtable })
    $taskLimits = @{core=768MB; worker=1536MB; redis=256MB}
    $taskCpus = @{core=1000000000; worker=2000000000; redis=500000000}
    foreach ($taskContainer in $taskState) {
        $taskRole = $taskContainer.role
        if ($taskContainer.health -ne 'healthy' -or $taskContainer.oom) { throw 'Unhealthy/OOM service' }
        if ($taskContainer.memory -ne $taskLimits[$taskRole] -or $taskContainer.swap -ne $taskLimits[$taskRole]) { throw 'Wrong memory caps' }
        if ($taskContainer.cpu -ne $taskCpus[$taskRole] -or -not $taskContainer.read_only -or $taskContainer.restart -ne 'no') { throw 'Wrong CPU/isolation/restart config' }
        if ($taskRole -eq 'core') {
            if ($taskContainer.bindings.'8080/tcp'[0].HostIp -ne '127.0.0.1') { throw 'Wrong actual binding' }
        } elseif ($taskContainer.bindings.Count -ne 0) { throw 'Private service port published' }
        if ($taskContainer.network -eq 'host') { throw 'Host network prohibited' }
        if ($taskRole -eq 'worker' -and '/var/lib/platform/core' -in $taskContainer.mount_targets) { throw 'Worker has owner DB mount' }
    }
    $taskTcp = @(Get-NetTCPConnection -LocalPort $taskPort -State Listen -ErrorAction SilentlyContinue)
    if (-not $taskTcp -or @($taskTcp | Where-Object LocalAddress -ne '127.0.0.1').Count) { throw 'Windows listener not loopback-only' }
    $taskStats = docker stats --no-stream --format '{{.MemUsage}}' @taskIds
    if ($LASTEXITCODE -ne 0) { throw 'Memory snapshot unavailable' }
    $taskVm = Get-Process -Name vmmemWSL -ErrorAction SilentlyContinue
    $taskReport = @{
        checks=@{panel='pass'; restart='pass'; recreate='pass'; core_redis_persistence='pass'; worker_cache='pass'; loopback_private_ports='pass'; resource_caps='pass'}
        services=$taskState; persistence=$taskReceipts; container_memory_snapshot=@($taskStats)
        wsl_working_set_mib=if ($taskVm) { [math]::Round(($taskVm | Measure-Object WorkingSet64 -Sum).Sum/1MB,2) } else { $null }
        scope='synthetic idle scaffold; no agent/jobs/models'
    }
    [IO.File]::WriteAllText((Join-Path $taskResults 'result.json'), ($taskReport | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
    Write-Output "M0-04 passed. Local result: runtime/m0-04/$taskProject/result.json"
} finally {
    docker @taskCompose stop
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Probe stop failed; inspect own project only' }
}
Write-Output 'Probe containers stopped; all volumes/data/images retained.'
