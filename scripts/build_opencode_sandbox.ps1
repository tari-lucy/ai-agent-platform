param(
    [string]$ArchivePath,
    [string]$Tag = 'ai-agent-platform:opencode-1.19.3'
)
$ErrorActionPreference = 'Stop'
$r1Root = Split-Path -Parent $PSScriptRoot
$r1Build = Join-Path $r1Root ('runtime/opencode-build/' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $r1Build | Out-Null
$r1Expected = '531cff2441129c30f0edd5878af494108ad07f5feb46d7ee29a2a5b830a5765d'
if (-not $ArchivePath) {
    $ArchivePath = Join-Path $r1Build 'opencode.tar.gz'
    Invoke-WebRequest -Uri 'https://github.com/anomalyco/opencode/releases/download/v1.19.3/opencode-linux-x64-baseline.tar.gz' -OutFile $ArchivePath
}
$r1Archive = (Resolve-Path -LiteralPath $ArchivePath).Path
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $r1Archive).Hash.ToLowerInvariant() -ne $r1Expected) {
    throw 'OPENCODE_ARCHIVE_DIGEST_MISMATCH'
}
$r1Entries = @(tar -tf $r1Archive)
if ($LASTEXITCODE -ne 0 -or $r1Entries.Count -ne 1 -or $r1Entries[0] -ne 'opencode') {
    throw 'OPENCODE_ARCHIVE_CONTENTS_INVALID'
}
tar -xf $r1Archive -C $r1Build
if ($LASTEXITCODE -ne 0) { throw 'OPENCODE_EXTRACT_FAILED' }
Copy-Item -LiteralPath (Join-Path $r1Root 'deploy/opencode/Dockerfile') -Destination $r1Build
Copy-Item -LiteralPath (Join-Path $r1Root 'deploy/opencode/LICENSE.txt') -Destination $r1Build
Copy-Item -LiteralPath (Join-Path $r1Root 'packages/adapters/src/ai_agent_adapters/agent_harness/worker.py') -Destination $r1Build
docker build --pull=false --network=none --tag $Tag $r1Build
if ($LASTEXITCODE -ne 0) { throw 'OPENCODE_IMAGE_BUILD_FAILED' }
docker image inspect --format '{{.Id}}' $Tag
if ($LASTEXITCODE -ne 0) { throw 'OPENCODE_IMAGE_INSPECT_FAILED' }
