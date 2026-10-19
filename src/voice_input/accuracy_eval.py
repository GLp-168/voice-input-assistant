"""Offline ASR accuracy evaluation utilities."""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .app import build_asr_engine, extract_text, normalize_recognition_text, resolve_runtime_settings
from .media import probe_audio_duration_seconds
from .settings import AppSettings, SettingsStore


DEFAULT_EVALUATION_TIMEOUT_SECONDS = 30.0 * 60.0


@dataclass(frozen=True, slots=True)
class EvaluationSample:
    audio: Path
    reference: str
    language: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    audio: Path
    reference: str
    hypothesis: str
    cer: float
    edit_distance: int
    reference_chars: int
    elapsed_seconds: float


def load_samples(path: str | Path) -> tuple[EvaluationSample, ...]:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".jsonl":
        return tuple(_load_jsonl_samples(source))
    if suffix in {".csv", ".tsv"}:
        return tuple(_load_delimited_samples(source, delimiter="\t" if suffix == ".tsv" else ","))
    raise ValueError("sample file must be .csv, .tsv, or .jsonl")


def evaluate_samples(
    samples: Iterable[EvaluationSample],
    settings: AppSettings,
    *,
    timeout_seconds: float | None = None,
) -> tuple[EvaluationResult, ...]:
    runtime_settings = resolve_runtime_settings(settings)
    engine = build_asr_engine(runtime_settings)
    results: list[EvaluationResult] = []

    for sample in samples:
        sample_settings = runtime_settings
        if sample.language:
            sample_settings = AppSettings.from_dict({**runtime_settings.to_dict(), "recognition_language": sample.language})
        started = time.perf_counter()
        sample_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else _evaluation_timeout_seconds(sample.audio)
        )
        raw_result = engine.transcribe(
            sample.audio,
            language=_engine_language(sample_settings),
            timeout_seconds=sample_timeout,
        )
        elapsed = time.perf_counter() - started
        hypothesis = normalize_recognition_text(extract_text(raw_result), sample_settings)
        reference = normalize_recognition_text(sample.reference, sample_settings)
        scored_reference = _score_text(reference)
        scored_hypothesis = _score_text(hypothesis)
        distance = levenshtein_distance(scored_reference, scored_hypothesis)
        denominator = max(1, len(scored_reference))
        results.append(
            EvaluationResult(
                audio=sample.audio,
                reference=reference,
                hypothesis=hypothesis,
                cer=distance / denominator,
                edit_distance=distance,
                reference_chars=len(scored_reference),
                elapsed_seconds=elapsed,
            )
        )
    return tuple(results)


def summarize_results(results: Iterable[EvaluationResult]) -> dict[str, float | int]:
    items = tuple(results)
    total_distance = sum(item.edit_distance for item in items)
    total_chars = sum(item.reference_chars for item in items)
    total_elapsed = sum(item.elapsed_seconds for item in items)
    return {
        "samples": len(items),
        "cer": total_distance / max(1, total_chars),
        "edit_distance": total_distance,
        "reference_chars": total_chars,
        "elapsed_seconds": total_elapsed,
    }


def levenshtein_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + cost,
                )
            )
        previous = current
    return previous[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate offline ASR accuracy with referenced audio samples.")
    parser.add_argument("samples", help="CSV/TSV/JSONL file with audio and reference fields")
    parser.add_argument("--settings", help="Optional settings.json path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--output", help="Write the report to a UTF-8 file instead of stdout")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_seconds,
        help="Maximum recognition time per sample; defaults to an audio-duration-based limit",
    )
    args = parser.parse_args(argv)

    settings = SettingsStore(Path(args.settings)).load() if args.settings else resolve_runtime_settings(AppSettings())
    samples = load_samples(args.samples)
    results = evaluate_samples(samples, settings, timeout_seconds=args.timeout_seconds)
    summary = summarize_results(results)

    if args.json:
        return _emit_report(
            json.dumps(
                {
                    "summary": summary,
                    "results": [
                        {
                            "audio": str(result.audio),
                            "reference": result.reference,
                            "hypothesis": result.hypothesis,
                            "cer": result.cer,
                            "edit_distance": result.edit_distance,
                            "reference_chars": result.reference_chars,
                            "elapsed_seconds": result.elapsed_seconds,
                        }
                        for result in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            output=args.output,
        )
    lines = [
        f"samples={summary['samples']}",
        f"cer={summary['cer']:.4f}",
        f"edit_distance={summary['edit_distance']}",
        f"reference_chars={summary['reference_chars']}",
        f"elapsed_seconds={summary['elapsed_seconds']:.2f}",
    ]
    for result in results:
        lines.append(f"{result.audio}\tcer={result.cer:.4f}\telapsed={result.elapsed_seconds:.2f}s")
    return _emit_report("\n".join(lines), output=args.output)


def _emit_report(payload: str, *, output: str | None) -> int:
    text = payload.rstrip() + "\n"
    if output:
        Path(output).expanduser().write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def _load_delimited_samples(path: Path, *, delimiter: str) -> Iterable[EvaluationSample]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError("sample file must include a header row")
        _validate_sample_fields(reader.fieldnames)
        for row in reader:
            yield _sample_from_mapping(row, base_dir=path.parent)


def _load_jsonl_samples(path: Path) -> Iterable[EvaluationSample]:
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


def _sample_from_mapping(payload: dict[str, object], *, base_dir: Path) -> EvaluationSample:
    audio = Path(str(payload.get("audio", "")).strip())
    reference = str(payload.get("reference", "")).strip()
    language = str(payload.get("language", "")).strip() or None
    if not audio:
        raise ValueError("sample is missing audio path")
    if not reference:
        raise ValueError(f"sample {audio} is missing reference text")
    if not audio.is_absolute():
        audio = base_dir / audio
    if not audio.is_file():
        raise ValueError(f"sample audio file not found: {audio}")
    return EvaluationSample(audio=audio, reference=reference, language=language)


def _engine_language(settings: AppSettings) -> str | None:
    from .language import engine_language_code

    return engine_language_code(settings.recognition_language)


def _evaluation_timeout_seconds(audio_path: Path) -> float:
    duration = probe_audio_duration_seconds(audio_path)
    if duration is None:
        return DEFAULT_EVALUATION_TIMEOUT_SECONDS
    return max(120.0, duration * 4.0 + 60.0)


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return seconds


def _score_text(text: str) -> str:
    ignored = set(" \t\r\n，。！？；：、,.!?;:\"'“”‘’（）()[]【】<>《》")
    return "".join(char for char in text if char not in ignored)


if __name__ == "__main__":
    raise SystemExit(main())
