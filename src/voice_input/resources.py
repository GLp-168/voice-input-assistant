"""Runtime discovery for bundled offline ASR resources."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import sys


DEFAULT_WHISPER_MODEL = "ggml-base.bin"
WHISPER_EXECUTABLE = "whisper-cli.exe" if sys.platform.startswith("win") else "whisper-cli"
DEFAULT_SHERPA_STREAMING_MODEL_DIR = "sherpa-onnx-streaming-zipformer-ctc-zh-xlarge-int8-2025-06-30"
DEFAULT_FUNASR_STREAMING_MODEL_DIR = "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
DEFAULT_FUNASR_OFFLINE_MODEL_DIR = "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
DEFAULT_FUNASR_VAD_MODEL_DIR = "speech_fsmn_vad_zh-cn-16k-common-pytorch"
DEFAULT_FUNASR_PUNC_MODEL_DIR = "punc_ct-transformer_cn-en-common-vocab471067-large"
DEFAULT_FUNASR_COMPACT_PUNC_MODEL_DIR = "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
DEFAULT_FUNASR_SPEAKER_MODEL_DIR = "speech_campplus_sv_zh-cn_16k-common"
FUNASR_STREAMING_MODEL_ALIAS = "paraformer-zh-streaming"
FUNASR_OFFLINE_MODEL_ALIAS = "paraformer-zh"
FUNASR_VAD_MODEL_ALIAS = "fsmn-vad"
FUNASR_PUNC_MODEL_ALIAS = "ct-punc"
DEFAULT_FUNASR_MODEL_ROLES = {
    "streaming": DEFAULT_FUNASR_STREAMING_MODEL_DIR,
    "offline": DEFAULT_FUNASR_OFFLINE_MODEL_DIR,
    "vad": DEFAULT_FUNASR_VAD_MODEL_DIR,
    "punc": DEFAULT_FUNASR_PUNC_MODEL_DIR,
    "speaker": DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
}
FUNASR_MODEL_DIRS = (
    DEFAULT_FUNASR_STREAMING_MODEL_DIR,
    DEFAULT_FUNASR_OFFLINE_MODEL_DIR,
    DEFAULT_FUNASR_VAD_MODEL_DIR,
    DEFAULT_FUNASR_PUNC_MODEL_DIR,
    DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
)
FUNASR_KNOWN_MODEL_DIRS = (
    *FUNASR_MODEL_DIRS,
    DEFAULT_FUNASR_COMPACT_PUNC_MODEL_DIR,
)
FUNASR_MODEL_REQUIRED_FILES = {
    DEFAULT_FUNASR_STREAMING_MODEL_DIR: (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "seg_dict",
        "am.mvn",
    ),
    DEFAULT_FUNASR_OFFLINE_MODEL_DIR: (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "seg_dict",
        "am.mvn",
    ),
    DEFAULT_FUNASR_VAD_MODEL_DIR: (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "am.mvn",
    ),
    DEFAULT_FUNASR_PUNC_MODEL_DIR: (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "jieba_usr_dict",
    ),
    DEFAULT_FUNASR_COMPACT_PUNC_MODEL_DIR: (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
    ),
    DEFAULT_FUNASR_SPEAKER_MODEL_DIR: (
        "configuration.json",
        "config.yaml",
        "campplus_cn_common.bin",
    ),
}
SHERPA_STREAMING_CTC_MODEL_FILES = (
    "model.int8.onnx",
    "tokens.txt",
)
SHERPA_STREAMING_TRANSDUCER_MODEL_FILES = (
    "encoder-epoch-99-avg-1.int8.onnx",
    "decoder-epoch-99-avg-1.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
    "tokens.txt",
)
SHERPA_STREAMING_MODEL_FILES = SHERPA_STREAMING_CTC_MODEL_FILES
PRODUCT_TIER_ORDER = {"basic": 0, "standard": 1, "enterprise": 2}
DEFAULT_PRODUCT_TIER = "enterprise"
DEFAULT_MODEL_PROFILE = "full"
MODEL_PROFILES = {"streaming", "full"}
LEXICON_CATALOG_FILENAME = "catalog.json"
SUPPORTED_LEXICON_SUFFIXES = {".csv", ".json", ".txt"}
_LEXICON_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class BundledLexiconInfo:
    id: str
    title: str
    description: str
    filename: str
    default_enabled: bool = True
    min_tier: str = "basic"
    tags: tuple[str, ...] = ()


BUNDLED_LEXICONS = (
    BundledLexiconInfo(
        id="government_digital",
        title="政务/数字政府",
        description="政务服务、数字政府、营商环境等常用表达纠错。",
        filename="government_digital.json",
        min_tier="basic",
        tags=("government", "digital-government"),
    ),
    BundledLexiconInfo(
        id="official_document",
        title="公文规范",
        description="公文要素、文种、格式术语等常见误识别修正。",
        filename="official_document.json",
        min_tier="basic",
        tags=("official-document", "government"),
    ),
    BundledLexiconInfo(
        id="office_meeting",
        title="办公会议",
        description="会议纪要、议题议程、项目协作等办公场景词汇。",
        filename="office_meeting.json",
        min_tier="basic",
        tags=("office-meeting", "project-management"),
    ),
    BundledLexiconInfo(
        id="finance_budget",
        title="财税/预算",
        description="专项债、预算、税务、采购、国库支付等财政场景词汇。",
        filename="finance_budget.json",
        min_tier="basic",
        tags=("finance", "budget", "tax"),
    ),
    BundledLexiconInfo(
        id="project_engineering",
        title="工程/招投标",
        description="招投标、工程量清单、施工、验收、结算等项目工程词汇。",
        filename="project_engineering.json",
        min_tier="basic",
        tags=("engineering", "project", "bidding"),
    ),
)
BUNDLED_LEXICON_FILES = tuple(item.filename for item in BUNDLED_LEXICONS)
DEFAULT_ENABLED_BUILTIN_LEXICONS = tuple(item.id for item in BUNDLED_LEXICONS if item.default_enabled)


def app_base_dir() -> Path:
    """Return the directory where side-by-side packaged resources live."""

    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
        for candidate in (start, *start.parents):
            if (
                (candidate / "models" / DEFAULT_WHISPER_MODEL).is_file()
                or (candidate / "models" / DEFAULT_SHERPA_STREAMING_MODEL_DIR / "tokens.txt").is_file()
                or (candidate / "models" / "funasr" / DEFAULT_FUNASR_STREAMING_MODEL_DIR).is_dir()
                or (candidate / "engines" / "whispercpp" / "bin" / WHISPER_EXECUTABLE).is_file()
                or (candidate / "tools" / "VoiceInputDiagnose" / "VoiceInputDiagnose.exe").is_file()
            ):
                return candidate
        return start
    installed_resources = Path(__file__).resolve().parent / "bundled"
    if installed_resources.is_dir():
        return installed_resources
    return Path(__file__).resolve().parents[2]


def bundled_whisper_model() -> Path:
    candidates = [
        app_base_dir() / "models" / DEFAULT_WHISPER_MODEL,
        app_base_dir() / "vendor" / "whispercpp" / "models" / DEFAULT_WHISPER_MODEL,
    ]
    return _first_existing(candidates, candidates[0])


def bundled_whisper_executable() -> Path:
    candidates = [
        app_base_dir() / "engines" / "whispercpp" / "bin" / WHISPER_EXECUTABLE,
        app_base_dir() / "vendor" / "whispercpp" / "bin" / "win-x64" / WHISPER_EXECUTABLE,
    ]
    return _first_existing(candidates, candidates[0])


def has_bundled_whisper() -> bool:
    return bundled_whisper_model().is_file() and bundled_whisper_executable().is_file()


def bundled_sherpa_streaming_model_dir() -> Path:
    candidates = [
        app_base_dir() / "models" / DEFAULT_SHERPA_STREAMING_MODEL_DIR,
        app_base_dir() / "vendor" / "sherpa-onnx" / "models" / DEFAULT_SHERPA_STREAMING_MODEL_DIR,
    ]
    return _first_existing_dir(candidates, candidates[0])


def has_bundled_sherpa_streaming_model() -> bool:
    model_dir = bundled_sherpa_streaming_model_dir()
    return all((model_dir / filename).is_file() for filename in SHERPA_STREAMING_MODEL_FILES)


def bundled_funasr_model_root() -> Path:
    candidates = [
        app_base_dir() / "models" / "funasr",
        app_base_dir() / "vendor" / "funasr" / "models",
    ]
    return _first_existing_dir(candidates, candidates[0])


def bundled_funasr_model_dir(model_dir_name: str) -> Path:
    return bundled_funasr_model_root() / model_dir_name


def bundled_funasr_streaming_model() -> str:
    return _bundled_funasr_role_model_or_alias("streaming", FUNASR_STREAMING_MODEL_ALIAS)


def bundled_funasr_offline_model() -> str:
    return _bundled_funasr_role_model_or_alias("offline", FUNASR_OFFLINE_MODEL_ALIAS)


def bundled_funasr_vad_model() -> str:
    return _bundled_funasr_role_model_or_alias("vad", FUNASR_VAD_MODEL_ALIAS)


def bundled_funasr_punc_model() -> str:
    return _bundled_funasr_role_model_or_alias("punc", FUNASR_PUNC_MODEL_ALIAS)


def bundled_funasr_speaker_model() -> str:
    dirname = packaged_funasr_model_roles().get("speaker", "")
    if not dirname:
        return ""
    candidate = bundled_funasr_model_root() / dirname
    return str(candidate) if _looks_like_funasr_model_dir(candidate) else ""


def has_bundled_funasr_models() -> bool:
    root = bundled_funasr_model_root()
    model_dirs = packaged_funasr_model_dirs()
    return bool(model_dirs) and all(_looks_like_funasr_model_dir(root / dirname) for dirname in model_dirs)


def has_bundled_funasr_streaming_model() -> bool:
    dirname = packaged_funasr_model_roles().get("streaming", "")
    return bool(dirname) and _looks_like_funasr_model_dir(bundled_funasr_model_root() / dirname)


def has_bundled_funasr_review_models(*, require_punctuation: bool = False) -> bool:
    roles = packaged_funasr_model_roles()
    required_roles = ("offline", "vad", "punc") if require_punctuation else ("offline", "vad")
    root = bundled_funasr_model_root()
    for role in required_roles:
        dirname = roles.get(role, "")
        if not dirname or not _looks_like_funasr_model_dir(root / dirname):
            return False
    return True


def has_bundled_funasr_speaker_model() -> bool:
    dirname = packaged_funasr_model_roles().get("speaker", "")
    return bool(dirname) and _looks_like_funasr_model_dir(bundled_funasr_model_root() / dirname)


def funasr_model_file_status(path: Path, dirname: str | None = None) -> dict[str, bool]:
    required_files = FUNASR_MODEL_REQUIRED_FILES.get(dirname or path.name, ("configuration.json",))
    return {filename: _is_nonempty_file(path / filename) for filename in required_files}


def bundled_app_icon() -> Path:
    candidates = [
        app_base_dir() / "assets" / "icons" / "app.ico",
        app_base_dir() / "_internal" / "assets" / "icons" / "app.ico",
    ]
    return _first_existing(candidates, candidates[0])


def bundled_lexicon_dir() -> Path:
    candidates = [
        app_base_dir() / "assets" / "lexicons",
        app_base_dir() / "_internal" / "assets" / "lexicons",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def bundled_lexicon_catalog_path() -> Path:
    return bundled_lexicon_dir() / LEXICON_CATALOG_FILENAME


def lexicon_catalog_from_dir(
    root: Path | str,
    *,
    catalog_path: Path | str | None = None,
    product_tier: str | None = None,
    strict: bool = False,
) -> tuple[BundledLexiconInfo, ...]:
    """Read a developer-maintained lexicon catalog from a lexicon asset directory."""

    catalog = _load_lexicon_catalog(Path(root), strict=strict, catalog_path=Path(catalog_path) if catalog_path else None)
    if product_tier is None:
        return catalog
    normalized_tier = _normalize_product_tier(product_tier, default="standard")
    return tuple(info for info in catalog if _tier_allows(normalized_tier, info.min_tier))


def bundled_product_profile() -> dict[str, object]:
    candidates = [
        app_base_dir() / "assets" / "product.json",
        app_base_dir() / "_internal" / "assets" / "product.json",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def packaged_product_tier() -> str:
    profile = bundled_product_profile()
    return _normalize_product_tier(profile.get("product_tier") or profile.get("tier"))


def packaged_model_profile() -> str:
    profile = bundled_product_profile()
    value = str(profile.get("model_profile") or "").strip().lower()
    if value in MODEL_PROFILES:
        return value
    roles = packaged_funasr_model_roles()
    if roles.get("offline") and roles.get("vad"):
        return "full"
    if roles.get("streaming"):
        return "streaming"
    return DEFAULT_MODEL_PROFILE


def packaged_funasr_model_roles() -> dict[str, str]:
    profile = bundled_product_profile()
    raw_roles = profile.get("funasr_models")
    if not isinstance(raw_roles, dict):
        return dict(DEFAULT_FUNASR_MODEL_ROLES)
    roles: dict[str, str] = {}
    for role in ("streaming", "offline", "vad", "punc", "speaker"):
        value = str(raw_roles.get(role) or "").strip()
        if value:
            roles[role] = value
    return roles


def packaged_funasr_model_dirs() -> tuple[str, ...]:
    dirs: list[str] = []
    raw_dirs = bundled_product_profile().get("bundled_funasr_models")
    if isinstance(raw_dirs, (list, tuple)):
        for item in raw_dirs:
            dirname = str(item or "").strip()
            if dirname and dirname not in dirs:
                dirs.append(dirname)
    for dirname in packaged_funasr_model_roles().values():
        if dirname and dirname not in dirs:
            dirs.append(dirname)
    return tuple(dirs)


def packaged_enabled_builtin_lexicons() -> tuple[str, ...]:
    profile = bundled_product_profile()
    raw_value = profile.get("enabled_builtin_lexicons")
    if raw_value is None:
        return default_enabled_builtin_lexicon_ids(product_tier=packaged_product_tier())
    selected = set(_iter_setting_values(raw_value))
    return tuple(
        item.id
        for item in bundled_lexicon_catalog(product_tier=packaged_product_tier())
        if item.id in selected
    )


def default_enabled_builtin_lexicon_ids(*, product_tier: str | None = None) -> tuple[str, ...]:
    return tuple(item.id for item in bundled_lexicon_catalog(product_tier=product_tier) if item.default_enabled)


def bundled_lexicon_catalog(*, product_tier: str | None = None) -> tuple[BundledLexiconInfo, ...]:
    catalog = _load_lexicon_catalog(bundled_lexicon_dir(), strict=False)
    if product_tier is None:
        return catalog
    normalized_tier = _normalize_product_tier(product_tier, default="standard")
    return tuple(info for info in catalog if _tier_allows(normalized_tier, info.min_tier))


def bundled_lexicon_paths(
    enabled_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    *,
    product_tier: str | None = None,
) -> tuple[Path, ...]:
    root = bundled_lexicon_dir()
    selected = set(default_enabled_builtin_lexicon_ids(product_tier=product_tier) if enabled_ids is None else enabled_ids)
    return tuple(
        path
        for info in bundled_lexicon_catalog(product_tier=product_tier)
        if info.id in selected and (path := root / info.filename).is_file()
    )


def effective_product_tier(current_tier: str | None, enabled_builtin_lexicons: object) -> str:
    """Return the minimum hidden tier needed by the selected built-in lexicons."""

    normalized = _normalize_product_tier(current_tier)
    selected = set(_iter_setting_values(enabled_builtin_lexicons))
    minimum_rank = PRODUCT_TIER_ORDER[normalized]
    for info in bundled_lexicon_catalog():
        if info.id in selected:
            minimum_rank = max(minimum_rank, PRODUCT_TIER_ORDER.get(info.min_tier, minimum_rank))
    for tier, rank in PRODUCT_TIER_ORDER.items():
        if rank == minimum_rank:
            return tier
    return normalized


def _load_lexicon_catalog(
    root: Path,
    *,
    strict: bool,
    catalog_path: Path | None = None,
) -> tuple[BundledLexiconInfo, ...]:
    manifest = catalog_path or (root / LEXICON_CATALOG_FILENAME)
    if not manifest.is_file():
        return BUNDLED_LEXICONS
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        return _parse_lexicon_catalog(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        if strict:
            raise
        return BUNDLED_LEXICONS


def _parse_lexicon_catalog(payload: object) -> tuple[BundledLexiconInfo, ...]:
    if not isinstance(payload, dict):
        raise ValueError("lexicon catalog must be a JSON object")
    raw_items = payload.get("lexicons")
    if not isinstance(raw_items, list):
        raise ValueError("lexicon catalog must contain a lexicons list")
    items: list[BundledLexiconInfo] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"lexicon catalog item {index} must be an object")
        info = _lexicon_info_from_mapping(raw_item, index=index)
        if info.id in seen:
            raise ValueError(f"duplicate lexicon catalog id: {info.id}")
        seen.add(info.id)
        items.append(info)
    if not items:
        raise ValueError("lexicon catalog must contain at least one lexicon")
    return tuple(items)


def _lexicon_info_from_mapping(payload: dict[str, object], *, index: int) -> BundledLexiconInfo:
    lexicon_id = str(payload.get("id") or "").strip()
    if not _LEXICON_ID_PATTERN.match(lexicon_id):
        raise ValueError(f"lexicon catalog item {index} has invalid id: {lexicon_id!r}")
    filename = _normalize_lexicon_filename(payload.get("filename"))
    title = str(payload.get("title") or lexicon_id).strip()
    description = str(payload.get("description") or "").strip()
    default_enabled = _bool_from_catalog_value(payload.get("default_enabled"), default=True)
    min_tier = _normalize_product_tier(payload.get("min_tier"), default="basic")
    raw_tags = payload.get("tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, (list, tuple)):
        tags = tuple(str(tag).strip() for tag in raw_tags if str(tag).strip())
    return BundledLexiconInfo(
        id=lexicon_id,
        title=title,
        description=description,
        filename=filename,
        default_enabled=default_enabled,
        min_tier=min_tier,
        tags=tags,
    )


def _normalize_lexicon_filename(value: object) -> str:
    filename = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(filename)
    if not filename or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe lexicon filename: {filename!r}")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError(f"unsafe lexicon filename: {filename!r}")
    if path.suffix.lower() not in SUPPORTED_LEXICON_SUFFIXES:
        raise ValueError(f"unsupported lexicon filename suffix: {filename!r}")
    return path.as_posix()


def _bool_from_catalog_value(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


def _first_existing(candidates: list[Path], fallback: Path) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return fallback


def _first_existing_dir(candidates: list[Path], fallback: Path) -> Path:
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return fallback


def _bundled_funasr_model_or_alias(model_dir_name: str, alias: str) -> str:
    model_dir = bundled_funasr_model_dir(model_dir_name)
    if _looks_like_funasr_model_dir(model_dir):
        return str(model_dir)
    return alias


def _bundled_funasr_role_model_or_alias(role: str, alias: str) -> str:
    dirname = packaged_funasr_model_roles().get(role, "")
    if not dirname:
        return ""
    model = _bundled_funasr_model_or_alias(dirname, alias)
    if model == alias and (bundled_product_profile() or bool(getattr(sys, "frozen", False))):
        return ""
    return model


def _looks_like_funasr_model_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    required_files = FUNASR_MODEL_REQUIRED_FILES.get(path.name, ("configuration.json",))
    return all(_is_nonempty_file(path / filename) for filename in required_files)


def _is_nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _tier_allows(product_tier: str, minimum_tier: str) -> bool:
    return _tier_rank(product_tier) >= _tier_rank(minimum_tier)


def _tier_rank(value: str) -> int:
    return PRODUCT_TIER_ORDER.get(value.strip().lower(), PRODUCT_TIER_ORDER["standard"])


def _normalize_product_tier(value: object, *, default: str = DEFAULT_PRODUCT_TIER) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in PRODUCT_TIER_ORDER:
        return normalized
    return default


def _iter_setting_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value)
    return ()
