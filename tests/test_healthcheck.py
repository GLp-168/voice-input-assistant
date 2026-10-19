from __future__ import annotations

import json

from voice_input import healthcheck
from voice_input.healthcheck import HealthCheckItem, run_health_checks
from voice_input.settings import AppSettings


def test_run_health_checks_summarizes_warnings(monkeypatch) -> None:
    monkeypatch.setattr(
        healthcheck,
        "_check_runtime",
        lambda _settings, _smoke: HealthCheckItem("runtime", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_offline_bundle",
        lambda _settings, _smoke: HealthCheckItem("offline_bundle", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_streaming_bundle",
        lambda _settings, _smoke: HealthCheckItem("streaming_bundle", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_audio_io",
        lambda _settings, _smoke: HealthCheckItem("audio_io", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_streaming_load",
        lambda _settings, _smoke: HealthCheckItem("streaming_load", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_microphones",
        lambda _settings, _smoke: HealthCheckItem("microphones", "warning", "no input devices found"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_ffmpeg",
        lambda _settings, _smoke: HealthCheckItem("ffmpeg", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_lexicons",
        lambda _settings, _smoke: HealthCheckItem("lexicons", "ok"),
    )
    monkeypatch.setattr(
        healthcheck,
        "_check_logs",
        lambda _settings, _smoke: HealthCheckItem("logs", "ok"),
    )

    report = run_health_checks(AppSettings())

    assert report.status == "warning"
    assert len(report.items) == 9


def test_audio_io_health_check_performs_wav_round_trip() -> None:
    item = healthcheck._check_audio_io(AppSettings(), False)

    assert item.status == "ok"
    assert item.name == "audio_io"
    assert item.details is not None
    assert item.details["sample_rate"] == 16000
    assert item.details["frames"] == 1600
    assert item.details["resampled_frames"] == 1600


def test_healthcheck_main_outputs_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        healthcheck,
        "run_health_checks",
        lambda settings, microphone_smoke=False: healthcheck.HealthReport(
            status="ok",
            generated_at=1.0,
            items=(HealthCheckItem("runtime", "ok", "ready"),),
        ),
    )

    assert healthcheck.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "ok"
    assert payload["items"][0]["name"] == "runtime"


def test_healthcheck_main_writes_output_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        healthcheck,
        "run_health_checks",
        lambda settings, microphone_smoke=False: healthcheck.HealthReport(
            status="ok",
            generated_at=1.0,
            items=(HealthCheckItem("microphones", "ok", "USB Microphone ®"),),
        ),
    )
    opened = []
    output_path = tmp_path / "report.json"
    monkeypatch.setattr(healthcheck, "_open_path", lambda path: opened.append(path))

    assert healthcheck.main(["--json", "--output", str(output_path), "--open"]) == 0

    assert opened == [output_path]
    assert "USB Microphone ®" in output_path.read_text(encoding="utf-8")


def test_basic_streaming_package_does_not_require_ffmpeg(monkeypatch) -> None:
    monkeypatch.setattr("voice_input.healthcheck.packaged_model_profile", lambda: "streaming")
    monkeypatch.setattr(
        "voice_input.healthcheck.find_ffmpeg_executable",
        lambda: (_ for _ in ()).throw(AssertionError("ffmpeg should not be probed for basic packages")),
    )

    item = healthcheck._check_ffmpeg(AppSettings(), False)

    assert item.status == "ok"
    assert item.details == {"path": "", "required": False}


def test_full_profile_still_reports_missing_ffmpeg_when_review_models_are_damaged(monkeypatch) -> None:
    monkeypatch.setattr("voice_input.healthcheck.packaged_model_profile", lambda: "full")
    monkeypatch.setattr("voice_input.healthcheck.find_ffmpeg_executable", lambda: "")

    item = healthcheck._check_ffmpeg(AppSettings(), False)

    assert item.status == "warning"
    assert item.details == {"path": ""}


def test_lexicon_check_reports_invalid_custom_file(tmp_path) -> None:
    custom = tmp_path / "bad.txt"
    custom.write_text("missing separator\n", encoding="utf-8")

    item = healthcheck._check_lexicons(
        AppSettings(
            lexicon_enabled=True,
            lexicon_path=str(custom),
            enabled_builtin_lexicons=(),
        ),
        False,
    )

    assert item.status == "error"
    assert "custom lexicon failed" in item.message


def test_lexicon_check_accepts_all_builtin_lexicons_in_default_dev_tier(tmp_path, monkeypatch) -> None:
    root = tmp_path / "assets" / "lexicons"
    root.mkdir(parents=True)
    for filename in (
        "government_digital.json",
        "official_document.json",
        "office_meeting.json",
        "finance_budget.json",
        "project_engineering.json",
    ):
        (root / filename).write_text("[]", encoding="utf-8")
    monkeypatch.setattr("voice_input.resources.bundled_lexicon_dir", lambda: root)

    item = healthcheck._check_lexicons(
        AppSettings(
            lexicon_enabled=True,
            product_tier="standard",
            enabled_builtin_lexicons=(
                "government_digital",
                "official_document",
                "office_meeting",
                "finance_budget",
                "project_engineering",
            ),
        ),
        False,
    )

    assert item.status == "ok"
    assert item.message == "5 bundled lexicon file(s)"
    assert item.details is not None
    assert item.details["product_tier"] == "enterprise"
