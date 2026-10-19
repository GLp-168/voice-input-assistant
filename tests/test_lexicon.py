from __future__ import annotations

from pathlib import Path

import pytest

from voice_input.lexicon import Lexicon, LexiconRule, LexiconRuleType
from voice_input.resources import bundled_lexicon_catalog, bundled_lexicon_paths, lexicon_catalog_from_dir


def test_lexicon_applies_basic_replacement_and_hotword_correction() -> None:
    lexicon = Lexicon(
        rules=(
            LexiconRule("政屋", "政务", LexiconRuleType.HOTWORD, weight=10),
            LexiconRule("张三丰", "张三", LexiconRuleType.REPLACE),
        )
    )

    assert lexicon.normalize("张三丰办理政屋服务") == "张三办理政务服务"


def test_lexicon_prefers_more_specific_or_weighted_rules() -> None:
    lexicon = Lexicon(
        rules=(
            LexiconRule("政务", "政务服务", weight=1),
            LexiconRule("政务服务", "一体化政务服务平台", weight=2),
        )
    )

    assert lexicon.normalize("请打开政务服务") == "请打开一体化政务服务平台"


def test_lexicon_filters_by_product_tier_and_rule_type() -> None:
    lexicon = Lexicon(
        rules=(
            LexiconRule("备案", "报备", product_tiers=frozenset({"basic"})),
            LexiconRule("备案", "事项备案", product_tiers=frozenset({"enterprise"})),
            LexiconRule("港澳台", "港澳台地区", LexiconRuleType.HOTWORD),
        )
    )

    assert lexicon.normalize("备案 港澳台", product_tier="basic") == "报备 港澳台地区"
    assert lexicon.normalize("备案 港澳台", product_tier="enterprise") == "事项备案 港澳台地区"
    assert lexicon.normalize("备案 港澳台", enabled_rule_types=[LexiconRuleType.HOTWORD]) == "备案 港澳台地区"
    assert lexicon.normalize("备案 港澳台", product_tier="unknown") == "备案 港澳台地区"


def test_lexicon_supports_case_insensitive_rules() -> None:
    lexicon = Lexicon(rules=(LexiconRule("OpenAI", "OpenAI API", case_sensitive=False),))

    assert lexicon.normalize("openai 和 OPENAI") == "OpenAI API 和 OpenAI API"


def test_lexicon_loads_from_json() -> None:
    lexicon = Lexicon.from_json(
        """
        [
          {
            "source": "营商",
            "target": "营商环境",
            "rule_type": "hotword",
            "product_tiers": ["enterprise"],
            "weight": 3,
            "metadata": {"domain": "gov"}
          }
        ]
        """
    )

    assert lexicon.normalize("优化营商", product_tier="enterprise") == "优化营商环境"
    assert lexicon.normalize("优化营商", product_tier="basic") == "优化营商"
    assert lexicon.rules[0].metadata == {"domain": "gov"}
    assert lexicon.rules[0].domain == "gov"


def test_lexicon_loads_rule_metadata_fields() -> None:
    lexicon = Lexicon.from_json(
        """
        [
          {
            "source": "专向债",
            "target": "专项债",
            "category": "bond",
            "domain": "finance",
            "usage": "correction",
            "confidence": 0.91,
            "examples": ["讨论专项债资金使用"]
          }
        ]
        """
    )

    rule = lexicon.rules[0]
    assert rule.category == "bond"
    assert rule.domain == "finance"
    assert rule.usage == "correction"
    assert rule.confidence == 0.91
    assert rule.examples == ("讨论专项债资金使用",)


def test_lexicon_canonical_terms_use_targets_not_error_sources() -> None:
    lexicon = Lexicon(
        rules=(
            LexiconRule("专向债", "专项债", LexiconRuleType.REPLACE, weight=100),
            LexiconRule("增值税", "增值税", LexiconRuleType.HOTWORD, weight=90),
        )
    )

    assert lexicon.canonical_terms() == ("专项债", "增值税")


def test_lexicon_json_aliases_expand_to_correction_rules() -> None:
    lexicon = Lexicon.from_json(
        """
        [
          {
            "source": "\u6570\u5b57\u653f\u5e9c",
            "target": "\u6570\u5b57\u653f\u5e9c",
            "aliases": ["\u6570\u5b57\u6b63\u5e9c", "\u6570\u5b57\u653f\u52a1"],
            "weight": 20
          }
        ]
        """
    )

    assert lexicon.normalize("\u5efa\u8bbe\u6570\u5b57\u6b63\u5e9c") == "\u5efa\u8bbe\u6570\u5b57\u653f\u5e9c"
    assert lexicon.normalize("\u63a8\u8fdb\u6570\u5b57\u653f\u52a1") == "\u63a8\u8fdb\u6570\u5b57\u653f\u5e9c"


def test_lexicon_loads_plain_text_pairs_from_file(tmp_path: Path) -> None:
    path = tmp_path / "lexicon.txt"
    path.write_text("# comment\n政屋=>政务\n 错词 => 正词 \n", encoding="utf-8")

    lexicon = Lexicon.from_file(path, product_tiers=["basic"])

    assert lexicon.normalize("政屋和错词", product_tier="basic") == "政务和正词"
    assert lexicon.normalize("政屋和错词", product_tier="enterprise") == "政屋和错词"


