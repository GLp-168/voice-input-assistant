from __future__ import annotations

from types import ModuleType
import sys
import threading

import numpy as np

from voice_input.resources import SHERPA_STREAMING_CTC_MODEL_FILES, SHERPA_STREAMING_TRANSDUCER_MODEL_FILES
from voice_input.streaming import SherpaOnnxStreamingRecognizer, _format_hotwords, _to_float32_mono


def test_streaming_recognizer_reports_missing_model_unavailable(tmp_path):
    recognizer = SherpaOnnxStreamingRecognizer(tmp_path / "missing")

    assert recognizer.is_available() is False


def test_streaming_audio_blocks_are_normalized_to_mono_float32():
    block = np.array([[0, 10], [16384, 20], [-32768, 30]], dtype=np.int16)

    samples = _to_float32_mono(block, np)

    assert samples.dtype == np.float32
    assert samples.tolist() == [0.0, 0.5, -1.0]


def test_streaming_hotwords_are_deduplicated_and_trimmed():
    assert (
        _format_hotwords(
            [
                " \u653f\u52a1\u670d\u52a1 ",
                "",
                "\u4f1a\u8bae\u7eaa\u8981",
                "\u653f\u52a1\u670d\u52a1",
            ]
        )
        == "\u653f \u52a1 \u670d \u52a1\n\u4f1a \u8bae \u7eaa \u8981"
    )


def test_streaming_hotwords_keep_ascii_runs_and_split_cjk():
    assert _format_hotwords(["OpenAI", "A\u80a1\u4f1a\u8bae"]) == "OpenAI\nA \u80a1 \u4f1a \u8bae"


def test_streaming_opens_microphone_before_loading_recognizer(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    for filename in SHERPA_STREAMING_CTC_MODEL_FILES:
        (model_dir / filename).write_text("model", encoding="utf-8")

    order: list[str] = []

    class CallbackStop(Exception):
        pass

    class FakeInputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            order.append("input_enter")
            try:
                self.callback(np.array([0, 100], dtype=np.int16), 100, None, None)
            except CallbackStop:
                pass
            return self

        def __exit__(self, *_args):
            order.append("input_exit")
            return None

    class FakeStream:
        def __init__(self):
            self.finished = False

        def accept_waveform(self, _sample_rate, _samples):
            order.append("accepted_audio")

        def input_finished(self):
            self.finished = True

    class FakeRecognizer:
        def create_stream(self, *_args):
            return FakeStream()

        def is_ready(self, _stream):
            return False

        def decode_stream(self, _stream):
            return None

        def get_result(self, stream):
            return "\u4f60" if stream.finished else ""

        def is_endpoint(self, _stream):
            return False

        def reset(self, _stream):
            return None

    recognizer_kwargs: dict[str, object] = {}

    class FakeOnlineRecognizer:
        @staticmethod
        def from_zipformer2_ctc(**kwargs):
            recognizer_kwargs.update(kwargs)
            order.append("recognizer_created")
            return FakeRecognizer()

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.CallbackStop = CallbackStop
    fake_sounddevice.InputStream = FakeInputStream
    fake_sherpa = ModuleType("sherpa_onnx")
    fake_sherpa.OnlineRecognizer = FakeOnlineRecognizer
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake_sherpa)

    recognizer = SherpaOnnxStreamingRecognizer(model_dir, sample_rate=1000)
    result = recognizer.transcribe_microphone(
        max_duration_seconds=0.1,
        stop_event=threading.Event(),
        pause_event=threading.Event(),
        on_event=lambda _event: None,
    )

    assert order.index("input_enter") < order.index("recognizer_created")
    assert "accepted_audio" in order
    assert recognizer_kwargs["decoding_method"] == "greedy_search"
    assert str(recognizer_kwargs["model"]).endswith("model.int8.onnx")
    assert result == "\u4f60"


def test_streaming_transducer_model_still_uses_modified_beam_search(tmp_path):
    model_dir = tmp_path / "transducer"
    model_dir.mkdir()
    for filename in SHERPA_STREAMING_TRANSDUCER_MODEL_FILES:
        (model_dir / filename).write_text("model", encoding="utf-8")

    recognizer_kwargs: dict[str, object] = {}

    class FakeOnlineRecognizer:
        @staticmethod
        def from_transducer(**kwargs):
            recognizer_kwargs.update(kwargs)
            return object()

    fake_sherpa = ModuleType("sherpa_onnx")
    fake_sherpa.OnlineRecognizer = FakeOnlineRecognizer

    recognizer = SherpaOnnxStreamingRecognizer(model_dir)
    recognizer._create_recognizer(fake_sherpa)

    assert recognizer.supports_hotwords is True
    assert recognizer_kwargs["decoding_method"] == "modified_beam_search"
    assert str(recognizer_kwargs["encoder"]).endswith("encoder-epoch-99-avg-1.int8.onnx")
