from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest


_VALIDATOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_web_dist.py"
_VALIDATOR_SPEC = importlib.util.spec_from_file_location("voice_input_validate_web_dist", _VALIDATOR_PATH)
assert _VALIDATOR_SPEC is not None
_VALIDATOR = importlib.util.module_from_spec(_VALIDATOR_SPEC)
assert _VALIDATOR_SPEC.loader is not None
_VALIDATOR_SPEC.loader.exec_module(_VALIDATOR)
validate_web_dist = _VALIDATOR.validate_web_dist
write_web_manifest = _VALIDATOR.write_web_manifest


def _write_dist(root: Path, *, script: bytes = b"console.log('ok')", stylesheet: bytes = b"body{}") -> None:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text(
        """
        <!doctype html>
        <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
        <script type="module" src="./assets/app.js?v=1"></script>
        <link rel="stylesheet" href="./assets/app.css#theme">
        """,
        encoding="utf-8",
    )
    (assets / "app.js").write_bytes(script)
    (assets / "app.css").write_bytes(stylesheet)


def test_validate_web_dist_accepts_nonempty_local_script_and_stylesheet(tmp_path: Path) -> None:
    _write_dist(tmp_path)

    assets = validate_web_dist(tmp_path)

    assert {path.name for path in assets} == {"app.js", "app.css"}


@pytest.mark.parametrize(
    ("missing_name", "message"),
    (("app.js", "script asset is missing"), ("app.css", "stylesheet asset is missing")),
)
def test_validate_web_dist_rejects_missing_referenced_asset(
    tmp_path: Path, missing_name: str, message: str
) -> None:
    _write_dist(tmp_path)
    (tmp_path / "assets" / missing_name).unlink()

    with pytest.raises(ValueError, match=message):
        validate_web_dist(tmp_path)


def test_validate_web_dist_rejects_empty_referenced_asset(tmp_path: Path) -> None:
    _write_dist(tmp_path, script=b"")

    with pytest.raises(ValueError, match="script asset is empty"):
        validate_web_dist(tmp_path)


@pytest.mark.parametrize("index_content", (None, b""))
def test_validate_web_dist_requires_nonempty_index(tmp_path: Path, index_content: bytes | None) -> None:
    tmp_path.mkdir(exist_ok=True)
    if index_content is not None:
        (tmp_path / "index.html").write_bytes(index_content)

    with pytest.raises(ValueError, match="entry point is (missing|empty)"):
        validate_web_dist(tmp_path)


def test_validate_web_dist_rejects_path_traversal(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<script src="../outside.js"></script><link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    (tmp_path / "outside.js").write_bytes(b"outside")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.css").write_bytes(b"body{}")

    with pytest.raises(ValueError, match="escapes Web UI directory"):
        validate_web_dist(dist)


def test_web_integrity_manifest_detects_changes_and_unlisted_files(tmp_path: Path) -> None:
    _write_dist(tmp_path)
    write_web_manifest(tmp_path)

    validate_web_dist(tmp_path, require_manifest=True)
    (tmp_path / "assets" / "app.js").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="(size|SHA-256) mismatch"):
        validate_web_dist(tmp_path, require_manifest=True)

    write_web_manifest(tmp_path)
    (tmp_path / "assets" / "extra.js").write_bytes(b"extra")
    with pytest.raises(ValueError, match="omits file"):
        validate_web_dist(tmp_path, require_manifest=True)


def test_windows_build_lock_contains_only_exact_requirements() -> None:
    lock_path = Path(__file__).resolve().parents[1] / "requirements" / "windows-build.lock.txt"
    requirements = [
        line.strip()
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert requirements
    assert all(line.count("==") == 1 for line in requirements)
    assert any(line.startswith("soundfile==0.13.1 ") for line in requirements)
    assert any(line.startswith("soxr==1.1.0 ") for line in requirements)
    assert all("--hash=sha256:" in line for line in requirements)
    assert any(line.lower().startswith("pyinstaller==") for line in requirements)
    assert any(line.lower().startswith("pyside6==") for line in requirements)
    assert any(line.lower().startswith("torch==") for line in requirements)
    assert any(line.lower().startswith("wheel==") for line in requirements)

    bootstrap_lock = lock_path.with_name("windows-build-bootstrap.lock.txt")
    bootstrap_requirements = [
        line.strip()
        for line in bootstrap_lock.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert {line.split("==", 1)[0].lower() for line in bootstrap_requirements} == {
        "setuptools",
        "wheel",
    }
    assert all("--hash=sha256:" in line for line in bootstrap_requirements)


def test_windows_build_uses_lock_and_validates_source_and_packaged_web_ui() -> None:
    build_script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"
    ).read_text(encoding="utf-8")

    assert "pip install -U" not in build_script
    assert "--require-hashes" in build_script
    assert "--only-binary=:all:" in build_script
    assert "sf.available_formats()" in build_script
    assert "--hidden-import soxr" in build_script
    assert "--hidden-import voice_input.funasr_offline_worker" in build_script
    assert "--collect-binaries soxr" in build_script
    assert "--exclude-module scipy" in build_script
    assert "--collect-all _soundfile_data" in build_script
    assert "Initialize-WindowsBuildEnvironment" in build_script
    assert 'Assert-WebUiDist $webUiDist "Source" -WriteManifest' in build_script
    assert 'Assert-WebUiDist (Join-Path $appStage "_internal\\voice_input\\web_dist") "Packaged" -RequireManifest' in build_script
    assert "Assert-FrozenRuntime $appStage $ModelProfile" in build_script
    assert "Start-Process" in build_script
    assert '"--check-runtime"' in build_script
    assert "-Wait" in build_script
    assert "$setupCheck.ExitCode" in build_script
    assert 'VoiceInputAssistant-v$productVersion-$setupProfileName-Setup.exe' in build_script
    assert "$windowsIconValidator" in build_script
    assert "Frozen installer icon validation failed" in build_script
    assert "payload-manifest.json" in build_script


def test_windows_build_exposes_streaming_and_full_model_profiles() -> None:
    build_script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"
    ).read_text(encoding="utf-8")

    assert '[string]$ModelProfile = ""' in build_script
    assert '$validModelProfiles = @("streaming", "full")' in build_script
    assert 'model_profile = $ModelProfile' in build_script
    assert 'Optimize-WindowsStage $appStage $ModelProfile' in build_script
    assert 'speaker = $funasrSpeakerModelName' in build_script
    assert 'speaker = [bool]$selectedFunASRModelRoles.Contains("speaker")' in build_script
    assert 'cluster_backend.py' in build_script
    assert '$campplusUtilsText.Replace("sf.load(", "sf.read(")' in build_script
    assert 'speaker_clustering_smoke' in build_script
    assert 'sv_chunk = postprocess = distribute_spk = None' not in build_script


def test_source_distribution_excludes_webui_dependencies() -> None:
    manifest = (
        Path(__file__).resolve().parents[1] / "MANIFEST.in"
    ).read_text(encoding="utf-8")

    assert "prune webui/node_modules" in manifest
