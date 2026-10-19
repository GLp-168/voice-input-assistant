"""Audio capture helpers with optional sounddevice integration."""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from typing import Any, Mapping

from .config import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE, DEFAULT_SAMPLE_WIDTH


class AudioCaptureError(RuntimeError):
    """Raised when microphone capture cannot be performed."""


@dataclass(frozen=True)
class MicrophoneDevice:
    """A normalized microphone descriptor."""

    index: int
    name: str
    max_input_channels: int
    default_sample_rate: int | None = None
    hostapi: int | None = None

    @property
    def stable_id(self) -> str:
        """Persist a device identity that survives input-list reordering."""

        return f"{self.hostapi if self.hostapi is not None else -1}:{self.name}"


@dataclass(frozen=True)
class MicrophoneState:
    """Current microphone availability and capture state."""

    status: str = "idle"
    device: int | str | None = None
    name: str = "System default microphone"
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "device": self.device,
            "name": self.name,
            "message": self.message,
        }


@dataclass(frozen=True)
class AudioLevel:
    """Normalized microphone level derived from a PCM capture block."""

    level: float
    rms: float
    peak: float
    rms_dbfs: float
    peak_dbfs: float
    clipping: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "rms": self.rms,
            "peak": self.peak,
            "rms_dbfs": self.rms_dbfs,
            "peak_dbfs": self.peak_dbfs,
            "clipping": self.clipping,
        }


AudioLevelCallback = Callable[[AudioLevel], None]
MicrophoneStateCallback = Callable[[MicrophoneState], None]


class AudioLevelMeter:
    """Compute a perceptual 0..1 level while limiting callback frequency."""

    def __init__(self, *, minimum_interval_seconds: float = 0.08) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self._last_emitted_at: float | None = None
        self._lock = threading.Lock()

    def process(self, pcm: bytes, *, dtype: str = "int16", now: float | None = None) -> AudioLevel | None:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if (
                self._last_emitted_at is not None
                and timestamp - self._last_emitted_at < self.minimum_interval_seconds
            ):
                return None
            self._last_emitted_at = timestamp
        return calculate_audio_level(pcm, dtype=dtype)

    def reset(self) -> None:
        with self._lock:
            self._last_emitted_at = None


def calculate_audio_level(pcm: bytes, *, dtype: str = "int16") -> AudioLevel:
    """Return RMS/peak values for little-endian mono or interleaved PCM."""

    normalized_dtype = dtype.lower()
    typecode_and_scale = {
        "int8": ("b", 127.0),
        "int16": ("h", 32767.0),
        "int32": ("i", 2147483647.0),
        "float32": ("f", 1.0),
        "float64": ("d", 1.0),
    }.get(normalized_dtype)
    if typecode_and_scale is None:
        raise ValueError(f"unsupported recording dtype for audio level: {dtype}")

    typecode, scale = typecode_and_scale
    samples = array(typecode)
    item_size = samples.itemsize
    usable_bytes = len(pcm) - (len(pcm) % item_size)
    if usable_bytes <= 0:
        return AudioLevel(level=0.0, rms=0.0, peak=0.0, rms_dbfs=-96.0, peak_dbfs=-96.0)
    samples.frombytes(pcm[:usable_bytes])

    squared_total = 0.0
    peak_value = 0.0
    for sample in samples:
        normalized = max(-1.0, min(1.0, float(sample) / scale))
        magnitude = abs(normalized)
        peak_value = max(peak_value, magnitude)
        squared_total += normalized * normalized
    rms_value = math.sqrt(squared_total / len(samples)) if samples else 0.0
    rms_dbfs = _linear_to_dbfs(rms_value)
    peak_dbfs = _linear_to_dbfs(peak_value)
    # Map the useful speech range (-60..0 dBFS) to an animation-friendly 0..1.
    level = max(0.0, min(1.0, (rms_dbfs + 60.0) / 60.0))
    return AudioLevel(
        level=level,
        rms=rms_value,
        peak=peak_value,
        rms_dbfs=rms_dbfs,
        peak_dbfs=peak_dbfs,
        clipping=peak_value >= 0.99,
    )


