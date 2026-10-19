$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    $python = "python"
}

Push-Location $root
try {
    & (Join-Path $root "scripts\build_webui.ps1")
    & $python -c "import importlib.metadata as m; import voice_input; print('voice-input-assistant ' + m.version('voice-input-assistant') + ' import ok')"
    if ($LASTEXITCODE -ne 0) { throw "Package import check failed." }
    & $python -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
}
finally {
    Pop-Location
}
