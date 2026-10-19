"""Compatibility package for running from a source checkout."""

from __future__ import annotations

from pathlib import Path


_SRC_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "voice_input"
if _SRC_PACKAGE.exists():
    __path__.append(str(_SRC_PACKAGE))

