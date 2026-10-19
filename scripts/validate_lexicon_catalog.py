from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from voice_input.lexicon import Lexicon  # noqa: E402
from voice_input.resources import lexicon_catalog_from_dir  # noqa: E402


TIER_RANK = {"basic": 0, "standard": 1, "enterprise": 2}


def validate_catalog(
    lexicon_dir: Path | str,
    *,
    catalog_path: Path | str | None = None,
    product_tier: str = "enterprise",
    enabled_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    root = Path(lexicon_dir).resolve()
    tier = _normalize_tier(product_tier)
    errors: list[str] = []
    warnings: list[str] = []
    files: list[dict[str, object]] = []

    if not root.is_dir():
        return {
            "ok": False,
            "product_tier": tier,
            "lexicon_dir": str(root),
            "catalog": str(Path(catalog_path).resolve()) if catalog_path else str(root / "catalog.json"),
            "selected_ids": [],
            "files": [],
            "totals": {"catalog": 0, "selected": 0, "rules": 0, "active_rules": 0, "hotwords": 0},
            "errors": [f"lexicon directory does not exist: {root}"],
            "warnings": [],
        }

    try:
        catalog = lexicon_catalog_from_dir(
            root,
            catalog_path=Path(catalog_path).resolve() if catalog_path else None,
            strict=True,
        )
    except Exception as exc:
        return {
            "ok": False,
            "product_tier": tier,
            "lexicon_dir": str(root),
            "catalog": str(Path(catalog_path).resolve()) if catalog_path else str(root / "catalog.json"),
            "selected_ids": [],
            "files": [],
            "totals": {"catalog": 0, "selected": 0, "rules": 0, "active_rules": 0, "hotwords": 0},
            "errors": [f"catalog is invalid: {exc}"],
            "warnings": [],
        }

    eligible = {item.id: item for item in catalog if _tier_allows(tier, item.min_tier)}
    if enabled_ids is None:
        selected_ids = [item.id for item in catalog if item.default_enabled and item.id in eligible]
    else:
        selected_ids = _dedupe([item.strip() for item in enabled_ids if item.strip()])
    unavailable = [item for item in selected_ids if item not in eligible]
    if unavailable:
        errors.append("enabled lexicon id is unavailable for this product tier: " + ", ".join(unavailable))
    selected = set(selected_ids) - set(unavailable)
    seen_sources: dict[str, str] = {}
    total_rules = 0
    total_active_rules = 0
    total_hotwords = 0

    for item in catalog:
        path = (root / item.filename).resolve()
        file_report: dict[str, object] = {
            "id": item.id,
            "title": item.title,
            "filename": item.filename,
            "selected": item.id in selected,
            "default_enabled": item.default_enabled,
            "min_tier": item.min_tier,
            "rules": 0,
            "active_rules": 0,
            "hotwords": 0,
        }
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(f"{item.id}: filename escapes lexicon directory: {item.filename}")
            files.append(file_report)
            continue
        if not path.is_file():
            errors.append(f"{item.id}: lexicon file is missing: {path}")
            files.append(file_report)
            continue
        try:
            lexicon = Lexicon.from_file(path)
        except Exception as exc:
            errors.append(f"{item.id}: lexicon file is invalid: {exc}")
            files.append(file_report)
            continue
        active_rules = lexicon.iter_rules(product_tier=tier)
        hotwords = lexicon.canonical_terms(product_tier=tier)
        file_report["rules"] = len(lexicon.rules)
        file_report["active_rules"] = len(active_rules)
        file_report["hotwords"] = len(hotwords)
        files.append(file_report)
        if item.id in selected:
            total_rules += len(lexicon.rules)
            total_active_rules += len(active_rules)
            total_hotwords += len(hotwords)
            for rule in active_rules:
                existing = seen_sources.get(rule.source)
                if existing is not None and existing != rule.target:
                    warnings.append(
                        f"{item.id}: source {rule.source!r} has multiple targets: {existing!r}, {rule.target!r}"
                    )
                else:
                    seen_sources[rule.source] = rule.target

    return {
        "ok": not errors,
        "product_tier": tier,
        "lexicon_dir": str(root),
        "catalog": str(Path(catalog_path).resolve()) if catalog_path else str(root / "catalog.json"),
        "selected_ids": selected_ids,
        "files": files,
        "totals": {
            "catalog": len(catalog),
            "selected": len(selected),
            "rules": total_rules,
            "active_rules": total_active_rules,
            "hotwords": total_hotwords,
        },
        "errors": errors,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a bundled lexicon catalog before packaging.")
    parser.add_argument("--lexicon-dir", default=str(ROOT / "assets" / "lexicons"))
    parser.add_argument("--catalog", default="")
    parser.add_argument("--product-tier", default="enterprise", choices=tuple(TIER_RANK))
    parser.add_argument("--enabled", default="", help="Comma or semicolon separated lexicon ids to validate as selected.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON report.")
    parser.add_argument("--strict-warnings", action="store_true", help="Return exit code 2 when warnings are present.")
    args = parser.parse_args(argv)

    report = validate_catalog(
        args.lexicon_dir,
        catalog_path=args.catalog or None,
        product_tier=args.product_tier,
        enabled_ids=_split_enabled(args.enabled) if args.enabled else None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text_report(report)
    if not report["ok"]:
        return 1
    if args.strict_warnings and report["warnings"]:
        return 2
    return 0


def _print_text_report(report: dict[str, object]) -> None:
    status = "OK" if report["ok"] else "FAILED"
    totals = report["totals"]
    assert isinstance(totals, dict)
    print(f"Lexicon catalog {status}")
    print(f"  dir: {report['lexicon_dir']}")
    print(f"  catalog: {report['catalog']}")
    print(f"  product_tier: {report['product_tier']}")
    print(f"  selected: {', '.join(report['selected_ids']) or '(none)'}")
    print(
        "  totals: "
        f"{totals['selected']} selected / {totals['catalog']} catalog, "
        f"{totals['active_rules']} active rules, {totals['hotwords']} hotwords"
    )
    for item in report["files"]:
        assert isinstance(item, dict)
        marker = "*" if item["selected"] else "-"
        print(
            f"  {marker} {item['id']} -> {item['filename']} "
            f"({item['active_rules']} active / {item['rules']} rules, {item['hotwords']} hotwords)"
        )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    for error in report["errors"]:
        print(f"ERROR: {error}")


def _split_enabled(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace(";", ",").split(",") if item.strip())


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def _normalize_tier(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in TIER_RANK else "enterprise"


def _tier_allows(product_tier: str, minimum_tier: str) -> bool:
    return TIER_RANK.get(product_tier, TIER_RANK["standard"]) >= TIER_RANK.get(
        str(minimum_tier).strip().lower(),
        TIER_RANK["standard"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
