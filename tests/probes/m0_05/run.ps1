param(
    [Parameter(Mandatory=$true)][string]$ModelCache,
    [string]$BaseImage = 'sha256:a35928bd28aab333eed5a98e165ff8402f3b6c7f7fcba776c0f1e459f8cd0d97'
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$taskCache = (Resolve-Path -LiteralPath $ModelCache).Path
$taskFixtures = Join-Path $taskRoot 'tests/fixtures/recall'
$taskCompat = Join-Path $taskRoot 'tests/probes/m0_03'
$taskRun = 'ai-platform-m0-05-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskResults = Join-Path $taskRoot ('runtime/m0-05/' + $taskRun)
New-Item -ItemType Directory -Path $taskResults -Force | Out-Null
$taskImage = docker image inspect $BaseImage --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'M0-02 check image missing; no implicit pull' }
if (-not (Test-Path -LiteralPath (Join-Path $taskCache 'model-manifest.json'))) {
    throw 'Pinned model cache missing; use M0-03 download workflow explicitly'
}
$taskCommon = @(
    '--cpus', '2', '--memory', '1536m', '--memory-swap', '1536m',
    '--pids-limit', '256', '--read-only', '--tmpfs', '/tmp:rw,size=128m',
    '--network', 'none',
    '--mount', "type=bind,source=$PSScriptRoot,target=/probe,readonly",
    '--mount', "type=bind,source=$taskFixtures,target=/fixtures,readonly",
    '--mount', "type=bind,source=$taskCompat,target=/compat,readonly",
    '--mount', "type=bind,source=$taskCache,target=/cache,readonly",
    '-e', 'HF_HUB_OFFLINE=1', '-e', 'HF_HUB_DISABLE_IMPLICIT_TOKEN=1',
    '-e', 'HF_HUB_DISABLE_TELEMETRY=1', '-e', 'HF_HUB_DISABLE_XET=1',
    '-e', 'TOKENIZERS_PARALLELISM=false', '-e', 'PYTHONDONTWRITEBYTECODE=1'
)
docker run --name "$taskRun-unit" @taskCommon $taskImage `
    /check/.venv/bin/python -m pytest /probe/test_recall.py -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw 'Corpus/privacy unit checks failed before benchmark' }
docker volume create $taskRun | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Isolated Linux volume creation failed' }
$taskReceipts = @()
foreach ($taskSize in @(1000,10000)) {
    foreach ($taskPhase in @('index','query')) {
        $taskName = "$taskRun-$taskPhase-$taskSize"
        docker run --name $taskName @taskCommon `
            --mount "type=volume,source=$taskRun,target=/data" $taskImage `
            /check/.venv/bin/python /probe/benchmark.py --phase $taskPhase --size $taskSize
        $taskExit = $LASTEXITCODE
        docker cp "${taskName}:/data/$taskPhase-$taskSize.json" (Join-Path $taskResults "$taskPhase-$taskSize.json")
        $taskState = docker inspect $taskName --format '{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},"memory":{{.HostConfig.Memory}},"nano_cpus":{{.HostConfig.NanoCpus}},"network":"{{.HostConfig.NetworkMode}}"}'
        $taskReceipts += @{ phase=$taskPhase; chunks=$taskSize; state=($taskState | ConvertFrom-Json) }
        if ($taskExit -ne 0) { throw "M0-05 $taskPhase/$taskSize failed; receipts retained" }
    }
}
$taskReceipt = @{
    image=$taskImage; containers=$taskReceipts
    uv_lock_sha256=(Get-FileHash (Join-Path $taskRoot 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText(
    (Join-Path $taskResults 'run-receipt.json'),
    ($taskReceipt | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false)
)
Write-Output "M0-05 retrieval gates passed. Results: runtime/m0-05/$taskRun"
Write-Output 'Stopped containers and isolated volume retained; no deletion or external API.'
