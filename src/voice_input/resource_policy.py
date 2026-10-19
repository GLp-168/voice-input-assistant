"""Pure session-level resource policy for ASR worker lifecycles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os


GIBIBYTE = 1024**3
CONSTRAINED_MIN_AVAILABLE_MEMORY_BYTES = 8 * GIBIBYTE
CONSTRAINED_MIN_TOTAL_MEMORY_BYTES = 12 * GIBIBYTE
CONSTRAINED_MIN_LOGICAL_CPUS = 6
HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES = 16 * GIBIBYTE
HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES = 24 * GIBIBYTE
HIGH_QUALITY_MIN_LOGICAL_CPUS = 12


class ResourceProfile(str, Enum):
    """Concrete resource profiles selected for one recognition session."""

    CONSTRAINED = "constrained"
    BALANCED = "balanced"
    HIGH_QUALITY = "high_quality"


class ResourcePolicyMode(str, Enum):
    """User selection mode; AUTO resolves to a concrete profile."""

    AUTO = "auto"
    CONSTRAINED = ResourceProfile.CONSTRAINED.value
    BALANCED = ResourceProfile.BALANCED.value
    HIGH_QUALITY = ResourceProfile.HIGH_QUALITY.value


@dataclass(frozen=True, slots=True)
class SystemResourceSnapshot:
    """Resource values captured by the caller at a single point in time."""

    available_memory_bytes: int | None = None
    total_memory_bytes: int | None = None
    logical_cpu_count: int | None = None
    on_battery: bool | None = None


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    """Worker lifecycle and per-worker thread budgets for one session."""

    profile: ResourceProfile
    online_prewarm: bool
    release_online_after_recording: bool
    retain_review_worker: bool
    allow_parallel_workers: bool
    online_threads: int
    review_threads: int


def select_resource_profile(snapshot: SystemResourceSnapshot) -> ResourceProfile:
    """Select a concrete profile without inspecting the host environment."""

    available_memory = _positive_int(snapshot.available_memory_bytes)
    total_memory = _positive_int(snapshot.total_memory_bytes)
    logical_cpus = _positive_int(snapshot.logical_cpu_count)

    # A known hard limit wins over other signals. Partial or invalid snapshots
    # cannot qualify for high quality, but still allow a safe downgrade.
    if available_memory is not None and available_memory < CONSTRAINED_MIN_AVAILABLE_MEMORY_BYTES:
        return ResourceProfile.CONSTRAINED
    if total_memory is not None and total_memory < CONSTRAINED_MIN_TOTAL_MEMORY_BYTES:
        return ResourceProfile.CONSTRAINED
    if logical_cpus is not None and logical_cpus < CONSTRAINED_MIN_LOGICAL_CPUS:
        return ResourceProfile.CONSTRAINED

    has_complete_capacity = all(
        value is not None for value in (available_memory, total_memory, logical_cpus)
    )
    if (
        has_complete_capacity
        and available_memory >= HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES
        and total_memory >= HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES
        and logical_cpus >= HIGH_QUALITY_MIN_LOGICAL_CPUS
        and snapshot.on_battery is not True
    ):
        return ResourceProfile.HIGH_QUALITY
    return ResourceProfile.BALANCED


def build_resource_decision(
    profile: ResourceProfile,
    snapshot: SystemResourceSnapshot,
) -> ResourceDecision:
    """Build lifecycle decisions for a concrete profile."""

    logical_cpus = _positive_int(snapshot.logical_cpu_count)
    if profile is ResourceProfile.CONSTRAINED:
        online_threads = _bounded_threads(logical_cpus, default=2, maximum=2)
        review_threads = _bounded_threads(logical_cpus, default=2, maximum=2)
        return ResourceDecision(
            profile=profile,
            online_prewarm=False,
            release_online_after_recording=True,
            retain_review_worker=False,
            allow_parallel_workers=False,
            online_threads=online_threads,
            review_threads=review_threads,
        )
    if profile is ResourceProfile.BALANCED:
        online_threads = _bounded_threads(logical_cpus, default=4, maximum=4)
        review_threads = _bounded_threads(logical_cpus, default=4, maximum=4)
        return ResourceDecision(
            profile=profile,
            online_prewarm=True,
            release_online_after_recording=True,
            retain_review_worker=False,
            allow_parallel_workers=False,
            online_threads=online_threads,
            review_threads=review_threads,
        )
    if profile is ResourceProfile.HIGH_QUALITY:
        online_threads = _bounded_threads(logical_cpus, default=4, maximum=4)
        review_threads = _bounded_threads(logical_cpus, default=8, maximum=8)
        return ResourceDecision(
            profile=profile,
            online_prewarm=True,
            release_online_after_recording=False,
            retain_review_worker=True,
            allow_parallel_workers=False,
            online_threads=online_threads,
            review_threads=review_threads,
        )
    raise ValueError(f"unsupported resource profile: {profile!r}")


class SessionResourcePolicy:
    """Resolve once per session and keep the decision sticky until it ends."""

    def __init__(self) -> None:
        self._active_decision: ResourceDecision | None = None

    @property
    def active_decision(self) -> ResourceDecision | None:
        return self._active_decision

    def begin_session(
        self,
        snapshot: SystemResourceSnapshot,
        *,
        mode: ResourcePolicyMode = ResourcePolicyMode.AUTO,
    ) -> ResourceDecision:
        if self._active_decision is not None:
            return self._active_decision

        selected_mode = ResourcePolicyMode(mode)
        profile = (
            select_resource_profile(snapshot)
            if selected_mode is ResourcePolicyMode.AUTO
            else ResourceProfile(selected_mode.value)
        )
        self._active_decision = build_resource_decision(profile, snapshot)
        return self._active_decision

    def end_session(self) -> ResourceDecision | None:
        previous = self._active_decision
        self._active_decision = None
        return previous


def capture_system_resource_snapshot() -> SystemResourceSnapshot:
    """Capture host capacity when psutil is available, with a stdlib CPU fallback."""

    available_memory: int | None = None
    total_memory: int | None = None
    on_battery: bool | None = None
    try:
        import psutil

        memory = psutil.virtual_memory()
        available_memory = _positive_int(getattr(memory, "available", None))
        total_memory = _positive_int(getattr(memory, "total", None))
        battery = psutil.sensors_battery()
        if battery is not None:
            on_battery = not bool(getattr(battery, "power_plugged", False))
    except (ImportError, AttributeError, NotImplementedError, OSError, RuntimeError):
        pass
    return SystemResourceSnapshot(
        available_memory_bytes=available_memory,
        total_memory_bytes=total_memory,
        logical_cpu_count=_positive_int(os.cpu_count()),
        on_battery=on_battery,
    )


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _bounded_threads(logical_cpus: int | None, *, default: int, maximum: int) -> int:
    return max(1, min(maximum, logical_cpus if logical_cpus is not None else default))
