"""Local-only FunASR model resolver for packaged offline builds.

The desktop product ships its FunASR model directories side by side with the
application. Keeping ModelScope/HuggingFace download code in the frozen app
pulls a large dependency graph into the package and is not needed at runtime.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from omegaconf import DictConfig, OmegaConf


def download_model(**kwargs: Any) -> dict[str, Any]:
    model_or_path = str(kwargs.get("model_path") or kwargs.get("model") or "").strip()
    if not model_or_path:
        raise FileNotFoundError("FunASR packaged build requires a local model directory.")
    if not os.path.exists(model_or_path):
        raise FileNotFoundError(
            f"FunASR packaged build only supports bundled local models: {model_or_path}"
        )

    kwargs["model_path"] = model_or_path
    if os.path.exists(os.path.join(model_or_path, "configuration.json")):
        kwargs = _load_configuration_json(model_or_path, kwargs)
    elif os.path.exists(os.path.join(model_or_path, "config.yaml")):
        kwargs = _load_config_yaml(model_or_path, kwargs)

    if isinstance(kwargs, DictConfig):
        kwargs = OmegaConf.to_container(kwargs, resolve=True)
    logging.warning("trust_remote_code: %s", kwargs.get("trust_remote_code", False))
    return dict(kwargs)


def _load_configuration_json(model_or_path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    with open(os.path.join(model_or_path, "configuration.json"), "r", encoding="utf-8") as handle:
        conf_json = json.load(handle)

    cfg: dict[str, Any] = {}
    if "file_path_metas" in conf_json:
        add_file_root_path(model_or_path, conf_json["file_path_metas"], cfg)
    merged = OmegaConf.merge(cfg, kwargs)
    if "config" in merged:
        config = OmegaConf.load(merged["config"])
        merged = OmegaConf.merge(config, merged)
        merged["model"] = config["model"]
    return OmegaConf.to_container(merged, resolve=True)


def _load_config_yaml(model_or_path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    config = OmegaConf.load(os.path.join(model_or_path, "config.yaml"))
    merged = OmegaConf.merge(config, kwargs)
    init_param = os.path.join(model_or_path, "model.pt")
    if "init_param" not in merged or not os.path.exists(str(merged["init_param"])):
        merged["init_param"] = init_param
    if os.path.exists(os.path.join(model_or_path, "tokens.txt")):
        merged["tokenizer_conf"]["token_list"] = os.path.join(model_or_path, "tokens.txt")
    if os.path.exists(os.path.join(model_or_path, "tokens.json")):
        merged["tokenizer_conf"]["token_list"] = os.path.join(model_or_path, "tokens.json")
    if os.path.exists(os.path.join(model_or_path, "seg_dict")):
        merged["tokenizer_conf"]["seg_dict"] = os.path.join(model_or_path, "seg_dict")
    if os.path.exists(os.path.join(model_or_path, "bpe.model")):
        merged["tokenizer_conf"]["bpemodel"] = os.path.join(model_or_path, "bpe.model")
    merged["model"] = config["model"]
    if os.path.exists(os.path.join(model_or_path, "am.mvn")):
        merged["frontend_conf"]["cmvn_file"] = os.path.join(model_or_path, "am.mvn")
    if os.path.exists(os.path.join(model_or_path, "jieba_usr_dict")):
        merged["jieba_usr_dict"] = os.path.join(model_or_path, "jieba_usr_dict")
    return OmegaConf.to_container(merged, resolve=True)


def add_file_root_path(model_or_path: str, file_path_metas: dict[str, Any], cfg: Any) -> Any:
    if not isinstance(file_path_metas, dict):
        return cfg
    if isinstance(cfg, list):
        cfg.append({})

    for key, value in file_path_metas.items():
        if isinstance(value, str):
            path = os.path.join(model_or_path, value)
            if os.path.exists(path):
                if isinstance(cfg, dict):
                    cfg[key] = path
                elif isinstance(cfg, list):
                    cfg[-1][key] = path
        elif isinstance(value, dict):
            if isinstance(cfg, dict):
                cfg.setdefault(key, {})
                add_file_root_path(model_or_path, value, cfg[key])
        elif isinstance(value, (list, tuple)):
            if isinstance(cfg, dict):
                cfg.setdefault(key, [])
                for item in value:
                    if isinstance(item, str):
                        path = os.path.join(model_or_path, item)
                        if os.path.exists(path):
                            cfg[key].append(path)
                    elif isinstance(item, dict):
                        add_file_root_path(model_or_path, item, cfg[key])
    return cfg
