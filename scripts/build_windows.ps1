param(
    [string]$ProductTier = "",
    [string]$ModelProfile = "",
    [string]$LexiconDir = "",
    [string]$LexiconCatalog = "",
    [string]$EnabledLexicons = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProductTier)) {
    $ProductTier = $env:VOICE_INPUT_PRODUCT_TIER
}
if ([string]::IsNullOrWhiteSpace($ProductTier)) {
    $ProductTier = "enterprise"
}
$ProductTier = $ProductTier.Trim().ToLowerInvariant()
$validProductTiers = @("basic", "standard", "enterprise")
if ($validProductTiers -notcontains $ProductTier) {
    throw "Unsupported product tier '$ProductTier'. Use one of: $($validProductTiers -join ', ')."
}
if ([string]::IsNullOrWhiteSpace($ModelProfile)) {
    $ModelProfile = $env:VOICE_INPUT_MODEL_PROFILE
}
$defaultModelProfile = if ($ProductTier -eq "basic") { "streaming" } else { "full" }
if ([string]::IsNullOrWhiteSpace($ModelProfile)) {
    $ModelProfile = $defaultModelProfile
}
$ModelProfile = $ModelProfile.Trim().ToLowerInvariant()
$validModelProfiles = @("streaming", "full")
if ($validModelProfiles -notcontains $ModelProfile) {
    throw "Unsupported model profile '$ModelProfile'. Use one of: $($validModelProfiles -join ', ')."
}

$root = Split-Path -Parent $PSScriptRoot
$projectFile = Join-Path $root "pyproject.toml"
if (-not (Test-Path -LiteralPath $projectFile)) {
    throw "Missing project metadata: $projectFile"
}
$versionMatch = [regex]::Match(
    (Get-Content -LiteralPath $projectFile -Raw -Encoding UTF8),
    '(?m)^\s*version\s*=\s*"([^"]+)"'
)
if (-not $versionMatch.Success) {
    throw "Unable to read project version from: $projectFile"
}
$productVersion = $versionMatch.Groups[1].Value.Trim()
if ($productVersion -notmatch '^[0-9A-Za-z][0-9A-Za-z.+-]*$') {
    throw "Project version is not safe for Windows artifact names: $productVersion"
}
if ([string]::IsNullOrWhiteSpace($LexiconDir)) {
    $LexiconDir = $env:VOICE_INPUT_LEXICON_DIR
}
if ([string]::IsNullOrWhiteSpace($LexiconCatalog)) {
    $LexiconCatalog = $env:VOICE_INPUT_LEXICON_CATALOG
}
if ([string]::IsNullOrWhiteSpace($EnabledLexicons)) {
    $EnabledLexicons = $env:VOICE_INPUT_ENABLED_LEXICONS
}
$buildRoot = Join-Path $root "build\windows"
$buildEnvRoot = Join-Path $root "build\windows-venv"
$pyiDist = Join-Path $buildRoot "pyinstaller-dist"
$pyiWork = Join-Path $buildRoot "pyinstaller-work"
$pyiSpec = Join-Path $buildRoot "spec"
$stageRoot = Join-Path $buildRoot "stage"
$appStage = Join-Path $stageRoot "VoiceInputAssistant"
$installerStage = Join-Path $buildRoot "installer"
$outputDir = Join-Path $root "dist\windows"
$productOutputSuffix = if ($ProductTier -eq "enterprise") { "" } else { "-$ProductTier" }
if ($ModelProfile -ne $defaultModelProfile) {
    $productOutputSuffix += "-$ModelProfile"
}
$portableZip = Join-Path $outputDir "VoiceInputAssistant$productOutputSuffix-portable.zip"
$installerZip = Join-Path $outputDir "VoiceInputAssistant$productOutputSuffix-installer.zip"
$setupBundle = Join-Path $outputDir "VoiceInputAssistantSetup$productOutputSuffix"
$pyinstallerSetupBundle = Join-Path $outputDir "VoiceInputAssistantSetup"
$setupProfileName = if ($ModelProfile -eq "streaming") { "Streaming" } else { "Full" }
$setupExeName = "VoiceInputAssistant-v$productVersion-$setupProfileName-Setup.exe"
$setupExe = Join-Path $setupBundle $setupExeName
$profileAssets = Join-Path $buildRoot "profile-assets"
$profileSrc = Join-Path $buildRoot "profile-src"
$profileLexicons = Join-Path $profileAssets "lexicons"
$productManifest = Join-Path $profileAssets "product.json"
$appIcon = Join-Path $root "assets\icons\app.ico"
$webUiBuildScript = Join-Path $root "scripts\build_webui.ps1"
$webUiDist = Join-Path $root "src\voice_input\web_dist"
$webUiValidator = Join-Path $root "scripts\validate_web_dist.py"
$windowsIconValidator = Join-Path $root "scripts\validate_windows_exe_icon.py"
$windowsBuildLock = Join-Path $root "requirements\windows-build.lock.txt"
$windowsBuildBootstrapLock = Join-Path $root "requirements\windows-build-bootstrap.lock.txt"
$funasrPatchDir = Join-Path $root "packaging\windows\funasr_patches"
if ([string]::IsNullOrWhiteSpace($LexiconDir)) {
    $lexiconAssets = Join-Path $root "assets\lexicons"
} elseif ([System.IO.Path]::IsPathRooted($LexiconDir)) {
    $lexiconAssets = [System.IO.Path]::GetFullPath($LexiconDir)
} else {
    $lexiconAssets = [System.IO.Path]::GetFullPath((Join-Path $root $LexiconDir))
}
if ([string]::IsNullOrWhiteSpace($LexiconCatalog)) {
    $defaultLexiconCatalog = Join-Path $lexiconAssets "catalog.json"
    $lexiconCatalogPath = if (Test-Path -LiteralPath $defaultLexiconCatalog) { $defaultLexiconCatalog } else { "" }
} elseif ([System.IO.Path]::IsPathRooted($LexiconCatalog)) {
    $lexiconCatalogPath = [System.IO.Path]::GetFullPath($LexiconCatalog)
} else {
    $lexiconCatalogPath = [System.IO.Path]::GetFullPath((Join-Path $root $LexiconCatalog))
}
$vendorFunASRModels = Join-Path $root "vendor\funasr\models"
$funasrStreamingModelName = "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
$funasrOfflineModelName = "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
$funasrVadModelName = "speech_fsmn_vad_zh-cn-16k-common-pytorch"
$funasrCompactPuncModelName = "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
$funasrLargePuncModelName = "punc_ct-transformer_cn-en-common-vocab471067-large"
$funasrSpeakerModelName = "speech_campplus_sv_zh-cn_16k-common"
$funasrStreamingModel = Join-Path $vendorFunASRModels $funasrStreamingModelName
$funasrOfflineModel = Join-Path $vendorFunASRModels $funasrOfflineModelName
$funasrVadModel = Join-Path $vendorFunASRModels $funasrVadModelName
$funasrCompactPuncModel = Join-Path $vendorFunASRModels $funasrCompactPuncModelName
$funasrLargePuncModel = Join-Path $vendorFunASRModels $funasrLargePuncModelName
$toolLauncherScript = Join-Path $root "packaging\windows\tool_launcher.py"
$funasrModelProfiles = @{
    streaming = [ordered]@{
        streaming = $funasrStreamingModelName
    }
    full = [ordered]@{
        streaming = $funasrStreamingModelName
        offline = $funasrOfflineModelName
        vad = $funasrVadModelName
        punc = if ($ProductTier -eq "enterprise") { $funasrLargePuncModelName } else { $funasrCompactPuncModelName }
        speaker = $funasrSpeakerModelName
    }
}
$funasrModelRequiredFiles = @{
    $funasrStreamingModelName = @("configuration.json", "config.yaml", "model.pt", "tokens.json", "seg_dict", "am.mvn")
    $funasrOfflineModelName = @("configuration.json", "config.yaml", "model.pt", "tokens.json", "seg_dict", "am.mvn")
    $funasrVadModelName = @("configuration.json", "config.yaml", "model.pt", "am.mvn")
    $funasrCompactPuncModelName = @("configuration.json", "config.yaml", "model.pt", "tokens.json")
    $funasrLargePuncModelName = @("configuration.json", "config.yaml", "model.pt", "tokens.json", "jieba_usr_dict")
    $funasrSpeakerModelName = @("configuration.json", "config.yaml", "campplus_cn_common.bin")
}

