from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

import pytest

from voice_input.resource_policy import (
    CONSTRAINED_MIN_AVAILABLE_MEMORY_BYTES,
    CONSTRAINED_MIN_LOGICAL_CPUS,
    CONSTRAINED_MIN_TOTAL_MEMORY_BYTES,
    HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES,
    HIGH_QUALITY_MIN_LOGICAL_CPUS,
    HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES,
    ResourcePolicyMode,
    ResourceProfile,
    SessionResourcePolicy,
    SystemResourceSnapshot,
    build_resource_decision,
    capture_system_resource_snapshot,
    select_resource_profile,
)


def test_unknown_or_invalid_snapshot_defaults_to_balanced() -> None:
    assert select_resource_profile(SystemResourceSnapshot()) is ResourceProfile.BALANCED
    assert (
        select_resource_profile(
            SystemResourceSnapshot(
                available_memory_bytes=-1,
                total_memory_bytes=0,
                logical_cpu_count=True,
            )
        )
        is ResourceProfile.BALANCED
    )


@pytest.mark.parametrize(
    ("field", "boundary"),
    [
        ("available_memory_bytes", CONSTRAINED_MIN_AVAILABLE_MEMORY_BYTES),
        ("total_memory_bytes", CONSTRAINED_MIN_TOTAL_MEMORY_BYTES),
        ("logical_cpu_count", CONSTRAINED_MIN_LOGICAL_CPUS),
    ],
)
def test_known_capacity_below_constrained_boundary_wins(field: str, boundary: int) -> None:
    values = {
        "available_memory_bytes": HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES,
        "total_memory_bytes": HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES,
        "logical_cpu_count": HIGH_QUALITY_MIN_LOGICAL_CPUS,
        field: boundary - 1,
    }

    assert select_resource_profile(SystemResourceSnapshot(**values)) is ResourceProfile.CONSTRAINED


def test_exact_constrained_boundaries_select_balanced() -> None:
    snapshot = SystemResourceSnapshot(
        available_memory_bytes=CONSTRAINED_MIN_AVAILABLE_MEMORY_BYTES,
        total_memory_bytes=CONSTRAINED_MIN_TOTAL_MEMORY_BYTES,
        logical_cpu_count=CONSTRAINED_MIN_LOGICAL_CPUS,
    )

    assert select_resource_profile(snapshot) is ResourceProfile.BALANCED


def test_exact_high_quality_boundaries_select_high_quality() -> None:
    snapshot = SystemResourceSnapshot(
        available_memory_bytes=HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES,
        total_memory_bytes=HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES,
        logical_cpu_count=HIGH_QUALITY_MIN_LOGICAL_CPUS,
        on_battery=False,
    )

    assert select_resource_profile(snapshot) is ResourceProfile.HIGH_QUALITY


def test_partial_high_capacity_snapshot_stays_balanced() -> None:
    snapshot = SystemResourceSnapshot(
        available_memory_bytes=HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES,
        logical_cpu_count=HIGH_QUALITY_MIN_LOGICAL_CPUS,
    )

    assert select_resource_profile(snapshot) is ResourceProfile.BALANCED


def test_battery_power_prevents_high_quality_but_not_hard_downgrade() -> None:
    capable = SystemResourceSnapshot(
        available_memory_bytes=HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES,
        total_memory_bytes=HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES,
        logical_cpu_count=HIGH_QUALITY_MIN_LOGICAL_CPUS,
        on_battery=True,
    )
    low_memory = SystemResourceSnapshot(
        available_memory_bytes=CONSTRAINED_MIN_AVAILABLE_MEMORY_BYTES - 1,
        total_memory_bytes=HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES,
        logical_cpu_count=HIGH_QUALITY_MIN_LOGICAL_CPUS,
        on_battery=True,
    )

    assert select_resource_profile(capable) is ResourceProfile.BALANCED
    assert select_resource_profile(low_memory) is ResourceProfile.CONSTRAINED


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            ResourceProfile.CONSTRAINED,
            (False, True, False, False, 2, 2),
        ),
        (
            ResourceProfile.BALANCED,
            (True, True, False, False, 4, 4),
        ),
        (
            ResourceProfile.HIGH_QUALITY,
            (True, False, True, False, 4, 8),
        ),
    ],
)
def test_profile_lifecycle_and_thread_decisions(profile, expected) -> None:
    decision = build_resource_decision(
        profile,
        SystemResourceSnapshot(logical_cpu_count=32),
    )

    assert (
        decision.online_prewarm,
        decision.release_online_after_recording,
        decision.retain_review_worker,
        decision.allow_parallel_workers,
        decision.online_threads,
        decision.review_threads,
    ) == expected