def test_lexicon_loads_csv_pairs_from_file(tmp_path: Path) -> None:
    path = tmp_path / "lexicon.csv"
    path.write_text("source,target\n政屋,政务\n证照分里,证照分离\n", encoding="utf-8")

    lexicon = Lexicon.from_file(path, weight=5)

    assert lexicon.normalize("政屋和证照分里") == "政务和证照分离"
    assert lexicon.iter_rules()[0].weight == 5


def test_lexicon_loads_structured_csv_metadata_and_aliases(tmp_path: Path) -> None:
    path = tmp_path / "lexicon.csv"
    path.write_text(
        (
            "source,target,rule_type,product_tiers,weight,enabled,case_sensitive,category,domain,usage,"
            "confidence,examples,aliases,owner\n"
            "专向债,专项债,hotword,basic;standard,11,是,否,bond,finance,boost,0.83,"
            "专项债资金;专项债项目,专相债;专项债券,finance-team\n"
        ),
        encoding="utf-8",
    )

    lexicon = Lexicon.from_file(path)
    rule = lexicon.rules[0]

    assert len(lexicon.rules) == 3
    assert rule.rule_type == LexiconRuleType.HOTWORD
    assert rule.product_tiers == frozenset({"basic", "standard"})
    assert rule.weight == 11
    assert rule.case_sensitive is False
    assert rule.category == "bond"
    assert rule.domain == "finance"
    assert rule.usage == "boost"
    assert rule.confidence == 0.83
    assert rule.examples == ("专项债资金", "专项债项目")
    assert rule.metadata == {"owner": "finance-team"}
    assert lexicon.normalize("专向债和专相债", product_tier="basic") == "专项债和专项债"
    assert lexicon.canonical_terms(enabled_rule_types=[LexiconRuleType.HOTWORD]) == ("专项债",)


def test_lexicon_rejects_invalid_plain_text_line() -> None:
    with pytest.raises(ValueError, match="lexicon line"):
        Lexicon.from_lines(["missing separator"])


def test_lexicon_rejects_invalid_csv_payload() -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        Lexicon.from_csv("source,wrong\n政屋,政务\n")


def test_bundled_lexicon_catalog_exposes_product_metadata() -> None:
    catalog = bundled_lexicon_catalog()

    assert [item.id for item in catalog] == [
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    ]
    assert catalog[0].title
    assert catalog[1].description
    assert catalog[0].default_enabled is True
    assert catalog[1].min_tier == "basic"
    assert "project-management" in catalog[2].tags


def test_bundled_lexicon_catalog_can_be_extended_by_manifest(tmp_path: Path) -> None:
    root = tmp_path / "lexicons"
    city_dir = root / "city"
    city_dir.mkdir(parents=True)
    (city_dir / "mayor_office.csv").write_text("source,target\n市府,市政府\n", encoding="utf-8")
    (root / "catalog.json").write_text(
        """
        {
          "schema_version": 1,
          "lexicons": [
            {
              "id": "city_government",
              "title": "市政府专属",
              "description": "客户专属政务词库",
              "filename": "city/mayor_office.csv",
              "default_enabled": false,
              "min_tier": "enterprise",
              "tags": ["custom", "government"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    catalog = lexicon_catalog_from_dir(root, strict=True)

    assert [item.id for item in catalog] == ["city_government"]
    assert catalog[0].filename == "city/mayor_office.csv"
    assert catalog[0].default_enabled is False
    assert lexicon_catalog_from_dir(root, product_tier="basic", strict=True) == ()
    assert [item.id for item in lexicon_catalog_from_dir(root, product_tier="enterprise", strict=True)] == [
        "city_government"
    ]


def test_bundled_lexicon_catalog_filters_by_product_tier() -> None:
    assert [item.id for item in bundled_lexicon_catalog(product_tier="basic")] == [
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    ]
    assert [item.id for item in bundled_lexicon_catalog(product_tier="standard")] == [
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    ]
    assert [item.id for item in bundled_lexicon_catalog(product_tier="enterprise")] == [
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    ]


def test_bundled_lexicon_paths_respects_product_tier_and_selection(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "assets" / "lexicons"
    root.mkdir(parents=True)
    (root / "government_digital.json").write_text("[]", encoding="utf-8")
    (root / "official_document.json").write_text("[]", encoding="utf-8")
    (root / "office_meeting.json").write_text("[]", encoding="utf-8")
    (root / "finance_budget.json").write_text("[]", encoding="utf-8")
    (root / "project_engineering.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr("voice_input.resources.bundled_lexicon_dir", lambda: root)

    paths = bundled_lexicon_paths(
        ["government_digital", "official_document", "office_meeting", "finance_budget", "project_engineering"],
        product_tier="basic",
    )

    assert [path.name for path in paths] == [
        "government_digital.json",
        "official_document.json",
        "office_meeting.json",
        "finance_budget.json",
        "project_engineering.json",
    ]
