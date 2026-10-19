"""Application controller that connects audio, ASR, and text injection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from .status import RunState, RuntimeStatus


class ASREngine(Protocol):
    """Minimal protocol expected from concrete ASR engines."""

    def transcribe(self, audio: object) -> object:
        """Return a result object, a string, or any object with a ``text`` attr."""


class AudioRecorder(Protocol):
    """Minimal protocol expected from audio recorders."""

    def record_once(self) -> object:
        """Capture one utterance or audio buffer for recognition."""


class TextInjector(Protocol):
    """Minimal protocol expected from text injection implementations."""

    def inject(self, text: str) -> None:
        """Insert text into the current target field."""


StatusCallback = Callable[[RuntimeStatus], None]


@dataclass(slots=True)
class VoiceInputController:
    """Coordinates a single listen-recognize-insert cycle.

    The MVP intentionally runs one short cycle at a time. Continuous streaming can
    be added behind the same controller once the target ASR engine is finalized.
    """

    recorder: AudioRecorder
    asr_engine: ASREngine
    injector: TextInjector
    on_status: StatusCallback | None = None
    status: RuntimeStatus = field(default_factory=RuntimeStatus)

    def run_once(self) -> str:
        """Record, transcribe, and inject one utterance."""
        try:
            self._set_status(RunState.LISTENING, "Listening")
            audio = self.recorder.record_once()

            self._set_status(RunState.RECOGNIZING, "Recognizing")
            result = self.asr_engine.transcribe(audio)
            text = _extract_text(result).strip()
            if not text:
                self._set_status(RunState.IDLE, "No speech recognized")
                return ""

            self._set_status(RunState.INJECTING, "Inserting text", text)
            self.injector.inject(text)

            self._set_status(RunState.IDLE, "Inserted", text)
            return text
        except Exception as exc:  # UI boundary: keep app alive and show a clear state.
            self._set_status(RunState.ERROR, str(exc))
            raise

    def _set_status(self, state: RunState, message: str, last_text: str | None = None) -> None:
        self.status = self.status.replace(state=state, message=message, last_text=last_text)
        if self.on_status is not None:
            self.on_status(self.status)


def _extract_text(result: object) -> str:
    if isinstance(result, str):
        return result
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(result, dict):
        value = result.get("text", "")
        return value if isinstance(value, str) else str(value)
    return str(result)
