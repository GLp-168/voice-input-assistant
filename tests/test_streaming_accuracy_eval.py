from __future__ import annotations

import json

import pytest

from voice_input import streaming_accuracy_eval
from voice_input.settings import AppSettings
from voice_input.streaming_accuracy_eval import (
    FunASRFileStreamingTranscriber,
    LexiconSuggestion,
    StreamingEvaluationResult,
    StreamingEvaluationSample,
    StreamingTranscript,
    StreamingTranscriptEvent,
    collect_hotwords,
    evaluate_streaming_samples,
    load_streaming_samples,
    score_text,
    suggest_lexicon_candidates,
    summarize_streaming_results,
)
from voice_input.lexicon import Lexicon


class FakeTranscriber:
    instances: list["FakeTranscriber"] = []

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.calls: list[tuple[object, str | None, tuple[str, ...], bool]] = []
        self.closed = False
        FakeTranscriber.instances.append(self)

    def transcribe_file(self, audio, *, language=None, hotwords=(), realtime=False):
        self.calls.append((audio, language, tuple(hotwords), realtime))
        return StreamingTranscript(
            preview_text="hello word",
            final_text="hello world",
            events=(
                StreamingTranscriptEvent(
                    text="hello word",
                    is_final=False,
                    is_endpoint=False,
                    elapsed_seconds=0.2,
                ),
            ),
            first_event_seconds=0.2,
            elapsed_seconds=1.5,
        )

    def close(self) -> None:
        self.closed = True


def test_score_text_ignores_spacing_and_punctuation() -> None:
    assert score_text("hello, world!") == "helloworld"
    assert score_text("a b\tc") == "abc"


