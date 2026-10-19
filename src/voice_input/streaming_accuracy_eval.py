"""File-driven streaming ASR accuracy evaluation utilities."""

from __future__ import annotations

import argparse
import base64
import csv
import difflib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .accuracy_eval import levenshtein_distance
from .app import normalize_recognition_text, normalize_streaming_text, resolve_runtime_settings
from .asr import FunASROfflineEngine
from .config import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE
from .funasr_streaming_process import _format_funasr_worker_failure, _worker_command
from .lexicon import Lexicon, LexiconRuleType
from .media import requires_audio_transcode, transcode_audio_to_wav
from .resources import (
    bundled_lexicon_paths,
)
from .resource_policy import (
    ResourcePolicyMode,
    SessionResourcePolicy,
    capture_system_resource_snapshot,
)
from .settings import AppSettings, SettingsStore
from .streaming_process import _read_json_lines, _read_text_lines, _terminate_process, _write_worker_message


@dataclass(frozen=True, slots=True)
class StreamingEvaluationSample:
    audio: Path
    reference: str
    language: str | None = None
    hotwords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StreamingTranscriptEvent:
    text: str
    is_final: bool
    is_endpoint: bool
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class StreamingTranscript:
    preview_text: str
    final_text: str
    events: tuple[StreamingTranscriptEvent, ...]
    first_event_seconds: float | None
    elapsed_seconds: float
    online_final_text: str | None = None
    online_final_elapsed_seconds: float | None = None
    review_status: str = "not_configured"
    ready_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class StreamingEvaluationResult:
    audio: Path
    reference: str
    preview_hypothesis: str
    final_hypothesis: str
    preview_cer: float
    final_cer: float
    preview_edit_distance: int
    final_edit_distance: int
    reference_chars: int
    elapsed_seconds: float
    first_event_seconds: float | None
    events: int
    hotword_total: int
    hotword_hits: int
    post_lexicon_preview_hypothesis: str | None = None
    post_lexicon_final_hypothesis: str | None = None
    post_lexicon_preview_cer: float | None = None
    post_lexicon_final_cer: float | None = None
    post_lexicon_preview_edit_distance: int | None = None
    post_lexicon_final_edit_distance: int | None = None
    online_final_hypothesis: str | None = None
    online_final_cer: float | None = None
    online_final_edit_distance: int | None = None
    online_final_elapsed_seconds: float | None = None
    review_status: str = "not_configured"
    online_hotword_hits: int | None = None
    ready_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LexiconSuggestion:
    source: str
    target: str
    stage: str
    count: int
    audios: tuple[str, ...]


class StreamingTranscriber(Protocol):
    def transcribe_file(
        self,
        audio: Path,
        *,
        language: str | None = None,
        hotwords: Iterable[str] = (),
        realtime: bool = False,
    ) -> StreamingTranscript:
        ...

    def close(self) -> None:
        ...


TranscriberFactory = Callable[[AppSettings], StreamingTranscriber]


def load_streaming_samples(path: str | Path) -> tuple[StreamingEvaluationSample, ...]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".jsonl":
        return tuple(_load_jsonl_samples(source))
    if suffix in {".csv", ".tsv"}:
        return tuple(_load_delimited_samples(source, delimiter="\t" if suffix == ".tsv" else ","))
    raise ValueError("sample file must be .csv, .tsv, or .jsonl")


