from __future__ import annotations

import sys
import builtins
from pathlib import Path
from types import ModuleType

from voice_input import funasr_compat
from voice_input.funasr_compat import patch_funasr_runtime


def test_registry_imports_include_campplus(monkeypatch) -> None:
    imported = []
    monkeypatch.setattr(funasr_compat, "_import_optional_module", imported.append)

    funasr_compat._import_registry_modules()

    assert "funasr.models.campplus.model" in imported


def test_patch_funasr_runtime_restores_missing_seg_dict_loader(monkeypatch) -> None:
    monkeypatch.delattr(builtins, "load_seg_dict", raising=False)
    tokenizer = ModuleType("funasr.tokenizer.char_tokenizer")

    def load_seg_dict(_path):
        return {"x": "x"}

    tokenizer.load_seg_dict = load_seg_dict
    seaco = ModuleType("funasr.models.seaco_paraformer.model")
    seaco.load_seg_dict = None
    contextual = ModuleType("funasr.models.contextual_paraformer.model")

    monkeypatch.setitem(sys.modules, "funasr", ModuleType("funasr"))
    monkeypatch.setitem(sys.modules, "funasr.tokenizer", ModuleType("funasr.tokenizer"))
    monkeypatch.setitem(sys.modules, "funasr.tokenizer.char_tokenizer", tokenizer)
    monkeypatch.setitem(sys.modules, "funasr.models", ModuleType("funasr.models"))
    monkeypatch.setitem(sys.modules, "funasr.models.seaco_paraformer", ModuleType("funasr.models.seaco_paraformer"))
    monkeypatch.setitem(sys.modules, "funasr.models.seaco_paraformer.model", seaco)
    monkeypatch.setitem(
        sys.modules,
        "funasr.models.contextual_paraformer",
        ModuleType("funasr.models.contextual_paraformer"),
    )
    monkeypatch.setitem(sys.modules, "funasr.models.contextual_paraformer.model", contextual)

    patch_funasr_runtime()

    assert tokenizer.load_seg_dict is load_seg_dict
    assert seaco.load_seg_dict is load_seg_dict
    assert contextual.load_seg_dict is load_seg_dict
    assert builtins.load_seg_dict is load_seg_dict


def test_patch_funasr_runtime_adds_fallback_loader_to_tokenizer(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delattr(builtins, "load_seg_dict", raising=False)
    tokenizer = ModuleType("funasr.tokenizer.char_tokenizer")
    monkeypatch.setitem(sys.modules, "funasr", ModuleType("funasr"))
    monkeypatch.setitem(sys.modules, "funasr.tokenizer", ModuleType("funasr.tokenizer"))
    monkeypatch.setitem(sys.modules, "funasr.tokenizer.char_tokenizer", tokenizer)

    seg_dict = tmp_path / "seg_dict"
    seg_dict.write_text("你 ni\n好 hao\n", encoding="utf-8")

    patch_funasr_runtime()

    assert tokenizer.load_seg_dict(str(seg_dict)) == {"你": "ni", "好": "hao"}
    assert builtins.load_seg_dict(str(seg_dict)) == {"你": "ni", "好": "hao"}