class DiskBackedPCM:
    """A bytes-like PCM payload that owns a temporary file."""

    def __init__(
        self,
        path: str | Path,
        size: int,
        *,
        channels: int,
        sample_width: int,
    ) -> None:
        self.path = Path(path)
        self.audio_path = self.path
        self._size = max(0, int(size))
        self._channels = max(1, int(channels))
        self._sample_width = max(1, int(sample_width))
        self._closed = False
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._size

    def __bytes__(self) -> bytes:
        return self.read_slice(0, self._size)

    def read_slice(self, start: int, end: int) -> bytes:
        with self._lock:
            if self._closed:
                raise ValueError("PCM temporary file is closed")
            bounded_start = min(self._size, max(0, int(start)))
            bounded_end = min(self._size, max(bounded_start, int(end)))
            frame_size = self._channels * self._sample_width
            start_frame, prefix = divmod(bounded_start, frame_size)
            byte_count = bounded_end - bounded_start
            frame_count = (prefix + byte_count + frame_size - 1) // frame_size
            with wave.open(str(self.path), "rb") as wav_file:
                wav_file.setpos(start_frame)
                payload = wav_file.readframes(frame_count)
            return payload[prefix : prefix + byte_count]

    def __getitem__(self, key: int | slice) -> int | bytes:
        return bytes(self)[key]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> DiskBackedPCM:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - deterministic callers use close()
        self.close()


@dataclass(frozen=True)
class AudioChunk:
    """A captured PCM audio segment."""

    pcm: bytes | DiskBackedPCM
    sample_rate: int = DEFAULT_SAMPLE_RATE
    channels: int = DEFAULT_CHANNELS
    sample_width: int = DEFAULT_SAMPLE_WIDTH
    device: int | str | None = None
    started_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        bytes_per_second = self.sample_rate * self.channels * self.sample_width
        if bytes_per_second <= 0:
            return 0.0
        return len(self.pcm) / float(bytes_per_second)

    def close(self) -> None:
        close = getattr(self.pcm, "close", None)
        if callable(close):
            close()


def list_microphones() -> list[MicrophoneDevice]:
    """Return available input devices.

    Requires the optional ``sounddevice`` package. The import is intentionally
    lazy so ASR tests and dummy mode work on machines without audio drivers.
    """

    sd = _require_sounddevice()
    devices = sd.query_devices()

    microphones: list[MicrophoneDevice] = []
    for index, device in enumerate(devices):
        max_input_channels = int(device.get("max_input_channels", 0))
        if max_input_channels <= 0:
            continue
        microphones.append(
            MicrophoneDevice(
                index=index,
                name=str(device.get("name", f"Input device {index}")),
                max_input_channels=max_input_channels,
                default_sample_rate=_as_int_or_none(device.get("default_samplerate")),
                hostapi=_as_int_or_none(device.get("hostapi")),
            )
        )
    return microphones


def resolve_microphone_device(
    stable_id: str | None,
    devices: list[MicrophoneDevice] | None = None,
) -> int | None:
    """Resolve a saved stable identity to the current sounddevice index."""

    selection = str(stable_id or "").strip()
    if not selection:
        return None
    available = list_microphones() if devices is None else list(devices)
    for device in available:
        if device.stable_id == selection:
            return device.index
    return None


