"""Lean FunASR package entry point for frozen local inference builds.

The upstream package imports every submodule during ``funasr`` import so its
registries are fully populated. In a PyInstaller bundle that recursive scan
pulls in training, server, CV and test modules, and can also trip Windows path
limits. The app registers the local inference modules explicitly via
``voice_input.funasr_compat.patch_funasr_runtime()``, so this proxy keeps import
startup narrow while still exposing the upstream package submodules.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from pkgutil import extend_path


__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]
_LOCAL_PACKAGE_DIR = Path(__file__).resolve().parent
for _entry in sys.path:
    _candidate = (Path(_entry or ".") / "funasr").resolve()
    if _candidate.is_dir() and _candidate != _LOCAL_PACKAGE_DIR:
        _candidate_text = str(_candidate)
        if _candidate_text not in __path__:
            __path__.append(_candidate_text)
_IMPORT_ERRORS: dict[str, str] = {}
_IMPORT_ERROR_TRACEBACKS: dict[str, str] = {}


try:
    __version__ = version("funasr")
except PackageNotFoundError:
    __version__ = ""


def get_import_errors() -> dict[str, str]:
    return dict(_IMPORT_ERRORS)


def get_import_error_tracebacks() -> dict[str, str]:
    return dict(_IMPORT_ERROR_TRACEBACKS)


from funasr.auto.auto_model import AutoModel  # noqa: E402
from funasr.auto.auto_frontend import AutoFrontend  # noqa: E402


os.environ["HYDRA_FULL_ERROR"] = "1"

__all__ = ["AutoFrontend", "AutoModel", "get_import_errors", "get_import_error_tracebacks"]
