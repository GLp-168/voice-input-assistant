"""Shared runtime state for the voice input assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import time


class RunState(str, Enum):
    """User-visible recording/recognition state."""

    IDLE = "idle"
    LISTENING = "listening"
    RECOGNIZING = "recognizing"
    INJECTING = "injecting"
    ERROR = "error"


@dataclass(slots=True)
class RuntimeStatus:
    """Small status object that UI code can render directly."""

    state: RunState = RunState.IDLE
    message: str = "Ready"
    last_text: str = ""
    updated_at: float = field(default_factory=time)

    def replace(
        self,
        *,
        state: RunState | None = None,
        message: str | None = None,
        last_text: str | None = None,
    ) -> "RuntimeStatus":
        return RuntimeStatus(
            state=state if state is not None else self.state,
            message=message if message is not None else self.message,
            last_text=last_text if last_text is not None else self.last_text,
        )