def evaluate_streaming_samples(
    samples: Iterable[StreamingEvaluationSample],
    settings: AppSettings,
    *,
    transcriber_factory: TranscriberFactory | None = None,
    realtime: bool = False,
) -> tuple[StreamingEvaluationResult, ...]:
    runtime_settings = resolve_runtime_settings(settings)
    factory = transcriber_factory or FunASRFileStreamingTranscriber
    transcriber = factory(runtime_settings)
    results: list[StreamingEvaluationResult] = []
    try:
        default_hotwords = collect_hotwords(runtime_settings)
        for sample in samples:
            sample_settings = runtime_settings
            if sample.language:
                sample_settings = AppSettings.from_dict(
                    {**runtime_settings.to_dict(), "recognition_language": sample.language}
                )
            hotwords = _merge_words(sample.hotwords, default_hotwords)
            transcript = transcriber.transcribe_file(
                sample.audio,
                language=sample.language,
                hotwords=hotwords,
                realtime=realtime,
            )
            # Ground truth and raw hypotheses are scoring inputs, not runtime output.
            # Keep them outside the lexicon pipeline so corrections cannot improve
            # the model's reported CER or hotword recall.
            reference = sample.reference
            preview = transcript.preview_text.strip()
            online_final = (
                transcript.online_final_text
                if transcript.online_final_text is not None
                else transcript.final_text
            ).strip()
            final = transcript.final_text.strip()
            post_lexicon_preview = normalize_streaming_text(
                transcript.preview_text,
                sample_settings,
                close_sentence=True,
            )
            post_lexicon_final = normalize_recognition_text(transcript.final_text, sample_settings)
            scored_reference = score_text(reference)
            scored_preview = score_text(preview)
            scored_online_final = score_text(online_final)
            scored_final = score_text(final)
            scored_post_lexicon_preview = score_text(post_lexicon_preview)
            scored_post_lexicon_final = score_text(post_lexicon_final)
            preview_distance = levenshtein_distance(scored_reference, scored_preview)
            online_final_distance = levenshtein_distance(scored_reference, scored_online_final)
            final_distance = levenshtein_distance(scored_reference, scored_final)
            post_lexicon_preview_distance = levenshtein_distance(
                scored_reference,
                scored_post_lexicon_preview,
            )
            post_lexicon_final_distance = levenshtein_distance(
                scored_reference,
                scored_post_lexicon_final,
            )
            denominator = max(1, len(scored_reference))
            tracked_hotwords = _tracked_hotwords(sample, reference, hotwords)
            online_hotword_hits = sum(1 for word in tracked_hotwords if word in online_final)
            hotword_hits = sum(1 for word in tracked_hotwords if word in final)
            results.append(
                StreamingEvaluationResult(
                    audio=sample.audio,
                    reference=reference,
                    preview_hypothesis=preview,
                    final_hypothesis=final,
                    preview_cer=preview_distance / denominator,
                    final_cer=final_distance / denominator,
                    preview_edit_distance=preview_distance,
                    final_edit_distance=final_distance,
                    reference_chars=len(scored_reference),
                    elapsed_seconds=transcript.elapsed_seconds,
                    first_event_seconds=transcript.first_event_seconds,
                    events=len(transcript.events),
                    hotword_total=len(tracked_hotwords),
                    hotword_hits=hotword_hits,
                    post_lexicon_preview_hypothesis=post_lexicon_preview,
                    post_lexicon_final_hypothesis=post_lexicon_final,
                    post_lexicon_preview_cer=post_lexicon_preview_distance / denominator,
                    post_lexicon_final_cer=post_lexicon_final_distance / denominator,
                    post_lexicon_preview_edit_distance=post_lexicon_preview_distance,
                    post_lexicon_final_edit_distance=post_lexicon_final_distance,
                    online_final_hypothesis=online_final,
                    online_final_cer=online_final_distance / denominator,
                    online_final_edit_distance=online_final_distance,
                    online_final_elapsed_seconds=transcript.online_final_elapsed_seconds,
                    review_status=transcript.review_status,
                    online_hotword_hits=online_hotword_hits,
                    ready_seconds=transcript.ready_seconds,
                )
            )
    finally:
        transcriber.close()
    return tuple(results)


