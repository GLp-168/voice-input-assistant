"""Compatibility shims for frozen FunASR runtimes."""

from __future__ import annotations

import sys
import builtins
import re
from importlib import import_module
from types import ModuleType
from typing import Callable


_LAST_IMPORT_ERRORS: dict[str, str] = {}


def patch_funasr_runtime() -> None:
    """Restore symbols that PyInstaller can miss in dynamically loaded FunASR models."""

    _patch_funasr_register_source_lookup()
    _patch_torch_jit_script()
    _import_registry_modules()
    load_seg_dict = _seg_dict_loader()
    if load_seg_dict is None:
        return

    if not callable(getattr(builtins, "load_seg_dict", None)):
        setattr(builtins, "load_seg_dict", load_seg_dict)

    for module in _candidate_modules():
        if module is not None and not callable(getattr(module, "load_seg_dict", None)):
            setattr(module, "load_seg_dict", load_seg_dict)


def should_retry_after_funasr_symbol_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "load_seg_dict" in message
        or "'NoneType' object is not callable" in message
        or "is not registered" in message
    )


def funasr_runtime_diagnostics() -> dict[str, object]:
    diagnostics: dict[str, object] = {"import_errors": dict(_LAST_IMPORT_ERRORS)}
    try:
        from funasr.register import tables
    except Exception as exc:
        diagnostics["registry_error"] = repr(exc)
        return diagnostics

    expected = {
        "model_classes": (
            "ParaformerStreaming",
            "SeacoParaformer",
            "FsmnVADStreaming",
            "CTTransformer",
            "CAMPPlus",
        ),
        "frontend_classes": ("WavFrontend", "WavFrontendOnline"),
        "encoder_classes": ("SANMEncoderChunkOpt", "SANMEncoder", "FSMN"),
        "decoder_classes": ("ParaformerSANMDecoder",),
        "predictor_classes": ("CifPredictorV2", "CifPredictorV3"),
        "specaug_classes": ("SpecAugLFR",),
        "tokenizer_classes": ("CharTokenizer",),
    }
    registered: dict[str, dict[str, bool]] = {}
    for table_name, keys in expected.items():
        registry = getattr(tables, table_name, {})
        registered[table_name] = {key: key in registry for key in keys}
    diagnostics["registered"] = registered
    return diagnostics


def _import_registry_modules() -> None:
    for module_name in (
        "funasr.frontends.wav_frontend",
        "funasr.models.paraformer_streaming.model",
        "funasr.models.seaco_paraformer.model",
        "funasr.models.fsmn_vad_streaming.model",
        "funasr.models.ct_transformer.model",
        "funasr.models.campplus.model",
        "funasr.models.scama.encoder",
        "funasr.models.sanm.encoder",
        "funasr.models.fsmn_vad_streaming.encoder",
        "funasr.models.e_paraformer.decoder",
        "funasr.models.paraformer.cif_predictor",
        "funasr.models.bicif_paraformer.cif_predictor",
        "funasr.models.specaug.specaug",
        "funasr.tokenizer.char_tokenizer",
    ):
        _import_optional_module(module_name)


def _patch_funasr_register_source_lookup() -> None:
    try:
        register_module = import_module("funasr.register")
    except Exception:
        return

    tables = getattr(register_module, "tables", None)
    inspect_module = getattr(register_module, "inspect", None)
    if tables is None or inspect_module is None or getattr(tables, "_voice_input_safe_register", False):
        return

    original_register = tables.register

    def safe_register(register_tables_key: str, key: str | None = None) -> callable:
        def decorator(target_class: object):
            if not hasattr(tables, register_tables_key):
                setattr(tables, register_tables_key, {})

            registry = getattr(tables, register_tables_key)
            registry_key = key if key is not None else getattr(target_class, "__name__", str(target_class))
            registry[registry_key] = target_class

            register_tables_key_meta = register_tables_key + "_meta"
            if not hasattr(tables, register_tables_key_meta):
                setattr(tables, register_tables_key_meta, {})
            registry_meta = getattr(tables, register_tables_key_meta)

            try:
                class_file = inspect_module.getfile(target_class)
            except Exception:
                class_file = getattr(target_class, "__module__", "unknown")
            try:
                class_line = inspect_module.getsourcelines(target_class)[1]
            except (OSError, TypeError):
                class_line = 0
            class_file = re.sub(r"^.+/funasr/", "funasr/", str(class_file))
            registry_meta[registry_key] = [
                registry_key,
                getattr(target_class, "__name__", str(target_class)),
                f"{class_file}:{class_line}",
            ]
            return target_class

        return decorator

    safe_register._voice_input_original_register = original_register  # type: ignore[attr-defined]
    tables.register = safe_register
    tables._voice_input_safe_register = True


def _patch_torch_jit_script() -> None:
    try:
        import torch
    except Exception:
        return

    script = getattr(getattr(torch, "jit", None), "script", None)
    if script is None or getattr(script, "_voice_input_safe_script", False):
        return

    def safe_script(obj=None, *args, **kwargs):
        if obj is None:
            def decorator(fn):
                try:
                    return script(fn, *args, **kwargs)
                except (OSError, RuntimeError) as exc:
                    if _is_frozen_source_lookup_error(exc):
                        return fn
                    raise

            return decorator
        try:
            return script(obj, *args, **kwargs)
        except (OSError, RuntimeError) as exc:
            if _is_frozen_source_lookup_error(exc):
                return obj
            raise

    safe_script._voice_input_safe_script = True  # type: ignore[attr-defined]
    safe_script._voice_input_original = script  # type: ignore[attr-defined]
    torch.jit.script = safe_script


def _is_frozen_source_lookup_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "could not get source code" in message
        or "Can't get source" in message
        or "Expected a single top-level function" in message
    )


def _seg_dict_loader() -> Callable[[str], dict[str, str]] | None:
    try:
        from funasr.tokenizer.char_tokenizer import load_seg_dict
    except Exception:
        return _load_seg_dict
    return load_seg_dict


def _candidate_modules() -> list[ModuleType | None]:
    modules: list[ModuleType | None] = []
    for module_name in (
        "funasr.tokenizer.char_tokenizer",
        "funasr.models.seaco_paraformer.model",
        "funasr.models.contextual_paraformer.model",
    ):
        modules.append(_import_optional_module(module_name))
    for name, module in list(sys.modules.items()):
        if not isinstance(module, ModuleType):
            continue
        if name == "funasr.tokenizer.char_tokenizer" or name.startswith("funasr.") and (
            "seaco_paraformer" in name or "contextual_paraformer" in name
        ):
            modules.append(module)
    return modules


def _import_optional_module(module_name: str) -> ModuleType | None:
    try:
        module = __import__(module_name, fromlist=["__name__"])
    except Exception:
        _LAST_IMPORT_ERRORS[module_name] = repr(sys.exc_info()[1])
        return None
    _LAST_IMPORT_ERRORS.pop(module_name, None)
    return module if isinstance(module, ModuleType) else None


def _load_seg_dict(seg_dict_file: str) -> dict[str, str]:
    seg_dict: dict[str, str] = {}
    with open(seg_dict_file, "r", encoding="utf8") as handle:
        for line in handle:
            parts = line.strip().split()
            if len(parts) >= 2:
                seg_dict[parts[0]] = " ".join(parts[1:])
    return seg_dict