function Remove-WorkspacePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $resolvedRoot = (Resolve-Path -LiteralPath $root).Path
    $resolved = Resolve-Path -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($resolved) {
        $target = $resolved.Path
        if (-not $target.StartsWith($resolvedRoot)) {
            throw "Refusing to remove path outside workspace: $target"
        }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

function Resolve-WindowsBuildPython {
    $candidate = $env:VOICE_INPUT_BUILD_PYTHON
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            $candidate = (& $launcher.Source -3.12-64 -c "import sys; print(sys.executable)") | Select-Object -First 1
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($command) {
            $candidate = $command.Source
        }
    }
    if ([string]::IsNullOrWhiteSpace($candidate) -or -not (Test-Path -LiteralPath $candidate)) {
        throw "CPython 3.12 x64 is required. Install it or set VOICE_INPUT_BUILD_PYTHON to python.exe."
    }
    $metadata = & $candidate -c "import json, platform, sys; print(json.dumps({'implementation': platform.python_implementation(), 'major': sys.version_info.major, 'minor': sys.version_info.minor, 'bits': platform.architecture()[0], 'machine': platform.machine()}))"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect Windows build Python: $candidate"
    }
    $runtime = $metadata | ConvertFrom-Json
    if ($runtime.implementation -ne "CPython" -or $runtime.major -ne 3 -or $runtime.minor -ne 12 -or $runtime.bits -ne "64bit" -or $runtime.machine -notmatch "^(AMD64|x86_64)$") {
        throw "Windows builds require CPython 3.12 x64 (AMD64); found $($runtime.implementation) $($runtime.major).$($runtime.minor) $($runtime.bits) $($runtime.machine) at $candidate."
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Initialize-WindowsBuildEnvironment {
    $basePython = Resolve-WindowsBuildPython
    Remove-WorkspacePath $buildEnvRoot
    & $basePython -m venv $buildEnvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create clean Windows build environment: $buildEnvRoot"
    }
    $buildPython = Join-Path $buildEnvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $buildPython)) {
        throw "Clean Windows build environment did not create python.exe: $buildPython"
    }
    return $buildPython
}

function Assert-WebUiDist {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$WriteManifest,
        [switch]$RequireManifest
    )

    $validatorArgs = @($webUiValidator, $Path)
    if ($WriteManifest) {
        $validatorArgs += "--write-manifest"
    }
    if ($RequireManifest) {
        $validatorArgs += "--require-manifest"
    }
    & $python @validatorArgs
    if ($LASTEXITCODE -ne 0) {
        throw "$Label Web UI resource validation failed: $Path"
    }
}

