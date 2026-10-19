from __future__ import annotations

import hashlib
import json
from pathlib import Path
import importlib.util
import subprocess
import zipfile

import pytest


REQUIRED_PAYLOAD_FILES = (
    "VoiceInputAssistant.exe",
    "Uninstall.exe",
    "tools/VoiceInputDiagnose/VoiceInputDiagnose.exe",
    "tools/VoiceInputEvaluate/VoiceInputEvaluate.exe",
    "tools/VoiceInputStreamEvaluate/VoiceInputStreamEvaluate.exe",
    "tools/VoiceInputHealth/VoiceInputHealth.exe",
    "assets/product.json",
    "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/configuration.json",
    "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/config.yaml",
    "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/model.pt",
    "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/tokens.json",
    "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/seg_dict",
    "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/am.mvn",
    "models/funasr/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/configuration.json",
    "models/funasr/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/config.yaml",
    "models/funasr/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/model.pt",
    "models/funasr/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/tokens.json",
    "models/funasr/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/seg_dict",
    "models/funasr/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/am.mvn",
    "models/funasr/speech_fsmn_vad_zh-cn-16k-common-pytorch/configuration.json",
    "models/funasr/speech_fsmn_vad_zh-cn-16k-common-pytorch/config.yaml",
    "models/funasr/speech_fsmn_vad_zh-cn-16k-common-pytorch/model.pt",
    "models/funasr/speech_fsmn_vad_zh-cn-16k-common-pytorch/am.mvn",
    "models/funasr/punc_ct-transformer_cn-en-common-vocab471067-large/configuration.json",
    "models/funasr/punc_ct-transformer_cn-en-common-vocab471067-large/config.yaml",
    "models/funasr/punc_ct-transformer_cn-en-common-vocab471067-large/model.pt",
    "models/funasr/punc_ct-transformer_cn-en-common-vocab471067-large/tokens.json",
    "models/funasr/punc_ct-transformer_cn-en-common-vocab471067-large/jieba_usr_dict",
    "models/funasr/speech_campplus_sv_zh-cn_16k-common/configuration.json",
    "models/funasr/speech_campplus_sv_zh-cn_16k-common/config.yaml",
    "models/funasr/speech_campplus_sv_zh-cn_16k-common/campplus_cn_common.bin",
    "assets/lexicons/government_digital.json",
    "assets/lexicons/official_document.json",
    "assets/lexicons/office_meeting.json",
    "assets/lexicons/finance_budget.json",
    "assets/lexicons/project_engineering.json",
    "docs/使用帮助.md",
    "docs/验收清单.md",
    "README.md",
    "_internal/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
)

_BOOTSTRAP_PATH = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "setup_bootstrap.py"
_BOOTSTRAP_SPEC = importlib.util.spec_from_file_location("voice_input_setup_bootstrap", _BOOTSTRAP_PATH)
assert _BOOTSTRAP_SPEC is not None
_BOOTSTRAP = importlib.util.module_from_spec(_BOOTSTRAP_SPEC)
assert _BOOTSTRAP_SPEC.loader is not None
_BOOTSTRAP_SPEC.loader.exec_module(_BOOTSTRAP)
_validate_payload = _BOOTSTRAP._validate_payload
_extract_payload = _BOOTSTRAP._extract_payload
_extract_payload_for_install = _BOOTSTRAP._extract_payload_for_install
_run_post_install_health_check = _BOOTSTRAP._run_post_install_health_check
InstallerWizard = _BOOTSTRAP.InstallerWizard


