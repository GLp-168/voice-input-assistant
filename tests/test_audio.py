from __future__ import annotations

from array import array
import sys
from types import ModuleType

import pytest

from voice_input.audio import (
    AudioCaptureError,
    AudioLevelMeter,
    DiskBackedPCM,
    MicrophoneDevice,
    MicrophoneRecorder,
    calculate_audio_level,
    resolve_microphone_device,
)


def _pcm16(*samples: int) -> bytes:
    values = array("h", samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def test_calculate_audio_level_maps_silence_and_full_scale() -> None:
    silence = calculate_audio_level(_pcm16(0, 0, 0, 0))
    loud = calculate_audio_level(_pcm16(32767, -32767, 32767, -32767))

    assert silence.level == 0.0
    assert silence.rms_dbfs == -96.0
    assert loud.level == pytest.approx(1.0)
    assert loud.rms == pytest.approx(1.0)
    assert loud.clipping is True


def test_audio_level_meter_throttles_capture_blocks() -> None:
    meter = AudioLevelMeter(minimum_interval_seconds=0.08)
    pcm = _pcm16(1000, -1000)

    assert meter.process(pcm, now=1.0) is not None
    assert meter.process(pcm, now=1.05) is None
    assert meter.process(pcm, now=1.081) is not None


def test_microphone_stable_id_resolves_after_device_reordering() -> None:
    saved = MicrophoneDevice(index=7, name="Conference microphone", max_input_channels=1, hostapi=2)
    reordered = MicrophoneDevice(index=3, name="Conference microphone", max_input_channels=1, hostapi=2)

    assert saved.stable_id == "2:Conference microphone"
    assert resolve_microphone_device(saved.stable_id, [reordered]) == 3
    assert resolve_microphone_device("9:Missing", [reordered]) is None


def test_microphone_recorder_reports_level_and_device_state(monkeypatch) -> None:
    class CallbackStop(Exception):
        pass

    class FakeInputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            try:
                self.callback(_pcm16(12000, -12000, 12000, -12000), 4, None, None)
            except CallbackStop:
                pass
            return self

        def __exit__(self, *_args):
            return None

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.CallbackStop = CallbackStop
    fake_sounddevice.InputStream = FakeInputStream
    fake_sounddevice.query_devices = lambda *_args: {"name": "Test microphone", "index": 7}
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    recorder = MicrophoneRecorder(max_duration_seconds=0.004, sample_rate=1000)
    levels = []
    states = []
    recorder.set_audio_monitor(levels.append, states.append)

    chunk = recorder.record_until_stopped()

    assert chunk.pcm == _pcm16(12000, -12000, 12000, -12000)
    assert [state.status for state in states] == ["opening", "active", "idle"]
    assert states[0].name == "Test microphone"
    assert levels[0].level > 0.0
    assert levels[-1].level == 0.0


def test_microphone_recorder_reports_device_open_failure(monkeypatch) -> None:
    class FailingInputStream:
        def __init__(self, **_kwargs):
            raise RuntimeError("microphone permission denied")

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.InputStream = FailingInputStream
    fake_sounddevice.query_devices = lambda *_args: {"name": "Blocked microphone"}
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    recorder = MicrophoneRecorder(max_duration_seconds=1)
    states = []
    recorder.set_audio_monitor(None, states.append)

    with pytest.raises(AudioCaptureError, match="permission denied"):
        recorder.record_until_stopped()

    assert [state.status for state in states] == ["opening", "error"]
    assert states[-1].name == "Blocked microphone"
    assert "permission denied" in states[-1].message


def test_microphone_recorder_spills_long_capture_to_disk_and_cleans_up(monkeypatch) -> None:
    class CallbackStop(Exception):
        pass

    class FakeInputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            try:
                self.callback(b"12345678", 4, None, None)
            except CallbackStop:
                pass
            return self

        def __exit__(self, *_args):
            return None

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.CallbackStop = CallbackStop
    fake_sounddevice.InputStream = FakeInputStream
    fake_sounddevice.query_devices = lambda *_args: {"name": "Test microphone"}
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    recorder = MicrophoneRecorder(
        max_duration_seconds=0.004,
        sample_rate=1000,
        memory_limit_bytes=1,
    )
    chunk = recorder.record_until_stopped()

    assert isinstance(chunk.pcm, DiskBackedPCM)
    assert len(chunk.pcm) == 8
    assert bytes(chunk.pcm) == b"12345678"
    temp_path = chunk.pcm.path
    assert temp_path.suffix == ".wav"
    assert temp_path.is_file()
    assert not hasattr(recorder, "_frames")

    chunk.close()
    assert not temp_path.exists()


def test_microphone_recorder_snapshots_read_only_new_disk_bytes(monkeypatch) -> None:
    class CallbackStop(Exception):
        pass

    snapshots = []

    class FakeInputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            self.callback(b"first", 3, None, None)
            snapshots.append(recorder.snapshot_since_last())
            try:
                self.callback(b"second", 3, None, None)
            except CallbackStop:
                pass
            return self

        def __exit__(self, *_args):
            return None

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.CallbackStop = CallbackStop
    fake_sounddevice.InputStream = FakeInputStream
    fake_sounddevice.query_devices = lambda *_args: {"name": "Test microphone"}
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    recorder = MicrophoneRecorder(max_duration_seconds=0.006, sample_rate=1000)
    chunk = recorder.record_until_stopped()
    final_snapshot = recorder.snapshot_since_last()

    assert snapshots[0] is not None
    assert snapshots[0].pcm == b"first"
    assert final_snapshot is not None
    assert final_snapshot.pcm == b"second"
    assert chunk.pcm == b"firstsecond"
