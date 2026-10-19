from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_lexicon_catalog.py"
_SPEC = importlib.util.spec_from_file_location("validate_lexicon_catalog", _SCRIPT_PATH)
assert _SPEC is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)
validate_catalog = _MODULE.validate_catalog


def test_validate_catalog_accepts_custom_manifest(tmp_path: Path) -> None:
    root = tmp_path / "lexicons"
    root.mkdir()
    (root / "client.csv").write_text("source,target\n市府,市政府\n", encoding="utf-8")
    (root / "catalog.json").write_text(
        """
        {
          "schema_version": 1,
          "lexicons": [
            {
              "id": "client_government",
              "title": "客户政务词库",
              "filename": "client.csv",
              "default_enabled": true,
              "min_tier": "basic"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    report = validate_catalog(root, product_tier="enterprise")

    assert report["ok"] is True
    assert report["selected_ids"] == ["client_government"]
    assert report["totals"]["selected"] == 1
    assert report["totals"]["active_rules"] == 1
    assert report["totals"]["hotwords"] == 1


def test_validate_catalog_reports_missing_lexicon_file(tmp_path: Path) -> None:
    root = tmp_path / "lexicons"
    root.mkdir()
    (root / "catalog.json").write_text(
        """
        {
          "schema_version": 1,
          "lexicons": [
            {
              "id": "client_government",
              "filename": "missing.csv",
              "default_enabled": true,
              "min_tier": "basic"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    report = validate_catalog(root)

    assert report["ok"] is False
    assert "missing.csv" in report["errors"][0]


def test_validate_catalog_rejects_enabled_lexicon_outside_tier(tmp_path: Path) -> None:
    root = tmp_path / "lexicons"
    root.mkdir()
    (root / "enterprise.csv").write_text("source,target\n专班,工作专班\n", encoding="utf-8")
    (root / "catalog.json").write_text(
        """
        {
          "schema_version": 1,
          "lexicons": [
            {
              "id": "enterprise_only",
              "filename": "enterprise.csv",
              "default_enabled": false,
              "min_tier": "enterprise"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    report = validate_catalog(root, product_tier="basic", enabled_ids=("enterprise_only",))

    assert report["ok"] is False
    assert "unavailable" in report["errors"][0]
