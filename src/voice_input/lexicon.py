"""Lexicon helpers for transcript replacement and hotword correction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import csv
from pathlib import Path
from typing import Iterable
import json
from io import StringIO


class LexiconRuleType(str, Enum):
    """Supported lexicon behaviors."""

    REPLACE = "replace"
    HOTWORD = "hotword"


@dataclass(frozen=True, slots=True)
class LexiconRule:
    """One text normalization rule."""

    source: str
    target: str
    rule_type: LexiconRuleType = LexiconRuleType.REPLACE
    product_tiers: frozenset[str] = field(default_factory=frozenset)
    weight: int = 0
    enabled: bool = True
    case_sensitive: bool = True
    metadata: dict[str, str] = field(default_factory=dict)
    category: str = ""
    domain: str = ""
    usage: str = ""
    confidence: float = 1.0
    examples: tuple[str, ...] = ()

    def applies_to(self, *, product_tier: str | None = None, enabled_rule_types: set[LexiconRuleType] | None = None) -> bool:
        if not self.enabled or not self.source:
            return False
        if enabled_rule_types is not None and self.rule_type not in enabled_rule_types:
            return False
        if product_tier is None or not self.product_tiers:
            return True
        return product_tier in self.product_tiers

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "LexiconRule":
        source = str(payload.get("source", "")).strip()
        target = str(payload.get("target", ""))
        try:
            rule_type = LexiconRuleType(str(payload.get("rule_type", LexiconRuleType.REPLACE.value)))
        except ValueError as exc:
            raise ValueError(f"unsupported lexicon rule_type: {payload.get('rule_type')!r}") from exc
        metadata = payload.get("metadata", {})
        weight = payload.get("weight", 0)
        enabled = payload.get("enabled", True)
        case_sensitive = payload.get("case_sensitive", True)
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a mapping")
        if not source:
            raise ValueError("lexicon rule is missing source")
        if "target" not in payload:
            raise ValueError(f"lexicon rule {source!r} is missing target")
        return cls(
            source=source,
            target=target,
            rule_type=rule_type,
            product_tiers=_normalize_tiers(payload.get("product_tiers", ())),
            weight=int(weight),
            enabled=_parse_bool(enabled, default=True),
            case_sensitive=_parse_bool(case_sensitive, default=True),
            metadata={str(key): str(value) for key, value in metadata.items()},
            category=str(payload.get("category", "") or metadata.get("category", "")).strip(),
            domain=str(payload.get("domain", "") or metadata.get("domain", "")).strip(),
            usage=str(payload.get("usage", "") or metadata.get("usage", "")).strip(),
            confidence=_parse_confidence(payload.get("confidence", metadata.get("confidence", 1.0))),
            examples=_normalize_examples(payload.get("examples", ())),
        )


@dataclass(frozen=True, slots=True)
class Lexicon:
    """A composable lexicon that can be packaged per product tier."""

    rules: tuple[LexiconRule, ...] = ()

    def normalize(
        self,
        text: str,
        *,
        product_tier: str | None = None,
        enabled_rule_types: Iterable[LexiconRuleType] | None = None,
    ) -> str:
        active_types = set(enabled_rule_types) if enabled_rule_types is not None else None
        return apply_rules(
            text,
            self.iter_rules(product_tier=product_tier, enabled_rule_types=active_types),
        )

    def iter_rules(
        self,
        *,
        product_tier: str | None = None,
        enabled_rule_types: set[LexiconRuleType] | None = None,
    ) -> tuple[LexiconRule, ...]:
        active_rules = [
            rule
            for rule in self.rules
            if rule.applies_to(product_tier=product_tier, enabled_rule_types=enabled_rule_types)
        ]
        active_rules.sort(key=lambda rule: (rule.weight, len(rule.source)), reverse=True)
        return tuple(active_rules)

    def merge(self, *others: "Lexicon") -> "Lexicon":
        merged_rules = list(self.rules)
        for other in others:
            merged_rules.extend(other.rules)
        return Lexicon(rules=tuple(merged_rules))

    def canonical_terms(
        self,
        *,
        product_tier: str | None = None,
        enabled_rule_types: Iterable[LexiconRuleType] | None = None,
        limit: int | None = None,
    ) -> tuple[str, ...]:
        """Return deduplicated target terms suitable for ASR hotword boosting."""

        active_types = set(enabled_rule_types) if enabled_rule_types is not None else None
        terms: list[str] = []
        for rule in self.iter_rules(product_tier=product_tier, enabled_rule_types=active_types):
            term = rule.target.strip()
            if term and term not in terms:
                terms.append(term)
                if limit is not None and len(terms) >= limit:
                    break
        return tuple(terms)

    def with_weight_offset(self, offset: int) -> "Lexicon":
        if not offset:
            return self
        return Lexicon(
            rules=tuple(
                LexiconRule(
                    source=rule.source,
                    target=rule.target,
                    rule_type=rule.rule_type,
                    product_tiers=rule.product_tiers,
                    weight=rule.weight + offset,
                    enabled=rule.enabled,
                    case_sensitive=rule.case_sensitive,
                    metadata=rule.metadata,
                    category=rule.category,
                    domain=rule.domain,
                    usage=rule.usage,
                    confidence=rule.confidence,
                    examples=rule.examples,
                )
                for rule in self.rules
            )
        )

    @classmethod
    def from_files(
        cls,
        paths: Iterable[str | Path],
        *,
        rule_type: LexiconRuleType = LexiconRuleType.REPLACE,
        product_tiers: Iterable[str] = (),
        separator: str = "=>",
        encoding: str = "utf-8",
        weight: int = 0,
    ) -> "Lexicon":
        lexicon = cls()
        for path in paths:
            lexicon = lexicon.merge(
                cls.from_file(
                    path,
                    rule_type=rule_type,
                    product_tiers=product_tiers,
                    separator=separator,
                    encoding=encoding,
                    weight=weight,
                )
            )
        return lexicon

    @classmethod
    def from_pairs(
        cls,
        replacements: dict[str, str],
        *,
        rule_type: LexiconRuleType = LexiconRuleType.REPLACE,
        product_tiers: Iterable[str] = (),
        weight: int = 0,
    ) -> "Lexicon":
        tiers = frozenset(str(item).strip() for item in product_tiers if str(item).strip())
        rules = tuple(
            LexiconRule(
                source=source,
                target=target,
                rule_type=rule_type,
                product_tiers=tiers,
                weight=weight,
            )
            for source, target in replacements.items()
        )
        return cls(rules=rules)

    @classmethod
    def from_json(cls, payload: str) -> "Lexicon":
        data = json.loads(payload)
        if not isinstance(data, list):
            raise ValueError("lexicon json must be a list of rules")
        rules: list[LexiconRule] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("lexicon json rules must be objects")
            rule = LexiconRule.from_mapping(item)
            rules.append(rule)
            for alias in _normalize_aliases(item.get("aliases", ())):
                if alias == rule.source:
                    continue
                rules.append(
                    LexiconRule(
                        source=alias,
                        target=rule.target,
                        rule_type=rule.rule_type,
                        product_tiers=rule.product_tiers,
                        weight=max(rule.weight - 1, 0),
                        enabled=rule.enabled,
                        case_sensitive=rule.case_sensitive,
                        metadata={**rule.metadata, "alias_of": rule.source},
                        category=rule.category,
                        domain=rule.domain,
                        usage=rule.usage,
                        confidence=rule.confidence,
                        examples=rule.examples,
                    )
                )
        return cls(rules=tuple(rules))

    @classmethod
    def from_csv(
        cls,
        payload: str,
        *,
        rule_type: LexiconRuleType = LexiconRuleType.REPLACE,
        product_tiers: Iterable[str] = (),
        weight: int = 0,
        encoding: str = "utf-8",
    ) -> "Lexicon":
        tiers = _normalize_tiers(product_tiers)
        reader = csv.DictReader(StringIO(payload))
        if reader.fieldnames is None:
            raise ValueError("lexicon csv must include a header row")
        required_fields = {"source", "target"}
        missing = required_fields - {str(field).strip().lower() for field in reader.fieldnames}
        if missing:
            raise ValueError(f"lexicon csv is missing required columns: {', '.join(sorted(missing))}")
        rules: list[LexiconRule] = []
        for row_number, row in enumerate(reader, start=2):
            normalized_row = _normalize_csv_row(row)
            source = str(normalized_row.get("source", "")).strip()
            target = str(normalized_row.get("target", ""))
            if not source and not target and all(not str(value).strip() for value in row.values() if value is not None):
                continue
            if not source:
                raise ValueError(f"lexicon csv row {row_number} is missing source")
            if "target" not in normalized_row or normalized_row.get("target") is None:
                raise ValueError(f"lexicon csv row {row_number} is missing target")
            try:
                row_rule_type = LexiconRuleType(
                    str(normalized_row.get("rule_type") or normalized_row.get("type") or rule_type.value).strip()
                )
            except ValueError as exc:
                raise ValueError(f"lexicon csv row {row_number} has unsupported rule_type") from exc
            row_tiers = _normalize_delimited_values(normalized_row.get("product_tiers") or normalized_row.get("tiers"))
            base_rule = LexiconRule(
                source=source,
                target=target,
                rule_type=row_rule_type,
                product_tiers=frozenset(row_tiers) or tiers,
                weight=_parse_int(normalized_row.get("weight"), default=weight),
                enabled=_parse_bool(normalized_row.get("enabled"), default=True),
                case_sensitive=_parse_bool(normalized_row.get("case_sensitive"), default=True),
                metadata=_metadata_from_csv_row(normalized_row),
                category=str(normalized_row.get("category", "")).strip(),
                domain=str(normalized_row.get("domain", "")).strip(),
                usage=str(normalized_row.get("usage", "")).strip(),
                confidence=_parse_confidence(normalized_row.get("confidence", 1.0)),
                examples=_normalize_examples(_normalize_delimited_values(normalized_row.get("examples"))),
            )
            rules.append(base_rule)
            for alias in _normalize_delimited_values(normalized_row.get("aliases")):
                if alias == base_rule.source:
                    continue
                rules.append(
                    LexiconRule(
                        source=alias,
                        target=base_rule.target,
                        rule_type=base_rule.rule_type,
                        product_tiers=base_rule.product_tiers,
                        weight=max(base_rule.weight - 1, 0),
                        enabled=base_rule.enabled,
                        case_sensitive=base_rule.case_sensitive,
                        metadata={**base_rule.metadata, "alias_of": base_rule.source},
                        category=base_rule.category,
                        domain=base_rule.domain,
                        usage=base_rule.usage,
                        confidence=base_rule.confidence,
                        examples=base_rule.examples,
                    )
                )
        return cls(rules=tuple(rules))

    @classmethod
    def from_lines(
        cls,
        lines: Iterable[str],
        *,
        rule_type: LexiconRuleType = LexiconRuleType.REPLACE,
        product_tiers: Iterable[str] = (),
        separator: str = "=>",
        weight: int = 0,
    ) -> "Lexicon":
        tiers = _normalize_tiers(product_tiers)
        rules: list[LexiconRule] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if separator not in stripped:
                raise ValueError(f"lexicon line must contain {separator!r}: {line!r}")
            source, target = stripped.split(separator, 1)
            rules.append(
                LexiconRule(
                    source=source.strip(),
                    target=target.strip(),
                    rule_type=rule_type,
                    product_tiers=tiers,
                    weight=weight,
                )
            )
        return cls(rules=tuple(rules))

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        rule_type: LexiconRuleType = LexiconRuleType.REPLACE,
        product_tiers: Iterable[str] = (),
        separator: str = "=>",
        encoding: str = "utf-8",
        weight: int = 0,
    ) -> "Lexicon":
        source_path = Path(path)
        payload = source_path.read_text(encoding=encoding)
        suffix = source_path.suffix.lower()
        if suffix == ".json":
            return cls.from_json(payload).with_weight_offset(weight)
        if suffix == ".csv":
            return cls.from_csv(
                payload,
                rule_type=rule_type,
                product_tiers=product_tiers,
                weight=weight,
            )
        return cls.from_lines(
            payload.splitlines(),
            rule_type=rule_type,
            product_tiers=product_tiers,
            separator=separator,
            weight=weight,
        )


def _apply_rule(text: str, rule: LexiconRule) -> str:
    if rule.case_sensitive:
        return text.replace(rule.source, rule.target)
    if not rule.source:
        return text

    source = rule.source.casefold()
    index = 0
    chunks: list[str] = []
    while index < len(text):
        matched = text[index : index + len(rule.source)]
        if matched.casefold() == source:
            chunks.append(rule.target)
            index += len(rule.source)
            continue
        chunks.append(text[index])
        index += 1
    return "".join(chunks)


def apply_rules(text: str, rules: Iterable[LexiconRule]) -> str:
    """Apply lexicon rules in a single pass, preferring earlier rules."""

    ordered_rules = tuple(rule for rule in rules if rule.enabled and rule.source)
    if not ordered_rules:
        return text

    index = 0
    chunks: list[str] = []
    while index < len(text):
        match = _first_match(text, index, ordered_rules)
        if match is None:
            chunks.append(text[index])
            index += 1
            continue
        chunks.append(match.target)
        index += len(match.source)
    return "".join(chunks)


def _first_match(text: str, index: int, rules: tuple[LexiconRule, ...]) -> LexiconRule | None:
    for rule in rules:
        candidate = text[index : index + len(rule.source)]
        if rule.case_sensitive and candidate == rule.source:
            return rule
        if not rule.case_sensitive and candidate.casefold() == rule.source.casefold():
            return rule
    return None


def _normalize_tiers(value: object) -> frozenset[str]:
    if isinstance(value, str):
        values: Iterable[object] = (value,)
    else:
        values = value if isinstance(value, Iterable) else ()
    return frozenset(str(item).strip() for item in values if str(item).strip())


def _normalize_csv_row(row: dict[str, str | None]) -> dict[str, str]:
    return {
        str(key).strip().lower(): str(value).strip()
        for key, value in row.items()
        if key is not None and value is not None
    }


def _normalize_delimited_values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values: Iterable[object] = value.replace("；", ";").replace("，", ",").replace("|", ";").split(";")
        values: list[str] = []
        for item in raw_values:
            values.extend(str(item).split(","))
    elif isinstance(value, Iterable):
        values = [str(item) for item in value]
    else:
        values = ()
    normalized: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _metadata_from_csv_row(row: dict[str, str]) -> dict[str, str]:
    reserved = {
        "source",
        "target",
        "rule_type",
        "type",
        "product_tiers",
        "tiers",
        "weight",
        "enabled",
        "case_sensitive",
        "category",
        "domain",
        "usage",
        "confidence",
        "examples",
        "aliases",
    }
    return {key: value for key, value in row.items() if key not in reserved and value}


def _parse_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on", "启用", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "禁用", "否"}:
        return False
    return default


def _parse_int(value: object, *, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _parse_confidence(value: object) -> float:
    if value is None or str(value).strip() == "":
        return 1.0
    confidence = float(value)
    return max(0.0, min(1.0, confidence))


def _normalize_aliases(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[object] = (value,)
    elif isinstance(value, Iterable):
        values = value
    else:
        values = ()
    aliases: list[str] = []
    for item in values:
        alias = str(item).strip()
        if alias and alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def _normalize_examples(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[object] = (value,)
    elif isinstance(value, Iterable):
        values = value
    else:
        values = ()
    examples: list[str] = []
    for item in values:
        example = str(item).strip()
        if example and example not in examples:
            examples.append(example)
    return tuple(examples)
