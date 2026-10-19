from __future__ import annotations

import sys
from types import ModuleType

from voice_input.asr import FunASROfflineEngine
from voice_input.audio import AudioChunk


def test_funasr_offline_engine_extracts_sentence_info_segments(monkeypatch) -> None:
    class FakeAutoModel:
        def __init__(self, **_kwargs) -> None:
            pass

        def generate(self, **_kwargs):
            return [
                {
                    "text": "first second",
                    "sentence_info": [
                        {"text": "first", "start": 100, "end": 350, "spk": 0},
                        {
                            "text": "second",
                            "start": 350,
                            "end": 725,
                            "speaker": "speaker-a",
                            "timestamp": [[350, 500], [500, 725]],
                        },
                    ],
                }
            ]

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    result = FunASROfflineEngine(model="paraformer-zh").transcribe(
        AudioChunk(pcm=b"\x00" * 3200)
    )

    assert result.segments == (
        {"text": "first", "start": 0.1, "end": 0.35, "spk": 0, "time_unit": "seconds"},
        {
            "text": "second",
            "start": 0.35,
            "end": 0.725,
            "speaker": "speaker-a",
            "timestamp": [[0.35, 0.5], [0.5, 0.725]],
            "time_unit": "seconds",
        },
    )


def test_funasr_offline_engine_derives_exact_bounds_from_timestamp(monkeypatch) -> None:
    class FakeAutoModel:
        def __init__(self, **_kwargs) -> None:
            pass

        def generate(self, **_kwargs):
            return [
                {
                    "text": "sentence",
                    "sentence_info": [{"text": "sentence", "start": 1, "end": 10}],
                },
                {"text": "timed text", "timestamp": [[12, 34], [40, 78]], "spk": 2},
            ]

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    result = FunASROfflineEngine(model="paraformer-zh").transcribe(
        AudioChunk(pcm=b"\x00" * 3200)
    )

    assert result.segments == (
        {"text": "sentence", "start": 0.001, "end": 0.01, "time_unit": "seconds"},
        {
            "text": "timed text",
            "start": 0.012,
            "end": 0.078,
            "spk": 2,
            "timestamp": [[0.012, 0.034], [0.04, 0.078]],
            "time_unit": "seconds",
        },
    )