function Assert-FrozenRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$StagePath,
        [Parameter(Mandatory = $true)][string]$ExpectedModelProfile
    )

    $healthExe = Join-Path $StagePath "tools\VoiceInputHealth\VoiceInputHealth.exe"
    if (-not (Test-Path -LiteralPath $healthExe)) {
        throw "Frozen runtime health tool is missing: $healthExe"
    }
    $healthReportPath = Join-Path $buildRoot "health-$ExpectedModelProfile.json"
    Remove-Item -LiteralPath $healthReportPath -Force -ErrorAction SilentlyContinue
    & $healthExe --json --output $healthReportPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $healthReportPath)) {
        throw "Frozen runtime health check failed: $healthExe"
    }
    $report = Get-Content -LiteralPath $healthReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($report.status -eq "error") {
        throw "Frozen runtime health report contains errors: $healthReportPath"
    }
    $runtimeItem = @($report.items) | Where-Object { $_.name -eq "runtime" } | Select-Object -First 1
    $audioItem = @($report.items) | Where-Object { $_.name -eq "audio_io" } | Select-Object -First 1
    $modelItem = @($report.items) | Where-Object { $_.name -eq "streaming_load" } | Select-Object -First 1
    if (-not $runtimeItem -or -not [bool]$runtimeItem.details.frozen) {
        throw "Frozen runtime health report did not confirm a frozen executable."
    }
    if ([string]$runtimeItem.details.model_profile -ne $ExpectedModelProfile) {
        throw "Frozen runtime model profile mismatch: expected $ExpectedModelProfile."
    }
    if (-not $audioItem -or [int]$audioItem.details.resampled_frames -ne 1600) {
        throw "Frozen runtime audio resampling check did not produce 1600 frames."
    }
    if (-not $modelItem -or -not [bool]$modelItem.details.smoke_inference) {
        throw "Frozen runtime model smoke inference did not complete."
    }
    if ($ExpectedModelProfile -eq "full" -and -not [bool]$modelItem.details.speaker_clustering_smoke) {
        throw "Full frozen runtime did not complete the bundled speaker clustering smoke test."
    }
}

function Optimize-WindowsStage {
    param(
        [Parameter(Mandatory = $true)][string]$StagePath,
        [string]$ModelProfile = "full"
    )

    $pruneDirs = @(
        (Join-Path $StagePath "_internal\torch\include"),
        (Join-Path $StagePath "_internal\torch\share"),
        (Join-Path $StagePath "_internal\torch\utils\benchmark"),
        (Join-Path $StagePath "_internal\torch\utils\tensorboard"),
        (Join-Path $StagePath "_internal\aliyunsdkcore"),
        (Join-Path $StagePath "_internal\bcrypt"),
        (Join-Path $StagePath "_internal\Crypto"),
        (Join-Path $StagePath "_internal\cryptography"),
        (Join-Path $StagePath "_internal\google"),
        (Join-Path $StagePath "_internal\greenlet"),
        (Join-Path $StagePath "_internal\hf_xet"),
        (Join-Path $StagePath "_internal\mypy"),
        (Join-Path $StagePath "_internal\pandas"),
        (Join-Path $StagePath "_internal\pandas.libs"),
        (Join-Path $StagePath "_internal\rapidfuzz"),
        (Join-Path $StagePath "_internal\safetensors"),
        (Join-Path $StagePath "_internal\sentencepiece"),
        (Join-Path $StagePath "_internal\sherpa_onnx"),
        (Join-Path $StagePath "_internal\sklearn"),
        (Join-Path $StagePath "_internal\sqlalchemy"),
        (Join-Path $StagePath "_internal\tiktoken"),
        (Join-Path $StagePath "_internal\tokenizers"),
        (Join-Path $StagePath "_internal\transformers"),
        (Join-Path $StagePath "_internal\watchfiles"),
        (Join-Path $StagePath "_internal\jieba\lac_small")
    )
    if ($ModelProfile -eq "streaming") {
        $pruneDirs += @(
            (Join-Path $StagePath "_internal\imageio_ffmpeg")
        )
    }
    foreach ($dir in $pruneDirs) {
        if (Test-Path -LiteralPath $dir) {
            Remove-WorkspacePath $dir
        }
    }

    foreach ($legacyPath in @(
        (Join-Path $StagePath "engines\whispercpp"),
        (Join-Path $StagePath "models\ggml-base.bin"),
        (Join-Path $StagePath "models\NOTICE.txt")
    )) {
        if (Test-Path -LiteralPath $legacyPath) {
            Remove-WorkspacePath $legacyPath
        }
    }

    $qtRoot = Join-Path $StagePath "_internal\PySide6"
    if (Test-Path -LiteralPath $qtRoot) {
        $unusedQtFiles = @(
            "Qt6Pdf.dll",
            "Qt6Svg.dll",
            "Qt6VirtualKeyboard.dll"
        )
        foreach ($fileName in $unusedQtFiles) {
            $filePath = Join-Path $qtRoot $fileName
            if (Test-Path -LiteralPath $filePath) {
                Remove-Item -LiteralPath $filePath -Force
            }
        }

        foreach ($dirName in @(
            "plugins\iconengines",
            "plugins\generic"
        )) {
            $dirPath = Join-Path $qtRoot $dirName
            if (Test-Path -LiteralPath $dirPath) {
                Remove-WorkspacePath $dirPath
            }
        }

        $platformDir = Join-Path $qtRoot "plugins\platforms"
        if (Test-Path -LiteralPath $platformDir) {
            Get-ChildItem -LiteralPath $platformDir -Force -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne "qwindows.dll" } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
        }

        $imageFormatDir = Join-Path $qtRoot "plugins\imageformats"
        if (Test-Path -LiteralPath $imageFormatDir) {
            $keepImagePlugins = @("qico.dll", "qjpeg.dll", "qgif.dll")
            Get-ChildItem -LiteralPath $imageFormatDir -Force -File -ErrorAction SilentlyContinue |
                Where-Object { $keepImagePlugins -notcontains $_.Name } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
        }

        $webEngineResources = Join-Path $qtRoot "resources"
        if (Test-Path -LiteralPath $webEngineResources) {
            Get-ChildItem -LiteralPath $webEngineResources -File -Filter "*.debug.pak" -ErrorAction SilentlyContinue |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
        }

        $webEngineLocales = Join-Path $qtRoot "translations\qtwebengine_locales"
        if (Test-Path -LiteralPath $webEngineLocales) {
            $keepLocales = @("zh-CN.pak", "zh-TW.pak", "en-US.pak")
            Get-ChildItem -LiteralPath $webEngineLocales -File -ErrorAction SilentlyContinue |
                Where-Object { $keepLocales -notcontains $_.Name } |
                ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
        }
    }

    $pruneFilePatterns = @("*.lib", "*.h", "*.hpp", "*.hh", "*.cuh", "*.pxd")
    foreach ($pattern in $pruneFilePatterns) {
        Get-ChildItem -LiteralPath (Join-Path $StagePath "_internal") -Recurse -Force -File -Filter $pattern -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
    }

    foreach ($filePath in @(
        (Join-Path $StagePath "_internal\torch\bin\protoc.exe")
    )) {
        if (Test-Path -LiteralPath $filePath) {
            Remove-Item -LiteralPath $filePath -Force
        }
    }

    Get-ChildItem -LiteralPath $StagePath -Recurse -Force -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-WorkspacePath $_.FullName }

    $funasrModelRoot = Join-Path $StagePath "models\funasr"
    if (Test-Path -LiteralPath $funasrModelRoot) {
        foreach ($dirName in @("example", "fig", ".msc")) {
            Get-ChildItem -LiteralPath $funasrModelRoot -Recurse -Force -Directory -Filter $dirName -ErrorAction SilentlyContinue |
                ForEach-Object { Remove-WorkspacePath $_.FullName }
        }
    }
}