def test_collect_hotwords_uses_canonical_targets_only(tmp_path, monkeypatch) -> None:
    root = tmp_path / "assets" / "lexicons"
    root.mkdir(parents=True)
    (root / "custom.json").write_text(
        '[{"source":"wrong term","target":"right term","rule_type":"replace"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.resources.bundled_lexicon_dir", lambda: root)
    monkeypatch.setattr("voice_input.streaming_accuracy_eval.bundled_lexicon_paths", lambda *_args, **_kwargs: (root / "custom.json",))

    words = collect_hotwords(AppSettings(lexicon_enabled=True))

    assert words == ("right term",)


def test_load_streaming_samples_from_csv_with_hotwords(tmp_path) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    samples_path.write_text(
        "audio,reference,language,hotwords\nsample.wav,hello world,zh-Hans,hello;world\n",
        encoding="utf-8",
    )

    samples = load_streaming_samples(samples_path)

    assert samples == (
        StreamingEvaluationSample(
            audio=audio,
            reference="hello world",
            language="zh-Hans",
            hotwords=("hello", "world"),
        ),
    )


def test_load_streaming_samples_from_jsonl_with_hotword_list(tmp_path) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.jsonl"
    samples_path.write_text(
        json.dumps(
            {
                "audio": "sample.wav",
                "reference": "hello world",
                "hotwords": ["hello", "world"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    samples = load_streaming_samples(samples_path)

    assert samples[0].hotwords == ("hello", "world")


def test_evaluate_streaming_samples_reports_preview_and_final_cer(tmp_path, monkeypatch) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    monkeypatch.setattr(streaming_accuracy_eval, "resolve_runtime_settings", lambda settings: settings)

    results = evaluate_streaming_samples(
        [
            StreamingEvaluationSample(
                audio=audio,
                reference="hello world",
                language="zh-Hans",
                hotwords=("world",),
            )
        ],
        AppSettings(lexicon_enabled=False),
        transcriber_factory=FakeTranscriber,
        realtime=True,
    )

    assert len(results) == 1
    result = results[0]
    assert result.preview_cer == 0.1
    assert result.final_cer == 0.0
    assert result.post_lexicon_preview_cer == 0.1
    assert result.post_lexicon_final_cer == 0.0
    assert result.hotword_total == 1
    assert result.hotword_hits == 1
    assert result.events == 1
    assert result.first_event_seconds == 0.2
    assert FakeTranscriber.instances[-1].calls[0][2] == ("world",)
    assert FakeTranscriber.instances[-1].calls[0][3] is True
    assert FakeTranscriber.instances[-1].closed is True


def test_evaluation_separates_online_and_review_final_metrics(tmp_path, monkeypatch) -> None:
    class ReviewedTranscriber(FakeTranscriber):
        def transcribe_file(self, audio, *, language=None, hotwords=(), realtime=False):
            return StreamingTranscript(
                preview_text="项目推近会",
                online_final_text="项目推近会",
                final_text="项目推进会",
                events=(),
                first_event_seconds=0.3,
                online_final_elapsed_seconds=1.0,
                elapsed_seconds=2.0,
                review_status="completed",
            )

    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    monkeypatch.setattr(streaming_accuracy_eval, "resolve_runtime_settings", lambda settings: settings)

    results = evaluate_streaming_samples(
        [StreamingEvaluationSample(audio=audio, reference="项目推进会")],
        AppSettings(lexicon_enabled=False),
        transcriber_factory=ReviewedTranscriber,
    )
    result = results[0]
    summary = summarize_streaming_results(results)

    assert result.online_final_hypothesis == "项目推近会"
    assert result.online_final_cer == 0.2
    assert result.final_hypothesis == "项目推进会"
    assert result.final_cer == 0.0
    assert result.review_status == "completed"
    assert summary["online_final_cer"] == 0.2
    assert summary["final_cer"] == 0.0
    assert summary["review_completed_samples"] == 1


def test_evaluation_keeps_reference_immutable_and_separates_lexicon_metrics(
    tmp_path,
    monkeypatch,
) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    lexicon_path = tmp_path / "lexicon.json"
    lexicon_path.write_text(
        json.dumps(
            [
                {
                    "source": "项目推近会",
                    "target": "项目推进会",
                    "rule_type": "replace",
                },
                {
                    "source": "项目推进会",
                    "target": "推进会",
                    "rule_type": "replace",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class LexiconTranscriber(FakeTranscriber):
        def transcribe_file(self, audio, *, language=None, hotwords=(), realtime=False):
            self.calls.append((audio, language, tuple(hotwords), realtime))
            return StreamingTranscript(
                preview_text="项目推近会",
                final_text="项目推近会",
                events=(),
                first_event_seconds=None,
                elapsed_seconds=1.0,
            )

    monkeypatch.setattr(streaming_accuracy_eval, "resolve_runtime_settings", lambda settings: settings)
    results = evaluate_streaming_samples(
        [
            StreamingEvaluationSample(
                audio=audio,
                reference="项目推进会",
                hotwords=("项目推进会",),
            )
        ],
        AppSettings(
            lexicon_enabled=True,
            enabled_builtin_lexicons=(),
            lexicon_path=str(lexicon_path),
        ),
        transcriber_factory=LexiconTranscriber,
    )

    result = results[0]
    assert result.reference == "项目推进会"
    assert result.preview_hypothesis == "项目推近会"
    assert result.final_hypothesis == "项目推近会"
    assert result.preview_cer == 0.2
    assert result.final_cer == 0.2
    assert result.post_lexicon_preview_hypothesis == "项目推进会。"
    assert result.post_lexicon_final_hypothesis == "项目推进会。"
    assert result.post_lexicon_preview_cer == 0.0
    assert result.post_lexicon_final_cer == 0.0
    assert result.hotword_total == 1
    assert result.hotword_hits == 0
    summary = summarize_streaming_results(results)
    assert summary["raw_preview_cer"] == 0.2
    assert summary["raw_final_cer"] == 0.2
    assert summary["post_lexicon_preview_cer"] == 0.0
    assert summary["post_lexicon_final_cer"] == 0.0
    assert summary["hotword_recall"] == 0.0


def test_office_meeting_lexicon_preserves_project_progress_meeting() -> None:
    lexicon_path = (
        streaming_accuracy_eval.Path(__file__).parents[1]
        / "assets"
        / "lexicons"
        / "office_meeting.json"
    )
    lexicon = Lexicon.from_file(lexicon_path)

    assert lexicon.normalize("项目推进会") == "项目推进会"
    assert "项目推进会" in lexicon.canonical_terms()


def test_summarize_streaming_results() -> None:
    result = StreamingEvaluationResult(
        audio=__file__,
        reference="hello world",
        preview_hypothesis="hello word",
        final_hypothesis="hello world",
        preview_cer=0.1,
        final_cer=0.0,
        preview_edit_distance=1,
        final_edit_distance=0,
        reference_chars=10,
        elapsed_seconds=1.5,
        first_event_seconds=0.2,
        events=1,
        hotword_total=2,
        hotword_hits=1,
    )

    summary = summarize_streaming_results([result])

    assert summary["samples"] == 1
    assert summary["preview_cer"] == 0.1
    assert summary["final_cer"] == 0.0
    assert summary["raw_preview_cer"] == 0.1
    assert summary["raw_final_cer"] == 0.0
    assert summary["post_lexicon_preview_cer"] is None
    assert summary["post_lexicon_final_cer"] is None
    assert summary["hotword_recall"] == 0.5
    assert summary["first_event_avg_seconds"] == 0.2


def test_suggest_lexicon_candidates_from_preview_and_final_errors() -> None:
    result = StreamingEvaluationResult(
        audio="sample.wav",
        reference="政务服务",
        preview_hypothesis="政屋服务",
        final_hypothesis="政务服务",
        preview_cer=0.25,
        final_cer=0.0,
        preview_edit_distance=1,
        final_edit_distance=0,
        reference_chars=4,
        elapsed_seconds=1.0,
        first_event_seconds=0.2,
        events=1,
        hotword_total=0,
        hotword_hits=0,
    )

    suggestions = suggest_lexicon_candidates([result])

    assert suggestions == (
        LexiconSuggestion(
            source="屋",
            target="务",
            stage="preview",
            count=1,
            audios=("sample.wav",),
        ),
    )


def test_main_outputs_json_report(tmp_path, monkeypatch, capsys) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    samples_path.write_text("audio,reference\nsample.wav,hello world\n", encoding="utf-8")
    monkeypatch.setattr(streaming_accuracy_eval, "resolve_runtime_settings", lambda settings: settings)
    monkeypatch.setattr(streaming_accuracy_eval, "FunASRFileStreamingTranscriber", FakeTranscriber)

    assert streaming_accuracy_eval.main([str(samples_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["summary"]["samples"] == 1
    assert report["summary"]["preview_cer"] == 0.1
    assert report["summary"]["final_cer"] == 0.0
    assert report["summary"]["raw_preview_cer"] == 0.1
    assert report["summary"]["post_lexicon_preview_cer"] == 0.1
    assert "lexicon_suggestions" in report
    assert report["results"][0]["preview_hypothesis"].startswith("hello")
    assert report["results"][0]["raw_preview_cer"] == 0.1
    assert report["results"][0]["post_lexicon_preview_cer"] == 0.1


def test_main_writes_utf8_report_and_suggestion_csv(tmp_path, monkeypatch, capsys) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    samples_path = tmp_path / "samples.csv"
    report_path = tmp_path / "report.json"
    suggestions_path = tmp_path / "suggestions.csv"
    samples_path.write_text("audio,reference\nsample.wav,hello world\n", encoding="utf-8")

    class SuggestionTranscriber(FakeTranscriber):
        def transcribe_file(self, audio, *, language=None, hotwords=(), realtime=False):
            self.calls.append((audio, language, tuple(hotwords), realtime))
            return StreamingTranscript(
                preview_text="hello ward",
                final_text="hello world",
                events=(),
                first_event_seconds=None,
                elapsed_seconds=1.5,
            )

    monkeypatch.setattr(streaming_accuracy_eval, "resolve_runtime_settings", lambda settings: settings)
    monkeypatch.setattr(streaming_accuracy_eval, "FunASRFileStreamingTranscriber", SuggestionTranscriber)

    assert (
        streaming_accuracy_eval.main(
            [
                str(samples_path),
                "--json",
                "--output",
                str(report_path),
                "--suggestions-output",
                str(suggestions_path),
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == ""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["samples"] == 1
    csv_payload = suggestions_path.read_text(encoding="utf-8-sig")
    assert "source,target,rule_type" in csv_payload
    assert ",replace," in csv_payload
    assert ",false,true,evaluation,custom,review_required,0.40," in csv_payload


def test_file_transcriber_clears_stale_worker_messages() -> None:
    transcriber = FunASRFileStreamingTranscriber(AppSettings(lexicon_enabled=False))
    transcriber._output.put({"type": "final", "text": "stale"})
    transcriber._output.put({"type": "event", "text": "stale"})

    transcriber._clear_pending_messages()

    assert transcriber._output.empty()


@pytest.mark.parametrize(
    ("review_text", "raises", "expected_text", "expected_status"),
    [
        ("复核文本", False, "复核文本", "completed"),
        ("", False, "在线文本", "empty_fallback"),
        ("", True, "在线文本", "failed_fallback"),
    ],
)
def test_file_transcriber_review_status_and_fallback(
    tmp_path,
    review_text,
    raises,
    expected_text,
    expected_status,
) -> None:
    closed: list[bool] = []

    class FakeReviewEngine:
        def transcribe(self, _audio, **_kwargs):
            if raises:
                raise RuntimeError("review failed")
            return type("Result", (), {"text": review_text})()

        def close(self):
            closed.append(True)

    transcriber = FunASRFileStreamingTranscriber(
        AppSettings(funasr_offline_model="offline", lexicon_enabled=False)
    )
    transcriber._review_engine = FakeReviewEngine()
    transcriber.retain_review_worker = False

    text, status = transcriber._review_file(
        tmp_path / "sample.wav",
        language="zh",
        hotwords=("项目推进会",),
        fallback_text="在线文本",
    )

    assert text == expected_text
    assert status == expected_status
    assert closed == [True]
    assert transcriber._review_engine is None
