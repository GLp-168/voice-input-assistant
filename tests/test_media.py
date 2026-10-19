from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from voice_input.config import DIRECT_AUDIO_FILE_EXTENSIONS, SUPPORTED_AUDIO_FILE_EXTENSIONS
from voice_input.media import (
    AudioTranscodeCancelled,
    AudioTranscodeError,
    AudioTranscodeUnavailable,
    direct_audio_format_names,
    find_ffmpeg_executable,
    is_supported_audio_file,
    probe_audio_duration_seconds,
    requires_audio_transcode,
    transcode_audio_to_wav,
)


def test_audio_extension_sets_cover_direct_and_transcoded_formats() -> None:
    assert ".mp3" not in DIRECT_AUDIO_FILE_EXTENSIONS
    assert ".mp3" in SUPPORTED_AUDIO_FILE_EXTENSIONS
    assert ".m4a" in SUPPORTED_AUDIO_FILE_EXTENSIONS
    assert ".wma" in SUPPORTED_AUDIO_FILE_EXTENSIONS
    assert ".mp4" in SUPPORTED_AUDIO_FILE_EXTENSIONS
    assert is_supported_audio_file("meeting.webm")
    assert requires_audio_transcode("meeting.m4a")
    assert requires_audio_transcode("meeting.mp3")
    assert "MP3" not in direct_audio_format_names()


def test_find_ffmpeg_prefers_environment_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"exe")
    monkeypatch.setenv("VOICE_INPUT_FFMPEG", str(executable))
    monkeypatch.setattr("voice_input.media.shutil.which", lambda _name: None)

    assert find_ffmpeg_executable() == str(executable)


def test_transcode_audio_to_wav_invokes_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "meeting.m4a"
    target = tmp_path / "meeting.wav"
    source.write_bytes(b"audio")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        target.write_bytes(b"wav")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("voice_input.media.find_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr("voice_input.media.subprocess.run", fake_run)

    assert transcode_audio_to_wav(source, target) == target
    assert calls[0][:5] == ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i"]
    assert "-vn" in calls[0]
    assert ["-ac", "1"] == calls[0][calls[0].index("-ac") : calls[0].index("-ac") + 2]
    assert ["-ar", "16000"] == calls[0][calls[0].index("-ar") : calls[0].index("-ar") + 2]


def test_probe_audio_duration_uses_ffmpeg_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "meeting.mp3"
    source.write_bytes(b"audio")

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Duration: 02:03:04.50, start: 0.000000, bitrate: 128 kb/s",
        )

    monkeypatch.setattr("voice_input.media.find_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr("voice_input.media.subprocess.run", fake_run)

    assert probe_audio_duration_seconds(source) == pytest.approx(7384.5)


def test_transcode_audio_to_wav_requires_converter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "meeting.aac"
    target = tmp_path / "meeting.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("voice_input.media.find_ffmpeg_executable", lambda: None)

    with pytest.raises(AudioTranscodeUnavailable):
        transcode_audio_to_wav(source, target)


def test_transcode_audio_to_wav_surfaces_conversion_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "meeting.aac"
    target = tmp_path / "meeting.wav"
    source.write_bytes(b"audio")

    monkeypatch.setattr("voice_input.media.find_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(
        "voice_input.media.subprocess.run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr="bad input"),
    )

    with pytest.raises(AudioTranscodeError, match="bad input"):
        transcode_audio_to_wav(source, target)


def test_transcode_audio_to_wav_enforces_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "meeting.aac"
    target = tmp_path / "meeting.wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr("voice_input.media.find_ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(
        "voice_input.media.subprocess.run",
        lambda command, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(command, 1)),
    )

    with pytest.raises(AudioTranscodeError, match="转码超时"):
        transcode_audio_to_wav(source, target, timeout_seconds=1)


def test_cancellable_transcode_terminates_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self, timeout=None):
            raise AssertionError("cancelled process should not be awaited")

    process = FakeProcess()
    monkeypatch.setattr("voice_input.media.subprocess.Popen", lambda *_args, **_kwargs: process)
    cancel_event = __import__("threading").Event()
    cancel_event.set()

    with pytest.raises(AudioTranscodeCancelled):
        __import__("voice_input.media", fromlist=["_run_cancellable_transcode"])._run_cancellable_transcode(
            ["ffmpeg"], timeout_seconds=60, cancel_event=cancel_event
        )

    assert process.returncode == -15