Push-Location $root
try {
    $python = Initialize-WindowsBuildEnvironment
    if (-not (Test-Path -LiteralPath $appIcon)) {
        throw "Missing application icon: $appIcon"
    }
    if (-not (Test-Path -LiteralPath $webUiValidator)) {
        throw "Missing Web UI validation script: $webUiValidator"
    }
    if (-not (Test-Path -LiteralPath $windowsIconValidator)) {
        throw "Missing Windows executable icon validator: $windowsIconValidator"
    }
    if (-not (Test-Path -LiteralPath $windowsBuildLock)) {
        throw "Missing locked Windows build requirements: $windowsBuildLock"
    }
    if (-not (Test-Path -LiteralPath $windowsBuildBootstrapLock)) {
        throw "Missing locked Windows build bootstrap requirements: $windowsBuildBootstrapLock"
    }
    if (-not (Test-Path -LiteralPath $lexiconAssets)) {
        throw "Missing bundled lexicon assets: $lexiconAssets"
    }
    if (-not [string]::IsNullOrWhiteSpace($lexiconCatalogPath) -and -not (Test-Path -LiteralPath $lexiconCatalogPath)) {
        throw "Missing lexicon catalog: $lexiconCatalogPath"
    }
    $lexiconSelectionScript = @'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
product_tier = sys.argv[2].strip().lower()
lexicon_dir = Path(sys.argv[3]).resolve()
catalog_arg = "" if len(sys.argv) < 5 or sys.argv[4] == "__none__" else sys.argv[4].strip()
enabled_arg = "" if len(sys.argv) < 6 or sys.argv[5] == "__none__" else sys.argv[5].strip()
sys.path.insert(0, str(root / "src"))

from voice_input.resources import lexicon_catalog_from_dir

rank = {"basic": 0, "standard": 1, "enterprise": 2}

def tier_allows(tier, minimum):
    return rank.get(tier, rank["standard"]) >= rank.get(str(minimum).strip().lower(), rank["standard"])

catalog_path = Path(catalog_arg).resolve() if catalog_arg else None
catalog = lexicon_catalog_from_dir(
    lexicon_dir,
    catalog_path=catalog_path,
    strict=True,
)
eligible_by_id = {info.id: info for info in catalog if tier_allows(product_tier, info.min_tier)}
if enabled_arg:
    selected_ids = []
    for raw_item in enabled_arg.replace(";", ",").split(","):
        item = raw_item.strip()
        if item and item not in selected_ids:
            selected_ids.append(item)
else:
    selected_ids = [info.id for info in catalog if info.default_enabled and info.id in eligible_by_id]
unknown = [item for item in selected_ids if item not in eligible_by_id]
if unknown:
    raise SystemExit("Unsupported or unavailable bundled lexicon id(s): " + ", ".join(unknown))
selected = [eligible_by_id[item] for item in selected_ids]
files = []
for info in selected:
    path = (lexicon_dir / info.filename).resolve()
    try:
        path.relative_to(lexicon_dir)
    except ValueError as exc:
        raise SystemExit(f"Unsafe bundled lexicon path for {info.id}: {info.filename}") from exc
    if not path.is_file():
        raise SystemExit(f"Missing bundled lexicon for {info.id}: {path}")
    files.append(info.filename)
print(json.dumps({
    "root": str(lexicon_dir),
    "catalog": str(catalog_path) if catalog_path else "",
    "ids": selected_ids,
    "files": files,
}, ensure_ascii=False))
'@
    $lexiconSelectionScriptPath = Join-Path ([System.IO.Path]::GetTempPath()) ("voice-input-lexicon-selection-{0}.py" -f [System.Guid]::NewGuid().ToString("N"))
    $selectionUtf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($lexiconSelectionScriptPath, $lexiconSelectionScript, $selectionUtf8)
    $lexiconCatalogArg = if ([string]::IsNullOrWhiteSpace($lexiconCatalogPath)) { "__none__" } else { $lexiconCatalogPath }
    $enabledLexiconsArg = if ([string]::IsNullOrWhiteSpace($EnabledLexicons)) { "__none__" } else { $EnabledLexicons }
    try {
        $lexiconSelectionJson = & $python $lexiconSelectionScriptPath $root $ProductTier $lexiconAssets $lexiconCatalogArg $enabledLexiconsArg
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to resolve bundled lexicons for product tier '$ProductTier'."
        }
    } finally {
        Remove-Item -LiteralPath $lexiconSelectionScriptPath -Force -ErrorAction SilentlyContinue
    }
    $lexiconSelection = $lexiconSelectionJson | ConvertFrom-Json
    $lexiconAssets = [string]$lexiconSelection.root
    $lexiconCatalogPath = [string]$lexiconSelection.catalog
    $selectedLexiconFiles = @($lexiconSelection.files)
    $enabledLexiconIds = @($lexiconSelection.ids)

    $selectedFunASRModelRoles = $funasrModelProfiles[$ModelProfile]
    $selectedFunASRModelDirNames = @($selectedFunASRModelRoles.Values | Select-Object -Unique)
    $requiredFunASRModelFiles = @()
    foreach ($modelDirName in $selectedFunASRModelDirNames) {
        $modelDir = Join-Path $vendorFunASRModels $modelDirName
        $modelRequiredFiles = $funasrModelRequiredFiles[$modelDirName]
        if (-not $modelRequiredFiles) {
            throw "No resource contract is defined for bundled FunASR model: $modelDirName"
        }
        foreach ($filename in $modelRequiredFiles) {
            $requiredFunASRModelFiles += (Join-Path $modelDir $filename)
        }
    }
    foreach ($required in $requiredFunASRModelFiles) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Missing bundled FunASR model resource: $required. Run scripts\download_funasr_models.ps1 first."
        }
        if ((Get-Item -LiteralPath $required).Length -le 0) {
            throw "Bundled FunASR model resource is empty: $required. Re-run scripts\download_funasr_models.ps1."
        }
    }

    & $webUiBuildScript
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $webUiDist "index.html"))) {
        throw "Web UI build failed or produced no index.html."
    }
    Assert-WebUiDist $webUiDist "Source" -WriteManifest

    & $python -m pip install `
        --disable-pip-version-check `
        --no-cache-dir `
        --no-deps `
        --require-hashes `
        --only-binary=:all: `
        --requirement $windowsBuildBootstrapLock
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install hashed Windows build bootstrap requirements: $windowsBuildBootstrapLock"
    }
    $sourceOnlyPackages = "aliyun-python-sdk-core,antlr4-python3-runtime,crcmod,jieba,oss2"
    & $python -m pip install `
        --disable-pip-version-check `
        --no-cache-dir `
        --no-deps `
        --require-hashes `
        --only-binary=:all: `
        --no-binary=$sourceOnlyPackages `
        --no-build-isolation `
        --requirement $windowsBuildLock
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install the hashed Windows build lock. Verify CPython 3.12 x64, wheel availability, and every --hash entry in $windowsBuildLock."
    }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Locked Windows build requirements are incomplete or incompatible."
    }
    & $python -c "import soundfile as sf; assert 'WAV' in sf.available_formats(); print('SoundFile runtime:', sf.__version__, sf.__libsndfile_version__)"
    if ($LASTEXITCODE -ne 0) {
        throw "The locked SoundFile wheel cannot load its bundled libsndfile runtime."
    }

    Remove-WorkspacePath $buildRoot
    New-Item -ItemType Directory -Force -Path $pyiDist, $pyiWork, $pyiSpec, $appStage, $installerStage, $outputDir, $profileSrc | Out-Null
    $env:VOICE_INPUT_PROXY_FUNASR = (Join-Path $root "src\funasr")
    $funasrSourcePackageOutput = @'
