from __future__ import annotations

from pathlib import Path

from voice_input import logging_config


def test_configure_logging_creates_log_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logging_config, "default_config_dir", lambda: tmp_path)

    path = logging_config.configure_logging()

    assert path.parent.exists()
    assert path.name == "voice-input.log"