def summarize_streaming_results(results: Iterable[StreamingEvaluationResult]) -> dict[str, float | int | None]:
    items = tuple(results)
    total_chars = sum(item.reference_chars for item in items)
    total_preview_distance = sum(item.preview_edit_distance for item in items)
    total_online_final_distance = sum(
        item.online_final_edit_distance
        if item.online_final_edit_distance is not None
        else item.final_edit_distance
        for item in items
    )
    total_final_distance = sum(item.final_edit_distance for item in items)
    total_hotwords = sum(item.hotword_total for item in items)
    total_hotword_hits = sum(item.hotword_hits for item in items)
    total_online_hotword_hits = sum(
        item.online_hotword_hits if item.online_hotword_hits is not None else item.hotword_hits
        for item in items
    )
    first_events = [item.first_event_seconds for item in items if item.first_event_seconds is not None]
    ready_durations = [item.ready_seconds for item in items if item.ready_seconds is not None]
    post_lexicon_items = tuple(
        item
        for item in items
        if item.post_lexicon_preview_edit_distance is not None
        and item.post_lexicon_final_edit_distance is not None
    )
    post_lexicon_chars = sum(item.reference_chars for item in post_lexicon_items)
    post_lexicon_preview_distance = sum(
        item.post_lexicon_preview_edit_distance or 0 for item in post_lexicon_items
    )
    post_lexicon_final_distance = sum(
        item.post_lexicon_final_edit_distance or 0 for item in post_lexicon_items
    )
    post_lexicon_preview_cer = (
        post_lexicon_preview_distance / max(1, post_lexicon_chars) if post_lexicon_items else None
    )
    post_lexicon_final_cer = (
        post_lexicon_final_distance / max(1, post_lexicon_chars) if post_lexicon_items else None
    )
    preview_cer = total_preview_distance / max(1, total_chars)
    online_final_cer = total_online_final_distance / max(1, total_chars)
    final_cer = total_final_distance / max(1, total_chars)
    return {
        "samples": len(items),
        # Existing keys remain available and now consistently mean raw model output.
        "preview_cer": preview_cer,
        "final_cer": final_cer,
        "preview_accuracy": 1.0 - preview_cer,
        "final_accuracy": 1.0 - final_cer,
        "edit_distance_preview": total_preview_distance,
        "edit_distance_final": total_final_distance,
        "online_final_cer": online_final_cer,
        "online_final_accuracy": 1.0 - online_final_cer,
        "edit_distance_online_final": total_online_final_distance,
        "raw_preview_cer": preview_cer,
        "raw_final_cer": final_cer,
        "raw_preview_accuracy": 1.0 - preview_cer,
        "raw_final_accuracy": 1.0 - final_cer,
        "post_lexicon_preview_cer": post_lexicon_preview_cer,
        "post_lexicon_final_cer": post_lexicon_final_cer,
        "post_lexicon_preview_accuracy": (
            1.0 - post_lexicon_preview_cer if post_lexicon_preview_cer is not None else None
        ),
        "post_lexicon_final_accuracy": (
            1.0 - post_lexicon_final_cer if post_lexicon_final_cer is not None else None
        ),
        "edit_distance_post_lexicon_preview": (
            post_lexicon_preview_distance if post_lexicon_items else None
        ),
        "edit_distance_post_lexicon_final": (
            post_lexicon_final_distance if post_lexicon_items else None
        ),
        "reference_chars": total_chars,
        "elapsed_seconds": sum(item.elapsed_seconds for item in items),
        "first_event_avg_seconds": (sum(first_events) / len(first_events)) if first_events else None,
        "ready_avg_seconds": (
            sum(ready_durations) / len(ready_durations) if ready_durations else None
        ),
        "ready_max_seconds": max(ready_durations) if ready_durations else None,
        "hotword_recall": (total_hotword_hits / total_hotwords) if total_hotwords else None,
        "online_hotword_recall": (
            total_online_hotword_hits / total_hotwords if total_hotwords else None
        ),
        "online_hotword_hits": total_online_hotword_hits,
        "hotword_hits": total_hotword_hits,
        "hotword_total": total_hotwords,
        "review_completed_samples": sum(
            1 for item in items if item.review_status == "completed"
        ),
        "review_fallback_samples": sum(
            1 for item in items if item.review_status in {"empty_fallback", "failed_fallback"}
        ),
    }


def suggest_lexicon_candidates(
    results: Iterable[StreamingEvaluationResult],
    *,
    max_items: int = 20,
) -> tuple[LexiconSuggestion, ...]:
    candidates: dict[tuple[str, str, str], set[str]] = {}
    for result in results:
        for stage, hypothesis in (
            ("preview", result.preview_hypothesis),
            ("final", result.final_hypothesis),
        ):
            for source, target in _diff_replacements(score_text(hypothesis), score_text(result.reference)):
                if not source or not target or source == target:
                    continue
                if len(source) > 16 or len(target) > 16:
                    continue
                candidates.setdefault((source, target, stage), set()).add(str(result.audio))
    suggestions = [
        LexiconSuggestion(
            source=source,
            target=target,
            stage=stage,
            count=len(audios),
            audios=tuple(sorted(audios)),
        )
        for (source, target, stage), audios in candidates.items()
    ]
    suggestions.sort(key=lambda item: (item.count, len(item.target), len(item.source)), reverse=True)
    return tuple(suggestions[:max_items])


def score_text(text: str) -> str:
    """Return text normalized for CER: no whitespace, punctuation or symbols."""

    chars: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char.isspace() or category.startswith("P") or category.startswith("S"):
            continue
        chars.append(char)
    return "".join(chars)


def _diff_replacements(hypothesis: str, reference: str) -> Iterable[tuple[str, str]]:
    matcher = difflib.SequenceMatcher(a=hypothesis, b=reference, autojunk=False)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag != "replace":
            continue
        yield hypothesis[left_start:left_end], reference[right_start:right_end]