def test_thread_budgets_never_exceed_known_cpu_count() -> None:
    decision = build_resource_decision(
        ResourceProfile.HIGH_QUALITY,
        SystemResourceSnapshot(logical_cpu_count=2),
    )

    assert decision.online_threads == 2
    assert decision.review_threads == 2


def test_session_decision_is_sticky_until_end_session() -> None:
    policy = SessionResourcePolicy()
    capable = SystemResourceSnapshot(
        available_memory_bytes=HIGH_QUALITY_MIN_AVAILABLE_MEMORY_BYTES,
        total_memory_bytes=HIGH_QUALITY_MIN_TOTAL_MEMORY_BYTES,
        logical_cpu_count=HIGH_QUALITY_MIN_LOGICAL_CPUS,
    )
    constrained = SystemResourceSnapshot(
        available_memory_bytes=1,
        total_memory_bytes=1,
        logical_cpu_count=1,
    )

    first = policy.begin_session(capable)
    sticky = policy.begin_session(constrained, mode=ResourcePolicyMode.CONSTRAINED)

    assert first.profile is ResourceProfile.HIGH_QUALITY
    assert sticky is first
    assert policy.active_decision is first
    assert policy.end_session() is first
    assert policy.active_decision is None
    assert policy.begin_session(constrained).profile is ResourceProfile.CONSTRAINED


@pytest.mark.parametrize(
    ("mode", "profile"),
    [
        (ResourcePolicyMode.CONSTRAINED, ResourceProfile.CONSTRAINED),
        (ResourcePolicyMode.BALANCED, ResourceProfile.BALANCED),
        (ResourcePolicyMode.HIGH_QUALITY, ResourceProfile.HIGH_QUALITY),
    ],
)
def test_explicit_mode_overrides_automatic_selection(mode, profile) -> None:
    decision = SessionResourcePolicy().begin_session(SystemResourceSnapshot(), mode=mode)

    assert decision.profile is profile


def test_policy_instances_do_not_share_session_state() -> None:
    first = SessionResourcePolicy()
    second = SessionResourcePolicy()

    first.begin_session(SystemResourceSnapshot(), mode=ResourcePolicyMode.CONSTRAINED)

    assert second.active_decision is None
    assert (
        second.begin_session(SystemResourceSnapshot(), mode=ResourcePolicyMode.HIGH_QUALITY).profile
        is ResourceProfile.HIGH_QUALITY
    )


def test_capture_system_snapshot_uses_optional_psutil(monkeypatch) -> None:
    fake_psutil = ModuleType("psutil")
    fake_psutil.virtual_memory = lambda: SimpleNamespace(available=9, total=10)
    fake_psutil.sensors_battery = lambda: SimpleNamespace(power_plugged=False)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr("voice_input.resource_policy.os.cpu_count", lambda: 11)

    assert capture_system_resource_snapshot() == SystemResourceSnapshot(
        available_memory_bytes=9,
        total_memory_bytes=10,
        logical_cpu_count=11,
        on_battery=True,
    )


def test_capture_system_snapshot_falls_back_when_psutil_is_unavailable(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr("voice_input.resource_policy.os.cpu_count", lambda: 7)

    assert capture_system_resource_snapshot() == SystemResourceSnapshot(logical_cpu_count=7)
