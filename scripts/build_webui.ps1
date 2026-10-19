$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$webRoot = Join-Path $root "webui"
$output = Join-Path $root "src\voice_input\web_dist\index.html"
$dist = Split-Path -Parent $output
$validator = Join-Path $root "scripts\validate_web_dist.py"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    if (Test-Path -LiteralPath $output) {
        Write-Host "npm is unavailable; using the checked-in Web UI build."
        exit 0
    }
    throw "npm is required to build the Web UI and no checked-in build was found."
}

Push-Location $webRoot
try {
    if (Test-Path -LiteralPath (Join-Path $webRoot "package-lock.json")) {
        & npm ci --no-audit --no-fund
    } else {
        & npm install --no-audit --no-fund
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Web UI dependency installation failed."
    }
    & npm run check
    if ($LASTEXITCODE -ne 0) {
        throw "Web UI JavaScript validation failed."
    }
    & npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Web UI build failed."
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $output)) {
    throw "Web UI build did not produce $output"
}

& python $validator $dist --write-manifest
if ($LASTEXITCODE -ne 0) {
    throw "Web UI integrity manifest generation failed."
}

Write-Host "Web UI built: $output"