def collect_hotwords(settings: AppSettings) -> tuple[str, ...]:
    if not settings.lexicon_enabled:
        return ()
    lexicons: list[Lexicon] = []
    bundled_paths = bundled_lexicon_paths(
        settings.enabled_builtin_lexicons,
        product_tier=settings.product_tier.strip() or None,
    )
    if bundled_paths:
        lexicons.append(Lexicon.from_files(bundled_paths))
    custom_path = settings.lexicon_path.strip()
    if custom_path:
        lexicons.append(Lexicon.from_file(custom_path, weight=1000))
    if not lexicons:
        return ()
    merged = lexicons[0].merge(*lexicons[1:])
    return merged.canonical_terms(product_tier=settings.product_tier.strip() or None, limit=200)


class FunASRFileStreamingTranscriber:
    """Run the same FunASR worker used by microphone streaming, fed from files."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        resource_mode: ResourcePolicyMode = ResourcePolicyMode.AUTO,
    ) -> None:
        self.settings = settings
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self.chunk_ms = max(40, int(settings.streaming_chunk_ms))
        self.update_interval_ms = max(40, int(settings.streaming_update_interval_ms))
        decision = SessionResourcePolicy().begin_session(
            capture_system_resource_snapshot(),
            mode=resource_mode,
        )
        self.num_threads = decision.online_threads
        self.review_num_threads = decision.review_threads
        self.release_online_before_review = decision.release_online_after_recording
        self.retain_review_worker = decision.retain_review_worker
        self._process: subprocess.Popen[str] | None = None
        self._output: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._ready = False
        self._review_engine: FunASROfflineEngine | None = None

    def transcribe_file(
        self,
        audio: Path,
        *,
        language: str | None = None,
        hotwords: Iterable[str] = (),
        realtime: bool = False,
    ) -> StreamingTranscript:
        source = Path(audio).expanduser()
        temp_context: tempfile.TemporaryDirectory[str] | None = None
        if requires_audio_transcode(source):
            temp_context = tempfile.TemporaryDirectory(prefix="voice-input-stream-eval-")
            try:
                source = transcode_audio_to_wav(source, Path(temp_context.name) / "input.wav")
            except Exception:
                temp_context.cleanup()
                raise
        events: list[StreamingTranscriptEvent] = []
        preview_text = ""
        online_final_text = ""
        first_event_seconds: float | None = None
        online_final_elapsed_seconds: float | None = None
        review_status = "not_configured"
        started = 0.0
        ready_started = time.perf_counter()
        ready_seconds: float | None = None
        try:
            self._ensure_ready(timeout=120.0)
            ready_seconds = time.perf_counter() - ready_started
            started = time.perf_counter()
            process = self._require_process()
            self._clear_pending_messages()
            normalized_hotwords = tuple(str(word).strip() for word in hotwords if str(word).strip())
            _write_worker_message(
                process,
                {
                    "type": "start_external",
                    "hotwords": list(normalized_hotwords),
                    "language": language,
                },
            )
            for pcm, frames in _iter_audio_chunks(
                source,
                sample_rate=self.sample_rate,
                chunk_ms=self.chunk_ms,
            ):
                _write_worker_message(
                    process,
                    {
                        "type": "audio",
                        "pcm": base64.b64encode(pcm).decode("ascii"),
                        "frames": frames,
                        "sample_rate": self.sample_rate,
                        "channels": self.channels,
                    },
                )
                preview_text, online_final_text, first_event_seconds = self._drain_session_messages(
                    started,
                    events,
                    preview_text,
                    online_final_text,
                    first_event_seconds,
                    stop_on_final=False,
                )
                if realtime:
                    time.sleep(frames / float(self.sample_rate))
            _write_worker_message(process, {"type": "stop"})
            deadline = time.monotonic() + max(120.0, float(self.settings.partial_transcription_interval_seconds) * 4.0 + 60.0)
            while time.monotonic() < deadline:
                preview_text, online_final_text, first_event_seconds = self._drain_session_messages(
                    started,
                    events,
                    preview_text,
                    online_final_text,
                    first_event_seconds,
                    stop_on_final=True,
                )
                if online_final_text:
                    break
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(_format_funasr_worker_failure(return_code, self._stderr_lines))
                time.sleep(0.03)
            if not online_final_text:
                raise TimeoutError("streaming evaluation timed out waiting for final text")
            online_final_elapsed_seconds = time.perf_counter() - started
            final_text = online_final_text or preview_text
            if self.settings.funasr_offline_model.strip():
                if self.release_online_before_review:
                    self._close_online_worker()
                final_text, review_status = self._review_file(
                    source,
                    language=language,
                    hotwords=normalized_hotwords,
                    fallback_text=final_text,
                )
        except Exception:
            self.close()
            raise
        finally:
            if temp_context is not None:
                temp_context.cleanup()
        return StreamingTranscript(
            preview_text=preview_text,
            final_text=final_text,
            events=tuple(events),
            first_event_seconds=first_event_seconds,
            elapsed_seconds=time.perf_counter() - started,
            online_final_text=online_final_text,
            online_final_elapsed_seconds=online_final_elapsed_seconds,
            review_status=review_status,
            ready_seconds=ready_seconds,
        )

    def close(self) -> None:
        self._close_online_worker()
        self._close_review_engine()

    def _close_online_worker(self) -> None:
        process = self._process
        self._process = None
        self._ready = False
        if process is None:
            return
        if process.poll() is None:
            _write_worker_message(process, {"type": "shutdown"})
            _terminate_process(process, graceful_timeout=5.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _review_file(
        self,
        audio: Path,
        *,
        language: str | None,
        hotwords: tuple[str, ...],
        fallback_text: str,
    ) -> tuple[str, str]:
        engine = self._review_engine
        if engine is None:
            engine = FunASROfflineEngine(
                model=self.settings.funasr_offline_model,
                vad_model=self.settings.funasr_vad_model,
                punc_model=self.settings.funasr_punc_model,
                spk_model=self.settings.funasr_speaker_model,
                num_threads=self.review_num_threads,
                file_chunk_seconds=60.0,
            )
            self._review_engine = engine
        try:
            result = engine.transcribe(
                audio,
                language=language,
                hotwords=hotwords,
                timeout_seconds=300.0,
            )
            reviewed = str(result.text).strip()
            if not reviewed:
                return fallback_text, "empty_fallback"
            return reviewed, "completed"
        except Exception:
            return fallback_text, "failed_fallback"
        finally:
            if not self.retain_review_worker:
                self._close_review_engine()

    def _close_review_engine(self) -> None:
        engine = self._review_engine
        self._review_engine = None
        if engine is not None:
            engine.close()

    def _ensure_ready(self, *, timeout: float) -> None:
        if self._ready and self._process is not None and self._process.poll() is None:
            return
        if self._process is None or self._process.poll() is not None:
            self.close()
            self._start_worker()
        process = self._require_process()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self._output.get(timeout=0.05)
            except queue.Empty:
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(_format_funasr_worker_failure(return_code, self._stderr_lines))
                continue
            kind = str(message.get("type", ""))
            if kind == "ready":
                self._ready = True
                return
            if kind == "error":
                raise RuntimeError(str(message.get("message", "FunASR worker failed.")))
        raise TimeoutError("FunASR streaming worker did not become ready in time")

    def _start_worker(self) -> None:
        creationflags = 0
        if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env["PYTHONIOENCODING"] = "utf-8"
        env["OMP_NUM_THREADS"] = str(self.num_threads)
        env["MKL_NUM_THREADS"] = str(self.num_threads)
        self._process = subprocess.Popen(
            _worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )
        self._output = queue.Queue()
        self._stderr_lines = []
        self._ready = False
        threading.Thread(
            target=_read_json_lines,
            args=(self._process.stdout, self._output),
            name="voice-input-stream-eval-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=_read_text_lines,
            args=(self._process.stderr, self._stderr_lines),
            name="voice-input-stream-eval-stderr",
            daemon=True,
        ).start()
        _write_worker_message(
            self._process,
            {
                "type": "prepare",
                "streaming_model": self.settings.funasr_streaming_model,
                "offline_model": "",
                "vad_model": "",
                "punc_model": "",
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "chunk_ms": self.chunk_ms,
                "update_interval_ms": self.update_interval_ms,
                "num_threads": self.num_threads,
                "device": "cpu",
            },
        )

    def _require_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is None:
            raise RuntimeError("FunASR streaming worker is not running")
        return process

    def _drain_session_messages(
        self,
        started: float,
        events: list[StreamingTranscriptEvent],
        preview_text: str,
        final_text: str,
        first_event_seconds: float | None,
        *,
        stop_on_final: bool,
    ) -> tuple[str, str, float | None]:
        while True:
            try:
                message = self._output.get_nowait()
            except queue.Empty:
                return preview_text, final_text, first_event_seconds
            kind = str(message.get("type", ""))
            if kind == "event":
                text = str(message.get("text", ""))
                elapsed = time.perf_counter() - started
                is_final = bool(message.get("is_final", False))
                events.append(
                    StreamingTranscriptEvent(
                        text=text,
                        is_final=is_final,
                        is_endpoint=bool(message.get("is_endpoint", False)),
                        elapsed_seconds=elapsed,
                    )
                )
                if first_event_seconds is None and text:
                    first_event_seconds = elapsed
                if is_final:
                    final_text = text
                    if stop_on_final:
                        return preview_text, final_text, first_event_seconds
                else:
                    preview_text = text
            elif kind == "final":
                final_text = str(message.get("text", "")) or final_text
                if stop_on_final:
                    return preview_text, final_text, first_event_seconds
            elif kind == "error":
                raise RuntimeError(str(message.get("message", "FunASR worker failed.")))

    def _clear_pending_messages(self) -> None:
        while True:
            try:
                self._output.get_nowait()
            except queue.Empty:
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate streaming ASR accuracy with referenced audio samples.")
    parser.add_argument("samples", help="CSV/TSV/JSONL file with audio and reference fields")
    parser.add_argument("--settings", help="Optional settings.json path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--realtime", action="store_true", help="Feed audio at real-time speed for latency checks")
    parser.add_argument(
        "--resource-mode",
        choices=tuple(mode.value for mode in ResourcePolicyMode),
        default=ResourcePolicyMode.AUTO.value,
        help="Resource policy used by the online/review workers (default: auto)",
    )
    parser.add_argument("--output", help="Write the report to a UTF-8 file instead of stdout")
    parser.add_argument("--suggestions-output", help="Write lexicon suggestions to a UTF-8 CSV file")
    args = parser.parse_args(argv)

    settings = SettingsStore(Path(args.settings)).load() if args.settings else resolve_runtime_settings(AppSettings())
    samples = load_streaming_samples(args.samples)
    transcriber_factory: TranscriberFactory | None = None
    if args.resource_mode != ResourcePolicyMode.AUTO.value:
        selected_resource_mode = ResourcePolicyMode(args.resource_mode)

        def build_transcriber(resolved: AppSettings) -> FunASRFileStreamingTranscriber:
            return FunASRFileStreamingTranscriber(
                resolved,
                resource_mode=selected_resource_mode,
            )

        transcriber_factory = build_transcriber
    results = evaluate_streaming_samples(
        samples,
        settings,
        realtime=args.realtime,
        transcriber_factory=transcriber_factory,
    )
    summary = summarize_streaming_results(results)
    suggestions = suggest_lexicon_candidates(results)
    if args.suggestions_output:
        _write_suggestions_csv(Path(args.suggestions_output), suggestions)

    if args.json:
        return _emit_report(
            json.dumps(
                {
                    "summary": summary,
                    "results": [_result_to_json(result) for result in results],
                    "lexicon_suggestions": [_suggestion_to_json(item) for item in suggestions],
                },
                ensure_ascii=False,
                indent=2,
            ),
            output=args.output,
        )
    lines = [
        f"samples={summary['samples']}",
        f"preview_cer={summary['preview_cer']:.4f}",
        f"online_final_cer={summary['online_final_cer']:.4f}",
        f"final_cer={summary['final_cer']:.4f}",
        f"raw_preview_cer={summary['raw_preview_cer']:.4f}",
        f"raw_final_cer={summary['raw_final_cer']:.4f}",
        f"post_lexicon_preview_cer={_format_optional_float(summary['post_lexicon_preview_cer'])}",
        f"post_lexicon_final_cer={_format_optional_float(summary['post_lexicon_final_cer'])}",
        f"preview_accuracy={summary['preview_accuracy']:.4f}",
        f"final_accuracy={summary['final_accuracy']:.4f}",
        f"hotword_recall={_format_optional_float(summary['hotword_recall'])}",
        f"online_hotword_recall={_format_optional_float(summary['online_hotword_recall'])}",
        f"review_completed_samples={summary['review_completed_samples']}",
        f"review_fallback_samples={summary['review_fallback_samples']}",
        f"first_event_avg_seconds={_format_optional_float(summary['first_event_avg_seconds'])}",
        f"ready_avg_seconds={_format_optional_float(summary['ready_avg_seconds'])}",
        f"ready_max_seconds={_format_optional_float(summary['ready_max_seconds'])}",
        f"elapsed_seconds={summary['elapsed_seconds']:.2f}",
    ]
    for suggestion in suggestions[:10]:
        lines.append(
            f"lexicon_suggestion={suggestion.source}=>{suggestion.target}"
            f"\tstage={suggestion.stage}\tcount={suggestion.count}"
        )
    for result in results:
        lines.append(
            f"{result.audio}\tpreview_cer={result.preview_cer:.4f}"
            f"\tonline_final_cer={_format_optional_float(result.online_final_cer)}"
            f"\tfinal_cer={result.final_cer:.4f}"
            f"\tpost_lexicon_preview_cer={_format_optional_float(result.post_lexicon_preview_cer)}"
            f"\tpost_lexicon_final_cer={_format_optional_float(result.post_lexicon_final_cer)}"
            f"\tevents={result.events}"
        )
    return _emit_report("\n".join(lines), output=args.output)


def _emit_report(payload: str, *, output: str | None) -> int:
    text = payload.rstrip() + "\n"
    if output:
        Path(output).expanduser().write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def _write_suggestions_csv(path: Path, suggestions: Iterable[LexiconSuggestion]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "source",
                "target",
                "rule_type",
                "product_tiers",
                "weight",
                "enabled",
                "case_sensitive",
                "category",
                "domain",
                "usage",
                "confidence",
                "examples",
                "aliases",
                "stage",
                "count",
                "audios",
            ),
        )
        writer.writeheader()
        for suggestion in suggestions:
            safe_auto_rule = _is_safe_auto_lexicon_suggestion(suggestion)
            writer.writerow(
                {
                    "source": suggestion.source,
                    "target": suggestion.target,
                    "rule_type": LexiconRuleType.REPLACE.value,
                    "product_tiers": "",
                    "weight": 100 if safe_auto_rule else 10,
                    "enabled": "true" if safe_auto_rule else "false",
                    "case_sensitive": "true",
                    "category": "evaluation",
                    "domain": "custom",
                    "usage": "correction" if safe_auto_rule else "review_required",
                    "confidence": "0.80" if safe_auto_rule else "0.40",
                    "examples": "",
                    "aliases": "",
                    "stage": suggestion.stage,
                    "count": suggestion.count,
                    "audios": ";".join(suggestion.audios),
                }
            )


def _is_safe_auto_lexicon_suggestion(suggestion: LexiconSuggestion) -> bool:
    return len(suggestion.source) >= 2 and len(suggestion.target) >= 2


def _iter_audio_chunks(path: Path, *, sample_rate: int, chunk_ms: int) -> Iterable[tuple[bytes, int]]:
    import numpy as np
    import soundfile as sf

    source = Path(path).expanduser()
    temp_context: tempfile.TemporaryDirectory[str] | None = None
    try:
        if requires_audio_transcode(source):
            temp_context = tempfile.TemporaryDirectory(prefix="voice-input-stream-eval-")
            source = transcode_audio_to_wav(source, Path(temp_context.name) / "input.wav")
        samples, source_rate = sf.read(str(source), dtype="float32", always_2d=True)
        mono = np.asarray(samples[:, 0], dtype=np.float32)
        if int(source_rate) != int(sample_rate):
            mono = _resample_mono(mono, int(source_rate), int(sample_rate), np)
        mono = np.clip(mono, -1.0, 1.0)
        int_samples = (mono * 32767.0).astype(np.int16)
        frames_per_chunk = max(1, int(sample_rate * max(1, chunk_ms) / 1000))
        for offset in range(0, int_samples.size, frames_per_chunk):
            chunk = int_samples[offset : offset + frames_per_chunk]
            if chunk.size:
                yield chunk.tobytes(), int(chunk.size)
    finally:
        if temp_context is not None:
            temp_context.cleanup()


def _resample_mono(samples: Any, source_rate: int, target_rate: int, np: Any) -> Any:
    if samples.size == 0 or source_rate == target_rate:
        return samples
    try:
        import soxr

        return soxr.resample(samples, source_rate, target_rate, quality="HQ").astype(np.float32)
    except Exception:
        target_length = max(1, int(round(samples.size * target_rate / float(source_rate))))
        source_positions = np.linspace(0.0, float(samples.size - 1), num=samples.size)
        target_positions = np.linspace(0.0, float(samples.size - 1), num=target_length)
        return np.interp(target_positions, source_positions, samples).astype(np.float32)


def _load_delimited_samples(path: Path, *, delimiter: str) -> Iterable[StreamingEvaluationSample]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError("sample file must include a header row")
        _validate_sample_fields(reader.fieldnames)
        for row in reader:
            yield _sample_from_mapping(row, base_dir=path.parent)


def _load_jsonl_samples(path: Path) -> Iterable[StreamingEvaluationSample]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError(f"jsonl line {line_number} must be an object")
            yield _sample_from_mapping(payload, base_dir=path.parent)


def _validate_sample_fields(fields: Iterable[str]) -> None:
    names = {field.strip().lower() for field in fields}
    missing = {"audio", "reference"} - names
    if missing:
        raise ValueError(f"sample file is missing required columns: {', '.join(sorted(missing))}")


def _sample_from_mapping(payload: dict[str, object], *, base_dir: Path) -> StreamingEvaluationSample:
    audio = Path(str(payload.get("audio", "")).strip())
    reference = str(payload.get("reference", "")).strip()
    language = str(payload.get("language", "")).strip() or None
    hotwords = _parse_hotwords(payload.get("hotwords", ()))
    if not audio:
        raise ValueError("sample is missing audio path")
    if not reference:
        raise ValueError(f"sample {audio} is missing reference text")
    if not audio.is_absolute():
        audio = base_dir / audio
    if not audio.is_file():
        raise ValueError(f"sample audio file not found: {audio}")
    return StreamingEvaluationSample(audio=audio, reference=reference, language=language, hotwords=hotwords)


def _parse_hotwords(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return _merge_words(str(item) for item in value)
    text = str(value).strip()
    if not text:
        return ()
    for separator in (";", "；", "|", "，", ","):
        text = text.replace(separator, "\n")
    return _merge_words(item.strip() for item in text.splitlines())


def _merge_words(*groups: Iterable[str]) -> tuple[str, ...]:
    words: list[str] = []
    for group in groups:
        for item in group:
            word = str(item).strip()
            if word and word not in words:
                words.append(word)
    return tuple(words)


def _tracked_hotwords(
    sample: StreamingEvaluationSample,
    reference: str,
    hotwords: Iterable[str],
) -> tuple[str, ...]:
    if sample.hotwords:
        return sample.hotwords
    return tuple(word for word in hotwords if word and word in reference)


def _result_to_json(result: StreamingEvaluationResult) -> dict[str, object]:
    return {
        "audio": str(result.audio),
        "reference": result.reference,
        "preview_hypothesis": result.preview_hypothesis,
        "final_hypothesis": result.final_hypothesis,
        "preview_cer": result.preview_cer,
        "final_cer": result.final_cer,
        "preview_edit_distance": result.preview_edit_distance,
        "final_edit_distance": result.final_edit_distance,
        "raw_preview_hypothesis": result.preview_hypothesis,
        "raw_final_hypothesis": result.final_hypothesis,
        "raw_preview_cer": result.preview_cer,
        "raw_final_cer": result.final_cer,
        "raw_preview_edit_distance": result.preview_edit_distance,
        "raw_final_edit_distance": result.final_edit_distance,
        "online_final_hypothesis": result.online_final_hypothesis,
        "online_final_cer": result.online_final_cer,
        "online_final_edit_distance": result.online_final_edit_distance,
        "online_final_elapsed_seconds": result.online_final_elapsed_seconds,
        "review_final_hypothesis": result.final_hypothesis,
        "review_final_cer": result.final_cer,
        "review_final_edit_distance": result.final_edit_distance,
        "review_final_elapsed_seconds": result.elapsed_seconds,
        "review_status": result.review_status,
        "ready_seconds": result.ready_seconds,
        "post_lexicon_preview_hypothesis": result.post_lexicon_preview_hypothesis,
        "post_lexicon_final_hypothesis": result.post_lexicon_final_hypothesis,
        "post_lexicon_preview_cer": result.post_lexicon_preview_cer,
        "post_lexicon_final_cer": result.post_lexicon_final_cer,
        "post_lexicon_preview_edit_distance": result.post_lexicon_preview_edit_distance,
        "post_lexicon_final_edit_distance": result.post_lexicon_final_edit_distance,
        "reference_chars": result.reference_chars,
        "elapsed_seconds": result.elapsed_seconds,
        "first_event_seconds": result.first_event_seconds,
        "events": result.events,
        "hotword_hits": result.hotword_hits,
        "online_hotword_hits": result.online_hotword_hits,
        "hotword_total": result.hotword_total,
    }


def _suggestion_to_json(suggestion: LexiconSuggestion) -> dict[str, object]:
    return {
        "source": suggestion.source,
        "target": suggestion.target,
        "stage": suggestion.stage,
        "count": suggestion.count,
        "audios": list(suggestion.audios),
    }


def _format_optional_float(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