def _write_payload(
    path: Path,
    names: tuple[str, ...],
    *,
    product_tier: str = "enterprise",
    product_manifest: bytes | None = None,
    lexicon_catalog: bytes | None = None,
    content_overrides: dict[str, bytes] | None = None,
) -> None:
    entries: dict[str, bytes] = {}
    overrides = content_overrides or {}
    for name in names:
        if name in overrides:
            entries[name] = overrides[name]
        elif name == "assets/product.json":
            entries[name] = product_manifest or f'{{"product_tier":"{product_tier}"}}'.encode("utf-8")
        elif name == "assets/lexicons/catalog.json":
            entries[name] = lexicon_catalog or _DEFAULT_LEXICON_CATALOG
        else:
            entries[name] = b"x"
    manifest = {
        "schema_version": 1,
        "algorithm": "sha256",
        "files": [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(entries.items())
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        archive.writestr("assets/payload-manifest.json", json.dumps(manifest).encode("utf-8"))


_DEFAULT_LEXICON_CATALOG = b"""
{
  "schema_version": 1,
  "lexicons": [
    {"id": "government_digital", "filename": "government_digital.json", "default_enabled": true, "min_tier": "basic"},
    {"id": "official_document", "filename": "official_document.json", "default_enabled": true, "min_tier": "basic"},
    {"id": "office_meeting", "filename": "office_meeting.json", "default_enabled": true, "min_tier": "basic"},
    {"id": "finance_budget", "filename": "finance_budget.json", "default_enabled": true, "min_tier": "basic"},
    {"id": "project_engineering", "filename": "project_engineering.json", "default_enabled": true, "min_tier": "basic"}
  ]
}
"""


def test_validate_payload_requires_runtime_model_lexicons_docs_and_ffmpeg(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    _write_payload(payload, REQUIRED_PAYLOAD_FILES)

    _validate_payload(payload)


def test_validate_payload_requires_all_builtin_lexicons_for_basic_tier(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    files = tuple(
        name
        for name in REQUIRED_PAYLOAD_FILES
        if name
        not in {
            "assets/lexicons/official_document.json",
            "assets/lexicons/office_meeting.json",
            "assets/lexicons/finance_budget.json",
            "assets/lexicons/project_engineering.json",
        }
    )
    _write_payload(payload, files, product_tier="basic")

    with pytest.raises(RuntimeError, match="official document lexicon"):
        _validate_payload(payload)


def test_validate_payload_uses_packaged_lexicon_catalog(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    catalog = b"""
    {
      "schema_version": 1,
      "lexicons": [
        {
          "id": "client_government",
          "filename": "clients/client_government.csv",
          "default_enabled": true,
          "min_tier": "standard"
        }
      ]
    }
    """
    manifest = b"""
    {
      "product_tier": "standard",
      "enabled_builtin_lexicons": ["client_government"]
    }
    """
    files = (
        *REQUIRED_PAYLOAD_FILES,
        "assets/lexicons/catalog.json",
        "assets/lexicons/clients/client_government.csv",
    )
    _write_payload(payload, files, product_tier="standard", product_manifest=manifest, lexicon_catalog=catalog)

    _validate_payload(payload)

    missing_payload = tmp_path / "VoiceInputAssistant-missing.zip"
    _write_payload(
        missing_payload,
        tuple(name for name in files if name != "assets/lexicons/clients/client_government.csv"),
        product_tier="standard",
        product_manifest=manifest,
        lexicon_catalog=catalog,
    )
    with pytest.raises(RuntimeError, match="client_government"):
        _validate_payload(missing_payload)


def test_validate_payload_accepts_basic_tier_with_streaming_model_only(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    files = tuple(
        name
        for name in REQUIRED_PAYLOAD_FILES
        if "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch" not in name
        and "speech_fsmn_vad_zh-cn-16k-common-pytorch" not in name
        and "punc_ct-transformer_cn-en-common-vocab471067-large" not in name
        and "speech_campplus_sv_zh-cn_16k-common" not in name
        and "imageio_ffmpeg/binaries" not in name
    )
    manifest = b"""
    {
      "product_tier": "basic",
      "model_profile": "streaming",
      "bundled_funasr_models": [
        "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
      ],
      "funasr_models": {
        "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
      }
    }
    """
    _write_payload(payload, files, product_tier="basic", product_manifest=manifest)

    _validate_payload(payload)


def test_validate_payload_accepts_enterprise_streaming_profile_without_ffmpeg(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    files = tuple(
        name
        for name in REQUIRED_PAYLOAD_FILES
        if "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch" not in name
        and "speech_fsmn_vad_zh-cn-16k-common-pytorch" not in name
        and "punc_ct-transformer_cn-en-common-vocab471067-large" not in name
        and "speech_campplus_sv_zh-cn_16k-common" not in name
        and "imageio_ffmpeg/binaries" not in name
    )
    manifest = b"""
    {
      "product_tier": "enterprise",
      "model_profile": "streaming",
      "bundled_funasr_models": [
        "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
      ],
      "funasr_models": {
        "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
      }
    }
    """
    _write_payload(payload, files, product_manifest=manifest)

    _validate_payload(payload)


def test_validate_payload_rejects_full_profile_with_missing_review_roles(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    manifest = b"""
    {
      "product_tier": "enterprise",
      "model_profile": "full",
      "funasr_models": {
        "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
      }
    }
    """
    _write_payload(payload, REQUIRED_PAYLOAD_FILES, product_manifest=manifest)

    with pytest.raises(RuntimeError, match="full model profile is missing FunASR role"):
        _validate_payload(payload)


def test_validate_payload_rejects_missing_streaming_model(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    files = tuple(name for name in REQUIRED_PAYLOAD_FILES if "vocab8404-online/configuration.json" not in name)
    _write_payload(payload, files)

    with pytest.raises(RuntimeError, match="FunASR streaming model"):
        _validate_payload(payload)


def test_validate_payload_rejects_missing_ffmpeg_binary(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    files = tuple(name for name in REQUIRED_PAYLOAD_FILES if "imageio_ffmpeg/binaries" not in name)
    _write_payload(payload, files)

    with pytest.raises(RuntimeError, match="FFmpeg"):
        _validate_payload(payload)


def test_validate_payload_rejects_invalid_product_profile(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    _write_payload(payload, REQUIRED_PAYLOAD_FILES, product_manifest=b"not-json")

    with pytest.raises(RuntimeError, match="product profile"):
        _validate_payload(payload)


def test_validate_payload_rejects_empty_required_file(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    _write_payload(
        payload,
        REQUIRED_PAYLOAD_FILES,
        content_overrides={"VoiceInputAssistant.exe": b""},
    )

    with pytest.raises(RuntimeError, match="empty main application"):
        _validate_payload(payload)


def test_validate_payload_rejects_missing_model_auxiliary_file(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    missing = "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/config.yaml"
    _write_payload(payload, tuple(name for name in REQUIRED_PAYLOAD_FILES if name != missing))

    with pytest.raises(RuntimeError, match="FunASR streaming model resource"):
        _validate_payload(payload)


def test_validate_payload_rejects_integrity_mismatch(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    tampered = tmp_path / "VoiceInputAssistant-tampered.zip"
    _write_payload(payload, REQUIRED_PAYLOAD_FILES)
    with zipfile.ZipFile(payload) as source, zipfile.ZipFile(tampered, "w") as destination:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "README.md":
                content = b"tampered"
            destination.writestr(info, content)

    with pytest.raises(RuntimeError, match="integrity (size|SHA-256) mismatch"):
        _validate_payload(tampered)


@pytest.mark.parametrize(
    ("extra_names", "message"),
    (
        (("CON.txt",), "unsafe path"),
        (("readme.md",), "Windows path collision"),
    ),
)
def test_validate_payload_rejects_windows_unsafe_paths(
    tmp_path: Path, extra_names: tuple[str, ...], message: str
) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    _write_payload(payload, (*REQUIRED_PAYLOAD_FILES, *extra_names))

    with pytest.raises(RuntimeError, match=message):
        _validate_payload(payload)


def test_validate_payload_rejects_capabilities_inconsistent_with_roles(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    manifest = b"""
    {
      "model_profile": "full",
      "funasr_models": {
        "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
        "offline": "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "vad": "speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "punc": "punc_ct-transformer_cn-en-common-vocab471067-large",
        "speaker": "speech_campplus_sv_zh-cn_16k-common"
      },
      "funasr_capabilities": {
        "streaming": true,
        "offline_review": true,
        "vad": true,
        "punctuation": false,
        "speaker": true,
        "background_review": true
      }
    }
    """
    _write_payload(payload, REQUIRED_PAYLOAD_FILES, product_manifest=manifest)

    with pytest.raises(RuntimeError, match="capability punctuation is inconsistent"):
        _validate_payload(payload)


def test_extract_only_preserves_nonempty_destination(tmp_path: Path) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    _write_payload(payload, REQUIRED_PAYLOAD_FILES)

    with pytest.raises(RuntimeError, match="must be empty"):
        _extract_payload(payload, destination)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_install_payload_rolls_back_when_health_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    destination = tmp_path / "VoiceInputAssistant"
    destination.mkdir()
    old_exe = destination / "VoiceInputAssistant.exe"
    old_exe.write_bytes(b"old")
    _write_payload(
        payload,
        REQUIRED_PAYLOAD_FILES,
        content_overrides={"VoiceInputAssistant.exe": b"new"},
    )

    def fail_health(_install_dir: Path) -> None:
        raise RuntimeError("health failed")

    monkeypatch.setattr(_BOOTSTRAP, "_run_post_install_health_check", fail_health)
    with pytest.raises(RuntimeError, match="health failed"):
        _extract_payload_for_install(payload, destination)

    assert old_exe.read_bytes() == b"old"
    assert not list(tmp_path.glob(".VoiceInputAssistant.*-*"))


def test_install_payload_atomically_replaces_existing_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    destination = tmp_path / "VoiceInputAssistant"
    destination.mkdir()
    (destination / "VoiceInputAssistant.exe").write_bytes(b"old")
    _write_payload(
        payload,
        REQUIRED_PAYLOAD_FILES,
        content_overrides={"VoiceInputAssistant.exe": b"new"},
    )
    health_checks: list[Path] = []
    monkeypatch.setattr(
        _BOOTSTRAP,
        "_run_post_install_health_check",
        lambda install_dir: health_checks.append(install_dir),
    )

    _extract_payload_for_install(payload, destination)

    assert (destination / "VoiceInputAssistant.exe").read_bytes() == b"new"
    assert health_checks == [destination]
    assert not list(tmp_path.glob(".VoiceInputAssistant.*-*"))


def test_post_install_health_check_requires_valid_json_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    install_dir = tmp_path / "installed"
    _write_payload(payload, REQUIRED_PAYLOAD_FILES)
    with zipfile.ZipFile(payload) as archive:
        archive.extractall(install_dir)
    report_paths: list[Path] = []

    def run_health(command, **_kwargs):
        assert command[1:3] == ["--json", "--output"]
        report_path = Path(command[3])
        report_paths.append(report_path)
        report_path.write_text(
            json.dumps(
                {
                    "status": "warning",
                    "items": [
                        {
                            "name": "microphones",
                            "status": "ok",
                            "message": "扬声器（Realtek® Audio）",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        contaminated_stdout = "设备：扬声器（Realtek® Audio）\n".encode("cp936", errors="replace")
        return subprocess.CompletedProcess(command, 0, stdout=contaminated_stdout, stderr=b"")

    monkeypatch.setattr(_BOOTSTRAP, "_run_hidden", run_health)

    _run_post_install_health_check(install_dir)

    assert len(report_paths) == 1
    assert not report_paths[0].exists()


@pytest.mark.parametrize("report_content", [None, "", "not-json"])
def test_post_install_health_check_rejects_missing_or_invalid_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_content: str | None,
) -> None:
    install_dir = tmp_path / "installed"
    health_exe = install_dir / "tools" / "VoiceInputHealth" / "VoiceInputHealth.exe"
    health_exe.parent.mkdir(parents=True)
    health_exe.write_bytes(b"exe")

    def run_health(command, **_kwargs):
        if report_content is not None:
            Path(command[3]).write_text(report_content, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=b"ignored", stderr=b"")

    monkeypatch.setattr(_BOOTSTRAP, "_run_hidden", run_health)

    message = "did not produce a report" if not report_content else "returned invalid JSON"
    with pytest.raises(RuntimeError, match=message):
        _run_post_install_health_check(install_dir)


def test_post_install_health_check_rejects_nonzero_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dir = tmp_path / "installed"
    health_exe = install_dir / "tools" / "VoiceInputHealth" / "VoiceInputHealth.exe"
    health_exe.parent.mkdir(parents=True)
    health_exe.write_bytes(b"exe")

    def run_health(command, **_kwargs):
        Path(command[3]).write_text('{"status":"ok"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 2, stdout=b"", stderr=b"")

    monkeypatch.setattr(_BOOTSTRAP, "_run_hidden", run_health)

    with pytest.raises(RuntimeError, match="failed with exit code 2"):
        _run_post_install_health_check(install_dir)


@pytest.mark.parametrize(
    "report",
    [
        {"status": "error"},
        {"items": []},
        ["not", "a", "report"],
    ],
)
def test_post_install_health_check_rejects_invalid_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: object
) -> None:
    install_dir = tmp_path / "installed"
    health_exe = install_dir / "tools" / "VoiceInputHealth" / "VoiceInputHealth.exe"
    health_exe.parent.mkdir(parents=True)
    health_exe.write_bytes(b"exe")

    def run_health(command, **_kwargs):
        Path(command[3]).write_text(json.dumps(report), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(_BOOTSTRAP, "_run_hidden", run_health)

    with pytest.raises(RuntimeError, match="invalid runtime state"):
        _run_post_install_health_check(install_dir)


def test_main_check_runtime_validates_payload_and_frozen_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = tmp_path / "VoiceInputAssistant.zip"
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    calls: list[tuple[str, Path]] = []

    monkeypatch.setattr(_BOOTSTRAP, "_payload_path", lambda: payload)
    monkeypatch.setattr(
        _BOOTSTRAP,
        "_validate_payload",
        lambda path: calls.append(("payload", path)),
    )
    monkeypatch.setattr(
        _BOOTSTRAP,
        "_run_post_install_health_check",
        lambda path: calls.append(("runtime", path)),
    )

    assert _BOOTSTRAP.main(["--check-runtime", str(runtime_dir)]) == 0
    assert calls == [("payload", payload), ("runtime", runtime_dir.resolve())]
    assert "payload and runtime ok" in capsys.readouterr().out


def test_installer_wizard_uses_tk_fallback_when_qt_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = object()

    def unavailable_qt(**_options):
        raise ImportError("PySide6 unavailable")

    monkeypatch.setattr(_BOOTSTRAP, "InstallerWizard", unavailable_qt)
    monkeypatch.setattr(_BOOTSTRAP, "_TkInstallerWizard", lambda **_options: fallback)

    assert _BOOTSTRAP._create_installer_wizard(payload=Path("payload.zip")) is fallback


def test_qt_installer_uses_refined_setup_and_progress_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtTest

    payload = tmp_path / "VoiceInputAssistant.zip"
    payload.write_bytes(b"payload")
    long_install_dir = tmp_path / "VoiceInputAssistant" / ("very-long-folder-name-" * 6)

    wizard = InstallerWizard(
        payload=payload,
        default_install_dir=long_install_dir,
        default_desktop_shortcut=True,
        default_start_on_boot=False,
        default_launch_after_install=True,
    )
    try:
        wizard._set_install_dir_text(long_install_dir)
        wizard.window.show()
        wizard.qt_app.processEvents()

        brand_name = wizard.window.findChild(wizard.QtWidgets.QLabel, "InstallerBrandName")
        status_chip = wizard.window.findChild(wizard.QtWidgets.QLabel, "InstallerStatusChip")
        assert brand_name is not None and brand_name.text() == "VOICE INPUT"
        assert status_chip is not None and "全程本地处理" in status_chip.text()
        assert "#0f7f72" in wizard.window.styleSheet().lower()
        assert "#7c3aed" not in wizard.window.styleSheet().lower()

        setup_specs = wizard.window.findChild(wizard.QtWidgets.QFrame, "SetupSpecs")
        assert setup_specs is not None
        assert len(setup_specs.findChildren(wizard.QtWidgets.QFrame, "SpecRow")) == 3
        assert len(setup_specs.findChildren(wizard.QtWidgets.QLabel, "SpecLabel")) == 3
        assert len(setup_specs.findChildren(wizard.QtWidgets.QLabel, "SpecValue")) == 3
        setup_footnote = wizard.window.findChild(wizard.QtWidgets.QLabel, "SetupFootnote")
        option_divider = wizard.window.findChild(wizard.QtWidgets.QFrame, "OptionDivider")
        assurance = wizard.window.findChild(wizard.QtWidgets.QLabel, "AssuranceLabel")
        assert setup_footnote is not None and setup_footnote.text().strip()
        assert option_divider is not None
        assert assurance is not None and assurance.text().strip()

        assert wizard.install_dir_input.cursorPosition() == 0
        assert wizard.install_dir_input.hasSelectedText() is False
        assert "长路径会优先显示左侧开头" in wizard.install_dir_input.toolTip()
        assert str(long_install_dir) in wizard.install_dir_input.toolTip()
        assert wizard.install_dir_input.hasFocus() is False

        wizard.is_installing = False
        wizard._render_progress_page()
        assert wizard.progress_bar.isTextVisible() is False
        assert wizard.progress_bar.minimum() == 0
        assert wizard.progress_bar.maximum() == 100
        progress_percent = wizard.window.findChild(wizard.QtWidgets.QLabel, "ProgressPercent")
        motion_panel = wizard.window.findChild(wizard.QtWidgets.QWidget, "MotionPanel")
        motion_canvas = wizard.window.findChild(wizard.QtWidgets.QWidget, "MotionCanvas")
        assert progress_percent is wizard.progress_percent_label
        assert progress_percent.text() == "0"
        assert motion_panel is wizard._progress_visual
        assert motion_canvas is not None
        wizard.qt_app.processEvents()
        motion_before = motion_canvas.grab().toImage()
        QtTest.QTest.qWait(140)
        motion_after = motion_canvas.grab().toImage()
        assert motion_before != motion_after

        wizard._render_error_page("preview error")
        detail = wizard.window.findChild(wizard.QtWidgets.QPlainTextEdit, "ErrorDetails")
        assert detail is not None and detail.isReadOnly() is True
        assert detail.toPlainText() == "preview error"
        assert wizard._stage_labels[1].property("error") is True

        wizard._render_options_page()
        wizard.QtCore.QCoreApplication.sendPostedEvents(
            None, wizard.QtCore.QEvent.Type.DeferredDelete
        )
        wizard.qt_app.processEvents()
        assert wizard.install_dir_input.text() == str(long_install_dir)
        assert wizard.desktop_shortcut_check.isChecked() is True

        wizard._render_progress_page()
        wizard.QtCore.QCoreApplication.sendPostedEvents(
            None, wizard.QtCore.QEvent.Type.DeferredDelete
        )
        wizard.qt_app.processEvents()
        assert wizard.progress_bar.maximum() == 100
        assert wizard.progress_label.text()
    finally:
        wizard.window.close()
