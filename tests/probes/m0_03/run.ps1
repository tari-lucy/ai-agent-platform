param(
    [switch]$DownloadModel,
    [string]$BaseImage = 'ai-agent-platform:m0-02-check'
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$taskCache = Join-Path $taskRoot 'runtime/m0-03/model-cache'
$taskRun = 'ai-platform-m0-03-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskResults = Join-Path $taskRoot ('runtime/m0-03/' + $taskRun)
New-Item -ItemType Directory -Path $taskCache, $taskResults -Force | Out-Null
$taskImage = docker image inspect $BaseImage --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Build M0-02 Linux check image first; no implicit pull' }
$taskCommon = @(
    '--cpus', '2', '--memory', '1536m', '--memory-swap', '1536m',
    '--pids-limit', '256', '--read-only', '--tmpfs', '/tmp:rw,size=128m',
    '--mount', "type=bind,source=$PSScriptRoot,target=/probe,readonly",
    '-e', 'HF_HUB_DISABLE_IMPLICIT_TOKEN=1', '-e', 'HF_HUB_DISABLE_TELEMETRY=1',
    '-e', 'HF_HUB_DISABLE_XET=1', '-e', 'HF_HUB_DISABLE_PROGRESS_BARS=1',
    '-e', 'TOKENIZERS_PARALLELISM=false', '-e', 'PYTHONDONTWRITEBYTECODE=1'
)
if ($DownloadModel) {
    docker run --name "$taskRun-download" @taskCommon --network bridge `
        --mount "type=bind,source=$taskCache,target=/cache" `
        -e HF_HUB_OFFLINE=0 -e HF_HOME=/cache/hf $taskImage `
        /check/.venv/bin/python /probe/download.py
    if ($LASTEXITCODE -ne 0) { throw 'Pinned model download/checksum check failed' }
}
if (-not (Test-Path -LiteralPath (Join-Path $taskCache 'model-manifest.json'))) {
    throw 'No model cache: explicitly use -DownloadModel once'
}
docker volume create $taskRun | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Isolated Linux volume creation failed' }
$taskOffline = @(
    '--network', 'none', '--mount', "type=bind,source=$taskCache,target=/cache,readonly",
    '--mount', "type=volume,source=$taskRun,target=/data", '-e', 'HF_HUB_OFFLINE=1'
)
$taskReceipts = @()
foreach ($taskPhase in @('load', 'crash', 'restart')) {
    $taskStarted = [Diagnostics.Stopwatch]::StartNew()
    docker run --name "$taskRun-$taskPhase" @taskCommon @taskOffline $taskImage `
        /check/.venv/bin/python /probe/probe.py --phase $taskPhase
    if ($LASTEXITCODE -ne 0) { throw "M0-03 $taskPhase failed; resources retained for inspection" }
    $taskStarted.Stop()
    $taskReceipts += [pscustomobject]@{ phase=$taskPhase; wall_ms=$taskStarted.ElapsedMilliseconds }
}
docker run --name "$taskRun-checks" @taskCommon @taskOffline $taskImage `
    /check/.venv/bin/python -m pytest /probe/test_probe.py /check/tests/tooling -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw 'Probe/tooling tests failed' }
docker run --name "$taskRun-lint" @taskCommon @taskOffline $taskImage `
    /check/.venv/bin/ruff check --no-cache --config /check/pyproject.toml /probe
if ($LASTEXITCODE -ne 0) { throw 'Probe lint failed' }
docker run --name "$taskRun-format" @taskCommon @taskOffline $taskImage `
    /check/.venv/bin/ruff format --check --no-cache --config /check/pyproject.toml /probe
if ($LASTEXITCODE -ne 0) { throw 'Probe format failed' }
foreach ($taskPhase in @('load','restart')) {
    docker cp "${taskRun}-${taskPhase}:/data/${taskPhase}-result.json" (Join-Path $taskResults "$taskPhase-result.json")
    if ($LASTEXITCODE -ne 0) { throw 'Result copy failed' }
}
$taskState = docker inspect "$taskRun-restart" --format '{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},"memory":{{.HostConfig.Memory}},"nano_cpus":{{.HostConfig.NanoCpus}},"network":"{{.HostConfig.NetworkMode}}"}'
if ($LASTEXITCODE -ne 0) { throw 'Bounded runtime receipt unavailable' }
$taskReceipt = @{
    image=$taskImage; phases=$taskReceipts; restart_container=($taskState | ConvertFrom-Json)
    uv_lock_sha256=(Get-FileHash (Join-Path $taskRoot 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText((Join-Path $taskResults 'run-receipt.json'), ($taskReceipt | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
Write-Output "M0-03 passed. Local results: runtime/m0-03/$taskRun"
Write-Output "Stopped probe containers and isolated volume retained; no unrelated resources changed."
