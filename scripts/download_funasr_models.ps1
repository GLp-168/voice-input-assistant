$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}

Push-Location $root
try {
    & $python -m pip install -U "modelscope>=1.37.0"
    @'
from __future__ import annotations

from pathlib import Path
import shutil

from modelscope import snapshot_download


ROOT = Path.cwd()
TARGET_ROOT = ROOT / "vendor" / "funasr" / "models"
MODELS = (
    ("iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online", "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"),
    ("iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch", "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"),
    ("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "speech_fsmn_vad_zh-cn-16k-common-pytorch"),
    ("damo/punc_ct-transformer_zh-cn-common-vocab272727-pytorch", "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"),
    ("iic/punc_ct-transformer_cn-en-common-vocab471067-large", "punc_ct-transformer_cn-en-common-vocab471067-large"),
)
REQUIRED_FILES = ("configuration.json", "model.pt")


def has_required_files(path: Path) -> bool:
    for name in REQUIRED_FILES:
        candidate = path / name
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return False
    return True


def copy_snapshot(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    ignore = shutil.ignore_patterns(".git", ".cache", "__pycache__")
    shutil.copytree(source, destination, ignore=ignore)


def main() -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    for model_id, dirname in MODELS:
        destination = TARGET_ROOT / dirname
        if has_required_files(destination):
            print(f"already present: {destination}")
            continue
        if destination.exists():
            print(f"removing incomplete model directory: {destination}")
            shutil.rmtree(destination)
        print(f"downloading {model_id}")
        snapshot = Path(snapshot_download(model_id, cache_dir=str(ROOT / "tmp" / "modelscope")))
        if not has_required_files(snapshot):
            missing = ", ".join(name for name in REQUIRED_FILES if not (snapshot / name).is_file() or (snapshot / name).stat().st_size <= 0)
            raise RuntimeError(f"downloaded model snapshot is incomplete: {model_id} ({missing})")
        copy_snapshot(snapshot, destination)
        if not has_required_files(destination):
            raise RuntimeError(f"downloaded model is incomplete: {destination}")
        print(f"ready: {destination}")


if __name__ == "__main__":
    main()
'@ | & $python -
}
finally {
    Pop-Location
}
