from __future__ import annotations

from pathlib import Path

from voice_input import resources
from voice_input.resources import (
    FUNASR_MODEL_DIRS,
    FUNASR_MODEL_REQUIRED_FILES,
    DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
    bundled_funasr_speaker_model,
    has_bundled_funasr_review_models,
    has_bundled_funasr_speaker_model,
    has_bundled_funasr_streaming_model,
    lexicon_catalog_from_dir,
    packaged_funasr_model_dirs,
    packaged_funasr_model_roles,
    packaged_model_profile,
    packaged_enabled_builtin_lexicons,
    packaged_product_tier,
    funasr_model_file_status,
    has_bundled_funasr_models,
)


def _write_model_file(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_funasr_bundle_requires_nonempty_model_weights(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vendor" / "funasr" / "models"
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)

    for dirname in FUNASR_MODEL_DIRS:
        model_dir = root / dirname
        for filename in FUNASR_MODEL_REQUIRED_FILES[dirname]:
            _write_model_file(model_dir / filename)

    incomplete = root / FUNASR_MODEL_DIRS[1] / "model.pt"
    incomplete.write_bytes(b"")

    assert has_bundled_funasr_models() is False
    assert funasr_model_file_status(incomplete.parent, FUNASR_MODEL_DIRS[1]) == {
        filename: filename != "model.pt"
        for filename in FUNASR_MODEL_REQUIRED_FILES[FUNASR_MODEL_DIRS[1]]
    }

    incomplete.write_bytes(b"weights")

    assert has_bundled_funasr_models() is True


def test_packaged_product_profile_controls_tier_and_default_lexicons(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    profile = tmp_path / "assets" / "product.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '{"product_tier":"standard","enabled_builtin_lexicons":["government_digital","office_meeting"]}',
        encoding="utf-8",
    )

    assert packaged_product_tier() == "standard"
    assert packaged_enabled_builtin_lexicons() == ("government_digital", "office_meeting")


def test_basic_product_profile_keeps_all_builtin_lexicons(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    profile = tmp_path / "assets" / "product.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        '{"product_tier":"basic","enabled_builtin_lexicons":["government_digital","official_document","office_meeting","finance_budget","project_engineering"]}',
        encoding="utf-8",
    )

    assert packaged_product_tier() == "basic"
    assert packaged_enabled_builtin_lexicons() == (
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    )


def test_packaged_product_profile_controls_funasr_model_roles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    profile = tmp_path / "assets" / "product.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        """
        {
          "product_tier": "basic",
          "bundled_funasr_models": [
            "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
          ],
          "funasr_models": {
            "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
          }
        }
        """,
        encoding="utf-8",
    )
    model_root = tmp_path / "models" / "funasr"
    streaming_dir = "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
    for filename in FUNASR_MODEL_REQUIRED_FILES[streaming_dir]:
        _write_model_file(model_root / streaming_dir / filename)

    assert packaged_funasr_model_roles() == {
        "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
    }
    assert packaged_funasr_model_dirs() == ("speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",)
    assert has_bundled_funasr_models() is True
    assert has_bundled_funasr_streaming_model() is True
    assert has_bundled_funasr_review_models() is False
    assert packaged_model_profile() == "streaming"


def test_packaged_model_profile_prefers_explicit_full_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    profile = tmp_path / "assets" / "product.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        """
        {
          "model_profile": "full",
          "funasr_models": {
            "streaming": "streaming-model",
            "offline": "offline-model",
            "vad": "vad-model",
            "punc": "punc-model"
          }
        }
        """,
        encoding="utf-8",
    )

    assert packaged_model_profile() == "full"


def test_full_profile_resolves_bundled_speaker_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    profile = tmp_path / "assets" / "product.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        f'''{{
          "model_profile": "full",
          "bundled_funasr_models": ["{DEFAULT_FUNASR_SPEAKER_MODEL_DIR}"],
          "funasr_models": {{"speaker": "{DEFAULT_FUNASR_SPEAKER_MODEL_DIR}"}}
        }}''',
        encoding="utf-8",
    )
    model_dir = tmp_path / "models" / "funasr" / DEFAULT_FUNASR_SPEAKER_MODEL_DIR
    for filename in FUNASR_MODEL_REQUIRED_FILES[DEFAULT_FUNASR_SPEAKER_MODEL_DIR]:
        _write_model_file(model_dir / filename)

    assert has_bundled_funasr_speaker_model() is True
    assert bundled_funasr_speaker_model() == str(model_dir)


def test_packaged_profile_does_not_fall_back_to_online_alias_for_missing_model(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    profile = tmp_path / "assets" / "product.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        """
        {
          "model_profile": "full",
          "funasr_models": {
            "streaming": "missing-streaming",
            "offline": "missing-offline",
            "vad": "missing-vad",
            "punc": "missing-punc"
          }
        }
        """,
        encoding="utf-8",
    )

    assert resources.bundled_funasr_streaming_model() == ""
    assert resources.bundled_funasr_offline_model() == ""


def test_packaged_product_profile_defaults_to_enterprise_without_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)

    assert packaged_product_tier() == "enterprise"
    assert packaged_enabled_builtin_lexicons() == (
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    )


def test_packaged_product_profile_can_enable_manifest_lexicon(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(resources, "app_base_dir", lambda: tmp_path)
    lexicon_dir = tmp_path / "assets" / "lexicons"
    lexicon_dir.mkdir(parents=True)
    (lexicon_dir / "client_terms.csv").write_text("source,target\n市府,市政府\n", encoding="utf-8")
    (lexicon_dir / "catalog.json").write_text(
        """
        {
          "schema_version": 1,
          "lexicons": [
            {
              "id": "client_government",
              "title": "客户政务词库",
              "filename": "client_terms.csv",
              "default_enabled": false,
              "min_tier": "standard"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    profile = tmp_path / "assets" / "product.json"
    profile.write_text(
        '{"product_tier":"standard","enabled_builtin_lexicons":["client_government"]}',
        encoding="utf-8",
    )

    assert [item.id for item in lexicon_catalog_from_dir(lexicon_dir, strict=True)] == ["client_government"]
    assert packaged_enabled_builtin_lexicons() == ("client_government",)