def record_segment(
    duration_seconds: float,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    device: int | str | None = None,
    dtype: str = "int16",
) -> AudioChunk:
    """Record a fixed-duration microphone segment as PCM bytes."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than 0")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    if channels <= 0:
        raise ValueError("channels must be greater than 0")

    sd = _require_sounddevice()
    frames = int(round(duration_seconds * sample_rate))
    started_at = datetime.now(timezone.utc)

    try:
        recording = sd.rec(
            frames,
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
            device=device,
        )
        sd.wait()
    except Exception as exc:  # pragma: no cover - depends on local audio host
        raise AudioCaptureError(f"failed to record microphone audio: {exc}") from exc

    try:
        pcm = recording.tobytes()
    except AttributeError:
        pcm = bytes(recording)

    return AudioChunk(
        pcm=pcm,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=_sample_width_from_dtype(dtype),
        device=device,
        started_at=started_at,
        metadata={"frames": frames, "dtype": dtype},
    )


class MicrophoneRecorder:
    """Streaming recorder that stops on demand or at a maximum duration."""

    def __init__(
        self,
        *,
        max_duration_seconds: float,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        device: int | str | None = None,
        dtype: str = "int16",
        memory_limit_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be greater than 0")
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if channels <= 0:
            raise ValueError("channels must be greater than 0")
        self.max_duration_seconds = max_duration_seconds
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self.dtype = dtype
        self.memory_limit_bytes = max(0, int(memory_limit_bytes))
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._frame_total = 0
        self._last_snapshot_frame = 0
        self._last_snapshot_offset = 0
        self._pcm_size = 0
        self._temp_path: Path | None = None
        self._temp_file: Any | None = None
        self._completed_pcm: bytes | None = None
        self._disk_pcm: DiskBackedPCM | None = None
        self._buffer_lock = threading.Lock()
        self._audio_level_callback: AudioLevelCallback | None = None
        self._microphone_state_callback: MicrophoneStateCallback | None = None
        self._level_meter = AudioLevelMeter()

    def set_audio_monitor(
        self,
        on_level: AudioLevelCallback | None,
        on_state: MicrophoneStateCallback | None = None,
    ) -> None:
        self._audio_level_callback = on_level
        self._microphone_state_callback = on_state
        self._level_meter.reset()

    def stop(self) -> None:
        self._stop_event.set()

    def pause(self) -> None:
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def snapshot_since_last(self) -> AudioChunk | None:
        with self._buffer_lock:
            if self._frame_total <= self._last_snapshot_frame:
                return None
            start_frame = self._last_snapshot_frame
            start_offset = self._last_snapshot_offset
            end_offset = self._pcm_size
            if self._completed_pcm is not None:
                pcm = self._completed_pcm[start_offset:end_offset]
            elif self._disk_pcm is not None:
                pcm = self._disk_pcm.read_slice(start_offset, end_offset)
            else:
                if self._temp_file is not None:
                    self._temp_file.flush()
                temp_path = self._temp_path
                if temp_path is None:
                    return None
                with temp_path.open("rb") as source:
                    source.seek(start_offset)
                    pcm = source.read(end_offset - start_offset)
            frame_total = self._frame_total
            self._last_snapshot_frame = frame_total
            self._last_snapshot_offset = end_offset
        if not pcm:
            return None
        return AudioChunk(
            pcm=pcm,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=_sample_width_from_dtype(self.dtype),
            device=self.device,
            metadata={
                "frames": frame_total - start_frame,
                "snapshot_start_frame": start_frame,
                "snapshot_end_frame": frame_total,
                "partial": True,
            },
        )

    def record_until_stopped(self) -> AudioChunk:
        sd = _require_sounddevice()
        started_at = datetime.now(timezone.utc)
        frame_total = 0
        blocksize = max(800, int(self.sample_rate * 0.1))
        max_frames = int(round(self.max_duration_seconds * self.sample_rate))
        capture_error: Exception | None = None
        done_event = threading.Event()
        self._prepare_capture_file()
        device_state = describe_microphone(sd, self.device, status="opening")
        _safe_notify(self._microphone_state_callback, device_state)

        def callback(indata: object, frame_count: int, _time: object, status: object) -> None:
            nonlocal frame_total, capture_error
            if status:
                capture_error = RuntimeError(str(status))
            if self._stop_event.is_set():
                done_event.set()
                raise sd.CallbackStop()
            if self._pause_event.is_set():
                return
            try:
                payload = indata.copy().tobytes()  # type: ignore[attr-defined]
            except AttributeError:
                payload = bytes(indata)
            if self._audio_level_callback is not None:
                try:
                    level = self._level_meter.process(payload, dtype=self.dtype)
                except (TypeError, ValueError):
                    level = None
                if level is not None:
                    _safe_notify(self._audio_level_callback, level)
            with self._buffer_lock:
                if self._temp_file is None:
                    capture_error = RuntimeError("capture buffer is unavailable")
                    done_event.set()
                    raise sd.CallbackStop()
                self._temp_file.write(payload)
                self._pcm_size += len(payload)
                self._frame_total += int(frame_count)
            frame_total = self._frame_total
            if self._stop_event.is_set() or frame_total >= max_frames:
                done_event.set()
                raise sd.CallbackStop()

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=self.dtype,
                device=self.device,
                blocksize=blocksize,
                callback=callback,
            ):
                _safe_notify(
                    self._microphone_state_callback,
                    MicrophoneState(
                        status="active",
                        device=device_state.device,
                        name=device_state.name,
                    ),
                )
                while not done_event.wait(0.05):
                    if self._stop_event.is_set() or frame_total >= max_frames:
                        break
        except Exception as exc:  # pragma: no cover - depends on local audio host
            self.close()
            _safe_notify(
                self._microphone_state_callback,
                MicrophoneState(
                    status="error",
                    device=device_state.device,
                    name=device_state.name,
                    message=str(exc),
                ),
            )
            raise AudioCaptureError(f"failed to record microphone audio: {exc}") from exc

        if capture_error is not None:
            self.close()
            _safe_notify(
                self._microphone_state_callback,
                MicrophoneState(
                    status="error",
                    device=device_state.device,
                    name=device_state.name,
                    message=str(capture_error),
                ),
            )
            raise AudioCaptureError(f"failed to record microphone audio: {capture_error}")

        _safe_notify(self._audio_level_callback, _silent_audio_level())
        _safe_notify(
            self._microphone_state_callback,
            MicrophoneState(status="idle", device=device_state.device, name=device_state.name),
        )

        pcm = self._complete_capture_file()
        return AudioChunk(
            pcm=pcm,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=_sample_width_from_dtype(self.dtype),
            device=self.device,
            started_at=started_at,
            metadata={
                "frames": frame_total,
                "dtype": self.dtype,
                "max_duration_seconds": self.max_duration_seconds,
                "stopped_by_user": self._stop_event.is_set(),
            },
        )

    def close(self) -> None:
        with self._buffer_lock:
            temp_file = self._temp_file
            self._temp_file = None
            temp_path = self._temp_path
            self._temp_path = None
            disk_pcm = self._disk_pcm
            self._disk_pcm = None
        if temp_file is not None:
            try:
                temp_file.close()
            except OSError:
                pass
        if disk_pcm is not None:
            disk_pcm.close()
        elif temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _prepare_capture_file(self) -> None:
        self.close()
        temp_file = tempfile.NamedTemporaryFile(prefix="voice-input-recording-", suffix=".pcm", delete=False)
        with self._buffer_lock:
            self._temp_file = temp_file
            self._temp_path = Path(temp_file.name)
            self._completed_pcm = None
            self._disk_pcm = None
            self._frame_total = 0
            self._last_snapshot_frame = 0
            self._last_snapshot_offset = 0
            self._pcm_size = 0

    def _complete_capture_file(self) -> bytes | DiskBackedPCM:
        with self._buffer_lock:
            temp_file = self._temp_file
            self._temp_file = None
            temp_path = self._temp_path
            pcm_size = self._pcm_size
            if temp_file is not None:
                temp_file.flush()
                os.fsync(temp_file.fileno())
                temp_file.close()
            if temp_path is None:
                self._completed_pcm = b""
                return self._completed_pcm
            if pcm_size <= self.memory_limit_bytes:
                self._completed_pcm = temp_path.read_bytes()
                self._temp_path = None
                temp_path.unlink(missing_ok=True)
                return self._completed_pcm
            wav_file = tempfile.NamedTemporaryFile(
                prefix="voice-input-recording-",
                suffix=".wav",
                delete=False,
            )
            wav_path = Path(wav_file.name)
            wav_file.close()
            try:
                with temp_path.open("rb") as source, wave.open(str(wav_path), "wb") as output:
                    output.setnchannels(self.channels)
                    output.setsampwidth(_sample_width_from_dtype(self.dtype))
                    output.setframerate(self.sample_rate)
                    while payload := source.read(1024 * 1024):
                        output.writeframesraw(payload)
            except Exception:
                wav_path.unlink(missing_ok=True)
                temp_path.unlink(missing_ok=True)
                self._temp_path = None
                raise
            temp_path.unlink(missing_ok=True)
            self._temp_path = wav_path
            self._disk_pcm = DiskBackedPCM(
                wav_path,
                pcm_size,
                channels=self.channels,
                sample_width=_sample_width_from_dtype(self.dtype),
            )
            return self._disk_pcm


def _require_sounddevice() -> Any:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AudioCaptureError(
            "audio capture requires the optional dependency 'sounddevice'. "
            "Install it and ensure PortAudio/audio drivers are available."
        ) from exc
    return sd


def _sample_width_from_dtype(dtype: str) -> int:
    normalized = dtype.lower()
    if normalized in {"int16", "uint16", "float16"}:
        return 2
    if normalized in {"int32", "uint32", "float32"}:
        return 4
    if normalized in {"int8", "uint8"}:
        return 1
    raise ValueError(f"unsupported recording dtype for PCM width: {dtype}")


def _as_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _linear_to_dbfs(value: float) -> float:
    if value <= 0.0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(value))


def _silent_audio_level() -> AudioLevel:
    return AudioLevel(level=0.0, rms=0.0, peak=0.0, rms_dbfs=-96.0, peak_dbfs=-96.0)


def describe_microphone(sd: Any, device: int | str | None, *, status: str = "idle") -> MicrophoneState:
    """Resolve a capture device name without failing microphone startup."""

    fallback_name = "System default microphone" if device is None else str(device)
    query_devices = getattr(sd, "query_devices", None)
    if not callable(query_devices):
        return MicrophoneState(status=status, device=device, name=fallback_name)
    try:
        details = query_devices(device, "input")
    except TypeError:
        try:
            details = query_devices(device)
        except Exception:
            return MicrophoneState(status=status, device=device, name=fallback_name)
    except Exception:
        return MicrophoneState(status=status, device=device, name=fallback_name)
    if isinstance(details, Mapping):
        name = str(details.get("name") or fallback_name)
        resolved_device = details.get("index", device)
        return MicrophoneState(status=status, device=resolved_device, name=name)
    return MicrophoneState(status=status, device=device, name=fallback_name)


def _safe_notify(callback: Callable[[Any], None] | None, payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        # Audio callbacks must never be interrupted by monitoring/UI failures.
        return
