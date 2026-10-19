$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root ".venv"
$python = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $python)) {
    python -m venv $venv
}

Push-Location $root
try {
    & $python -m pip install -U pip
    & $python -m pip install -e ".[dev]"
}
finally {
    Pop-Location
}

Write-Host "Development environment is ready."