import os
from pathlib import Path
import sys

proxy = Path(os.environ["VOICE_INPUT_PROXY_FUNASR"]).resolve()
for entry in sys.path:
    candidate = (Path(entry or ".") / "funasr").resolve()
    if candidate == proxy:
        continue
    if (candidate / "auto").is_dir() and (candidate / "models").is_dir():
        print(candidate)
        raise SystemExit(0)
raise SystemExit("Unable to locate upstream FunASR package source.")
'@ | & $python -
    Remove-Item Env:\VOICE_INPUT_PROXY_FUNASR -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to locate upstream FunASR package source."
    }
    $funasrSourcePackage = ($funasrSourcePackageOutput | Select-Object -First 1).Trim()
    if ([string]::IsNullOrWhiteSpace($funasrSourcePackage) -or -not (Test-Path -LiteralPath $funasrSourcePackage)) {
        throw "Unable to locate upstream FunASR package source."
    }
    Copy-Item -LiteralPath $funasrSourcePackage -Destination $profileSrc -Recurse -Force
    $profileFunASR = Join-Path $profileSrc "funasr"
    Copy-Item -LiteralPath (Join-Path $root "src\funasr\__init__.py") -Destination (Join-Path $profileFunASR "__init__.py") -Force
    Copy-Item -LiteralPath (Join-Path $funasrPatchDir "download_model_from_hub.py") -Destination (Join-Path $profileFunASR "download\download_model_from_hub.py") -Force
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $loadUtilsPath = Join-Path $profileFunASR "utils\load_utils.py"
    $loadUtilsText = [System.IO.File]::ReadAllText($loadUtilsPath)
    $loadUtilsText = [regex]::Replace($loadUtilsText, "(?m)^import librosa\r?\n", "")
    [System.IO.File]::WriteAllText($loadUtilsPath, $loadUtilsText, $utf8NoBom)
    $wavFrontendPath = Join-Path $profileFunASR "frontends\wav_frontend.py"
    $wavFrontendText = [System.IO.File]::ReadAllText($wavFrontendPath)
    $wavFrontendText = $wavFrontendText.Replace("import funasr.frontends.eend_ola_feature as eend_ola_feature", "eend_ola_feature = None")
    [System.IO.File]::WriteAllText($wavFrontendPath, $wavFrontendText, $utf8NoBom)
    $campplusUtilsPath = Join-Path $profileFunASR "models\campplus\utils.py"
    $campplusUtilsText = [System.IO.File]::ReadAllText($campplusUtilsPath)
    $campplusUtilsText = $campplusUtilsText.Replace("import librosa as sf", "import soundfile as sf")
    $campplusUtilsText = $campplusUtilsText.Replace("sf.load(", "sf.read(")
    [System.IO.File]::WriteAllText($campplusUtilsPath, $campplusUtilsText, $utf8NoBom)
    Copy-Item -LiteralPath (Join-Path $funasrPatchDir "cluster_backend.py") -Destination (Join-Path $profileFunASR "models\campplus\cluster_backend.py") -Force
    New-Item -ItemType Directory -Force -Path $profileLexicons | Out-Null
    if (-not [string]::IsNullOrWhiteSpace($lexiconCatalogPath)) {
        Copy-Item -LiteralPath $lexiconCatalogPath -Destination (Join-Path $profileLexicons "catalog.json") -Force
    }
    foreach ($lexiconFile in $selectedLexiconFiles) {
        $destination = Join-Path $profileLexicons $lexiconFile
        $destinationDir = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationDir)) {
            New-Item -ItemType Directory -Force -Path $destinationDir | Out-Null
        }
        Copy-Item -LiteralPath (Join-Path $lexiconAssets $lexiconFile) -Destination $destination -Force
    }
    [ordered]@{
        product_name = "VoiceInputAssistant"
        product_tier = $ProductTier
        model_profile = $ModelProfile
        enabled_builtin_lexicons = $enabledLexiconIds
        bundled_funasr_models = $selectedFunASRModelDirNames
        funasr_models = $selectedFunASRModelRoles
        funasr_model_files = [ordered]@{}
        funasr_capabilities = [ordered]@{
            streaming = [bool]$selectedFunASRModelRoles.Contains("streaming")
            offline_review = [bool]$selectedFunASRModelRoles.Contains("offline")
            vad = [bool]$selectedFunASRModelRoles.Contains("vad")
            punctuation = [bool]$selectedFunASRModelRoles.Contains("punc")
            speaker = [bool]$selectedFunASRModelRoles.Contains("speaker")
            background_review = [bool]($selectedFunASRModelRoles.Contains("offline") -and $selectedFunASRModelRoles.Contains("vad"))
        }
    } | ForEach-Object {
        foreach ($modelDirName in $selectedFunASRModelDirNames) {
            $_.funasr_model_files[$modelDirName] = @($funasrModelRequiredFiles[$modelDirName])
        }
        $_
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $productManifest -Encoding UTF8

    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name VoiceInputAssistant `
        --icon $appIcon `
        --paths $profileSrc `
        --paths (Join-Path $root "src") `
        --exclude-module tkinter `
        --exclude-module _tkinter `
        --exclude-module tcl `
        --exclude-module tk `
        --exclude-module IPython `
        --exclude-module jupyter `
        --exclude-module notebook `
        --exclude-module pytest `
        --exclude-module _pytest `
        --exclude-module py `
        --exclude-module matplotlib `
        --exclude-module pandas `
        --exclude-module pandas.libs `
        --exclude-module sklearn `
        --exclude-module sqlalchemy `
        --exclude-module scipy `
        --exclude-module fastapi `
        --exclude-module starlette `
        --exclude-module uvicorn `
        --exclude-module websockets `
        --exclude-module watchfiles `
        --exclude-module modelscope `
        --exclude-module tensorboard `
        --exclude-module torch.utils.tensorboard `
        --exclude-module torch.utils.benchmark `
        --exclude-module funasr.bin `
        --exclude-module funasr.datasets `
        --exclude-module huggingface_hub `
        --exclude-module librosa `
        --exclude-module numba `
        --exclude-module llvmlite `
        --exclude-module umap `
        --exclude-module pynndescent `
        --exclude-module pydantic `
        --exclude-module modelscope.cli `
        --exclude-module modelscope.msdatasets `
        --exclude-module modelscope.server `
        --exclude-module modelscope.trainers `
        --exclude-module onnx `
        --exclude-module onnxruntime `
        --exclude-module transformers `
        --exclude-module tokenizers `
        --exclude-module safetensors `
        --hidden-import voice_input.diagnose_asr `
        --hidden-import voice_input.accuracy_eval `
        --hidden-import voice_input.streaming_accuracy_eval `
        --hidden-import voice_input.healthcheck `
        --hidden-import voice_input.funasr_streaming_worker `
        --hidden-import voice_input.funasr_offline_worker `
        --hidden-import PySide6.QtCore `
        --hidden-import PySide6.QtGui `
        --hidden-import PySide6.QtWidgets `
        --hidden-import PySide6.QtPrintSupport `
        --hidden-import PySide6.QtWebChannel `
        --hidden-import PySide6.QtWebEngineCore `
        --hidden-import PySide6.QtWebEngineWidgets `
        --hidden-import imageio_ffmpeg `
        --collect-binaries imageio_ffmpeg `
        --hidden-import opencc `
        --collect-data opencc `
        --hidden-import sounddevice `
        --hidden-import numpy `
        --hidden-import funasr `
        --hidden-import funasr.auto.auto_model `
        --hidden-import funasr.auto.auto_frontend `
        --hidden-import funasr.download.download_model_from_hub `
        --hidden-import funasr.download.file `
        --hidden-import funasr.frontends.wav_frontend `
        --hidden-import funasr.models.paraformer_streaming.model `
        --hidden-import funasr.models.seaco_paraformer.model `
        --hidden-import funasr.models.fsmn_vad_streaming.model `
        --hidden-import funasr.models.ct_transformer.model `
        --hidden-import funasr.models.campplus.model `
        --hidden-import funasr.models.campplus.components `
        --hidden-import funasr.models.campplus.utils `
        --hidden-import funasr.models.campplus.cluster_backend `
        --hidden-import funasr.models.scama.encoder `
        --hidden-import funasr.models.sanm.encoder `
        --hidden-import funasr.models.fsmn_vad_streaming.encoder `
        --hidden-import funasr.models.e_paraformer.decoder `
        --hidden-import funasr.models.paraformer.cif_predictor `
        --hidden-import funasr.models.bicif_paraformer.cif_predictor `
        --hidden-import funasr.models.specaug.specaug `
        --hidden-import funasr.tokenizer.char_tokenizer `
        --hidden-import funasr.utils.load_utils `
        --hidden-import funasr.utils.vad_utils `
        --hidden-import funasr.utils.timestamp_tools `
        --hidden-import torch `
        --collect-binaries torch `
        --hidden-import torchaudio `
        --collect-binaries torchaudio `
        --hidden-import soxr `
        --collect-binaries soxr `
        --hidden-import soundfile `
        --hidden-import _soundfile_data `
        --collect-all _soundfile_data `
        --hidden-import pystray `
        --hidden-import PIL `
        --add-data "$appIcon;assets\icons" `
        --add-data "$profileLexicons;assets\lexicons" `
        --add-data "$productManifest;assets" `
        --add-data "$webUiDist;voice_input\web_dist" `
        --distpath $pyiDist `
        --workpath $pyiWork `
        --specpath $pyiSpec `
        (Join-Path $root "packaging\windows\voice_input_gui.py")

    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --name VoiceInputToolLauncher `
        --icon $appIcon `
        --exclude-module tkinter `
        --exclude-module _tkinter `
        --exclude-module tcl `
        --exclude-module tk `
        --distpath $pyiDist `
        --workpath $pyiWork `
        --specpath $pyiSpec `
        $toolLauncherScript

    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name Uninstall `
        --icon $appIcon `
        --distpath $pyiDist `
        --workpath $pyiWork `
        --specpath $pyiSpec `
        (Join-Path $root "packaging\windows\uninstall.py")

    Copy-Item -Path (Join-Path $pyiDist "VoiceInputAssistant\*") -Destination $appStage -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $pyiDist "Uninstall.exe") -Destination (Join-Path $appStage "Uninstall.exe") -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $appStage "assets\icons") | Out-Null
    Copy-Item -LiteralPath $appIcon -Destination (Join-Path $appStage "assets\icons\app.ico") -Force
    New-Item -ItemType Directory -Force -Path (Join-Path $appStage "assets") | Out-Null
    Copy-Item -Path $profileLexicons -Destination (Join-Path $appStage "assets") -Recurse -Force
    Copy-Item -LiteralPath $productManifest -Destination (Join-Path $appStage "assets\product.json") -Force
    $toolLauncherExe = Join-Path $pyiDist "VoiceInputToolLauncher.exe"
    foreach ($toolName in @("VoiceInputDiagnose", "VoiceInputEvaluate", "VoiceInputStreamEvaluate", "VoiceInputHealth")) {
        $toolDir = Join-Path (Join-Path $appStage "tools") $toolName
        New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
        Copy-Item -LiteralPath $toolLauncherExe -Destination (Join-Path $toolDir "$toolName.exe") -Force
    }

    $modelStage = Join-Path $appStage "models"
    New-Item -ItemType Directory -Force -Path $modelStage | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $modelStage "funasr") | Out-Null
    foreach ($modelDirName in $selectedFunASRModelDirNames) {
        Copy-Item -LiteralPath (Join-Path $vendorFunASRModels $modelDirName) -Destination (Join-Path $modelStage "funasr") -Recurse -Force
    }
    @"
This package includes the FunASR models selected by the build profile.

Model profile: $ModelProfile

Streaming recognition uses the online Paraformer model for low latency.
Packages that include offline, VAD and punctuation models also support final
review, imported audio and long-session background review.
"@ | Set-Content -LiteralPath (Join-Path $appStage "models\README.txt") -Encoding UTF8

    Copy-Item -LiteralPath (Join-Path $root "README.md") -Destination (Join-Path $appStage "README.md") -Force
    Copy-Item -Path (Join-Path $root "docs") -Destination (Join-Path $appStage "docs") -Recurse -Force

    Optimize-WindowsStage $appStage $ModelProfile
    Assert-WebUiDist (Join-Path $appStage "_internal\voice_input\web_dist") "Packaged" -RequireManifest
    Assert-FrozenRuntime $appStage $ModelProfile

    $env:VOICE_INPUT_PAYLOAD_ROOT = $appStage
    $env:VOICE_INPUT_PAYLOAD_MANIFEST = (Join-Path $appStage "assets\payload-manifest.json")
    try {
        @'
from pathlib import Path
import hashlib
import json
import os

root = Path(os.environ["VOICE_INPUT_PAYLOAD_ROOT"]).resolve()
manifest = Path(os.environ["VOICE_INPUT_PAYLOAD_MANIFEST"]).resolve()
files = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path == manifest:
        continue
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    files.append({
        "path": path.relative_to(root).as_posix(),
        "size": size,
        "sha256": digest.hexdigest(),
    })
manifest.parent.mkdir(parents=True, exist_ok=True)
manifest.write_text(
    json.dumps({"schema_version": 1, "algorithm": "sha256", "files": files}, indent=2) + "\n",
    encoding="utf-8",
)
'@ | & $python -
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to generate payload integrity manifest."
        }
    } finally {
        Remove-Item Env:\VOICE_INPUT_PAYLOAD_ROOT -ErrorAction SilentlyContinue
        Remove-Item Env:\VOICE_INPUT_PAYLOAD_MANIFEST -ErrorAction SilentlyContinue
    }

    $env:VOICE_INPUT_PORTABLE_SOURCE = $appStage
    $env:VOICE_INPUT_PORTABLE_ZIP = $portableZip
    @'
from pathlib import Path
import os
import zipfile

source = Path(os.environ["VOICE_INPUT_PORTABLE_SOURCE"])
destination = Path(os.environ["VOICE_INPUT_PORTABLE_ZIP"])
if destination.exists():
    destination.unlink()
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source).as_posix())
'@ | & $python -
    Remove-Item Env:\VOICE_INPUT_PORTABLE_SOURCE -ErrorAction SilentlyContinue
    Remove-Item Env:\VOICE_INPUT_PORTABLE_ZIP -ErrorAction SilentlyContinue

    $env:VOICE_INPUT_PORTABLE_ZIP = $portableZip
    $env:VOICE_INPUT_SETUP_BOOTSTRAP = (Join-Path $root "packaging\windows\setup_bootstrap.py")
    try {
        @'
from pathlib import Path
import importlib.util
import os

bootstrap_path = Path(os.environ["VOICE_INPUT_SETUP_BOOTSTRAP"])
spec = importlib.util.spec_from_file_location("voice_input_setup_bootstrap_build_check", bootstrap_path)
if spec is None or spec.loader is None:
    raise SystemExit(f"Unable to load installer bootstrap: {bootstrap_path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._validate_payload(Path(os.environ["VOICE_INPUT_PORTABLE_ZIP"]))
'@ | & $python -
        if ($LASTEXITCODE -ne 0) {
            throw "Portable payload validation failed: $portableZip"
        }
    } finally {
        Remove-Item Env:\VOICE_INPUT_PORTABLE_ZIP -ErrorAction SilentlyContinue
        Remove-Item Env:\VOICE_INPUT_SETUP_BOOTSTRAP -ErrorAction SilentlyContinue
    }

    $zipPath = Join-Path $installerStage "VoiceInputAssistant.zip"
    Copy-Item -LiteralPath $portableZip -Destination $zipPath -Force
    if (Test-Path -LiteralPath $setupBundle) {
        Remove-WorkspacePath $setupBundle
    }
    if ($pyinstallerSetupBundle -ne $setupBundle -and (Test-Path -LiteralPath $pyinstallerSetupBundle)) {
        Remove-WorkspacePath $pyinstallerSetupBundle
    }
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --name VoiceInputAssistantSetup `
        --icon $appIcon `
        --hidden-import PySide6.QtCore `
        --hidden-import PySide6.QtGui `
        --hidden-import PySide6.QtWidgets `
        --exclude-module tkinter `
        --exclude-module _tkinter `
        --exclude-module tcl `
        --exclude-module tk `
        --add-data "$zipPath;." `
        --add-data "$appIcon;." `
        --distpath $outputDir `
        --workpath $pyiWork `
        --specpath $pyiSpec `
        (Join-Path $root "packaging\windows\setup_bootstrap.py")

    if ($pyinstallerSetupBundle -ne $setupBundle) {
        if (-not (Test-Path -LiteralPath $pyinstallerSetupBundle)) {
            throw "PyInstaller setup bundle was not created: $pyinstallerSetupBundle"
        }
        Move-Item -LiteralPath $pyinstallerSetupBundle -Destination $setupBundle -Force
    }

    $unversionedSetupExe = Join-Path $setupBundle "VoiceInputAssistantSetup.exe"
    if (-not (Test-Path -LiteralPath $unversionedSetupExe)) {
        throw "PyInstaller setup executable was not created: $unversionedSetupExe"
    }
    Move-Item -LiteralPath $unversionedSetupExe -Destination $setupExe -Force
    & $python $windowsIconValidator $appIcon $setupExe
    if ($LASTEXITCODE -ne 0) {
        throw "Frozen installer icon validation failed: $setupExe"
    }

    Optimize-WindowsStage $setupBundle $ModelProfile
    $setupCheck = Start-Process `
        -FilePath $setupExe `
        -ArgumentList @("--check-runtime", "`"$appStage`"") `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($setupCheck.ExitCode -ne 0) {
        throw "Frozen installer runtime self-check failed: $setupExe"
    }

    $env:VOICE_INPUT_INSTALLER_SOURCE = $setupBundle
    $env:VOICE_INPUT_INSTALLER_ZIP = $installerZip
    @'
from pathlib import Path
import os
import zipfile

source = Path(os.environ["VOICE_INPUT_INSTALLER_SOURCE"])
destination = Path(os.environ["VOICE_INPUT_INSTALLER_ZIP"])
if destination.exists():
    destination.unlink()
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(source).as_posix())
'@ | & $python -
    Remove-Item Env:\VOICE_INPUT_INSTALLER_SOURCE -ErrorAction SilentlyContinue
    Remove-Item Env:\VOICE_INPUT_INSTALLER_ZIP -ErrorAction SilentlyContinue

    Write-Host "Windows app directory: $appStage"
    Write-Host "Windows model profile: $ModelProfile"
    Write-Host "Windows portable zip: $portableZip"
    Write-Host "Windows installer zip: $installerZip"
    if (Test-Path -LiteralPath $setupExe) {
        Write-Host "Windows installer exe: $setupExe"
    }
}
finally {
    Pop-Location
}
