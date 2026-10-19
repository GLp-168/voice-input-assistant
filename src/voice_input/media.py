"""Audio file compatibility helpers for imported media."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from .config import (
    DEFAULT_SAMPLE_RATE,
    DIRECT_AUDIO_FILE_EXTENSIONS,
    SUPPORTED_AUDIO_FILE_EXTENSIONS,
    TRANSCODABLE_AUDIO_FILE_EXTENSIONS,
)
from .resources import app_base_dir


class AudioMediaError(RuntimeError):
    """Base error for imported audio preparation."""


class AudioTranscodeUnavailable(AudioMediaError):
    """Raised when a file needs transcoding but no converter is available."""


class AudioTranscodeError(AudioMediaError):
    """Raised when audio conversion fails."""


class AudioTranscodeCancelled(AudioMediaError):
    """Raised when the caller cancels an active audio conversion."""


def is_supported_audio_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_AUDIO_FILE_EXTENSIONS


def requires_audio_transcode(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TRANSCODABLE_AUDIO_FILE_EXTENSIONS


def supported_audio_format_names() -> str:
    return "、".join(extension.lstrip(".").upper() for extension in SUPPORTED_AUDIO_FILE_EXTENSIONS)


def direct_audio_format_names() -> str:
    return "、".join(extension.lstrip(".").upper() for extension in DIRECT_AUDIO_FILE_EXTENSIONS)


def transcodable_audio_format_names() -> str:
    return "、".join(extension.lstrip(".").upper() for extension in TRANSCODABLE_AUDIO_FILE_EXTENSIONS)


def probe_audio_duration_seconds(path: str | Path) -> float | None:
    """Best-effort duration probe for imported audio before running ASR."""

    source = Path(path).expanduser()
    try:
        import soundfile as sf

        info = sf.info(str(source))
        if info.frames and info.samplerate:
            return float(info.frames) / float(info.samplerate)
    except Exception:
        pass

    ffmpeg = find_ffmpeg_executable()
    if ffmpeg is None:
        return None

    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **_hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    return _parse_ffmpeg_duration((completed.stderr or "") + "\n" + (completed.stdout or ""))


def _parse_ffmpeg_duration(output: str) -> float | None:
    match = re.search(r"Duration:\s*(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)", output)
    if not match:
        return None
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600.0 + minutes * 60.0 + seconds


def find_ffmpeg_executable() -> str | None:
    """Find a bundled, configured, or PATH ffmpeg executable."""

    configured = os.environ.get("VOICE_INPUT_FFMPEG", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    base = app_base_dir()
    executable_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    candidates.extend(
        [
            base / "tools" / "ffmpeg" / "bin" / executable_name,
            base / "tools" / "ffmpeg" / executable_name,
            base / "ffmpeg" / "bin" / executable_name,
            base / executable_name,
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if bundled.is_file():
            return str(bundled)
    except Exception:
        pass

    return shutil.which("ffmpeg")


def transcode_audio_to_wav(
    input_path: str | Path,
    output_path: str | Path,
    *,
    timeout_seconds: float | None = 30 * 60,
    cancel_event: object | None = None,
) -> Path:
    """Convert an imported audio/video container to whisper-friendly WAV."""

    source = Path(input_path).expanduser()
    target = Path(output_path)
    ffmpeg = find_ffmpeg_executable()
    if ffmpeg is None:
        raise AudioTranscodeUnavailable(
            "当前文件格式需要转码组件。请安装包含 FFmpeg 的新版安装包，"
            "或将 ffmpeg 加入 PATH 后重试。"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(DEFAULT_SAMPLE_RATE),
        "-f",
        "wav",
        str(target),
    ]
    try:
        if cancel_event is None:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                **_hidden_subprocess_kwargs(),
            )
        else:
            completed = _run_cancellable_transcode(
                command,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
    except subprocess.TimeoutExpired as exc:
        raise AudioTranscodeError("音频转码超时，请缩短文件或检查文件是否损坏。") from exc
    except OSError as exc:
        raise AudioTranscodeError(f"无法启动音频转码组件：{ffmpeg}") from exc

    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "无输出").strip()
        raise AudioTranscodeError(f"音频转码失败，请检查文件是否包含有效音轨。输出：{details[:1200]}")
    if not target.is_file() or target.stat().st_size <= 0:
        raise AudioTranscodeError("音频转码失败：未生成有效 WAV 文件。")
    return target


def _run_cancellable_transcode(
    command: list[str],
    *,
    timeout_seconds: float | None,
    cancel_event: object,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_subprocess_kwargs(),
    )
    deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
    try:
        while True:
            if _event_is_set(cancel_event):
                _terminate_subprocess(process)
                raise AudioTranscodeCancelled("音频转码已取消。")
            if deadline is not None and time.monotonic() >= deadline:
                _terminate_subprocess(process)
                raise AudioTranscodeError("音频转码超时，请缩短文件或检查文件是否损坏。")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            _terminate_subprocess(process)


def _event_is_set(event: object | None) -> bool:
    is_set = getattr(event, "is_set", None)
    return bool(callable(is_set) and is_set())


def _terminate_subprocess(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if not sys.platform.startswith("win"):
        return {}
    kwargs: dict[str, object] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    kwargs["startupinfo"] = startupinfo
    return kwargs
