from __future__ import annotations

import json
from pathlib import Path

from voice_input import accuracy_eval
from voice_input.accuracy_eval import EvaluationResult, EvaluationSample, levenshtein_distance, load_samples, summarize_results


def test_levenshtein_distance_for_cer() -> None:
    assert levenshtein_distance("政务服务", "政屋服务") == 1
    assert levenshtein_distance("", "abc") == 3
    assert levenshtein_distance("abc", "abc") == 0


def test_load_samples_from_csv(tmp_path) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    samples_path.write_text("audio,reference,language\nsample.wav,政务服务,zh-Hans\n", encoding="utf-8")

    samples = load_samples(samples_path)

    assert samples == (EvaluationSample(audio=audio, reference="政务服务", language="zh-Hans"),)


def test_summarize_results() -> None:
    summary = summarize_results(
        [
            EvaluationResult(
                audio=Path("a.wav"),
                reference="政务服务",
                hypothesis="政屋服务",
                cer=0.25,
                edit_distance=1,
                reference_chars=4,
                elapsed_seconds=0.5,
            )
        ]
    )

    assert summary["samples"] == 1
    assert summary["cer"] == 0.25
    assert summary["elapsed_seconds"] == 0.5


def test_main_outputs_json_report(tmp_path, monkeypatch, capsys) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    samples_path.write_text("audio,reference\nsample.wav,政务服务\n", encoding="utf-8")

    transcribe_options: list[dict[str, object]] = []

    class FakeEngine:
        def transcribe(self, audio_path, **options):
            assert audio_path == audio
            transcribe_options.append(options)
            return "政屋服务"

    monkeypatch.setattr(accuracy_eval, "resolve_runtime_settings", lambda settings: settings)
    monkeypatch.setattr(accuracy_eval, "build_asr_engine", lambda _settings: FakeEngine())
    monkeypatch.setattr(accuracy_eval, "probe_audio_duration_seconds", lambda _path: 10.0)

    assert accuracy_eval.main([str(samples_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["summary"]["samples"] == 1
    assert report["summary"]["cer"] == 0.0
    assert report["results"][0]["hypothesis"] == "政务服务。"
    assert transcribe_options[0]["timeout_seconds"] == 120.0


def test_main_writes_utf8_json_report(tmp_path, monkeypatch, capsys) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    output_path = tmp_path / "report.json"
    samples_path.write_text("audio,reference\nsample.wav,政务服务\n", encoding="utf-8")

    class FakeEngine:
        def transcribe(self, audio_path, **_options):
            return "政务服务"

    monkeypatch.setattr(accuracy_eval, "resolve_runtime_settings", lambda settings: settings)
    monkeypatch.setattr(accuracy_eval, "build_asr_engine", lambda _settings: FakeEngine())

    assert accuracy_eval.main([str(samples_path), "--json", "--output", str(output_path)]) == 0
    assert capsys.readouterr().out == ""
    assert output_path.read_bytes().startswith(b"{")
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["results"][0]["reference"] == "政务服务。"


def test_main_accepts_explicit_per_sample_timeout(tmp_path, monkeypatch, capsys) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    samples_path.write_text("audio,reference\nsample.wav,政务服务\n", encoding="utf-8")
    timeouts: list[float] = []

    class FakeEngine:
        def transcribe(self, _audio_path, **options):
            timeouts.append(options["timeout_seconds"])
            return "政务服务"

    monkeypatch.setattr(accuracy_eval, "resolve_runtime_settings", lambda settings: settings)
    monkeypatch.setattr(accuracy_eval, "build_asr_engine", lambda _settings: FakeEngine())

    assert accuracy_eval.main(
        [str(samples_path), "--json", "--timeout-seconds", "45"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["samples"] == 1
    assert timeouts == [45.0]
