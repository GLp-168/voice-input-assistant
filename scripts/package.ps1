$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    $python = "python"
}

Push-Location $root
try {
    & (Join-Path $root "scripts\build_webui.ps1")
    & $python -m pip install -U build
    & $python -m build
}
finally {
    Pop-Location
}
