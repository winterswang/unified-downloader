"""Regression tests for US quarterly routing in bulk_download.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bulk_download.py"


def load_bulk_module():
    spec = importlib.util.spec_from_file_location("bulk_download", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_search_docs_parses_us_iso_dates(monkeypatch):
    bulk = load_bulk_module()

    def fake_run(cmd, timeout=120):
        return 0, """搜索 AAPL 所有年份 10q...

找到 3 个文档:

1. 10-Q - 2026-05-01
   Accession: x
2. 10-Q - 2025-08-01
   Accession: y
3. 10-Q - 2025-05-02
   Accession: z
""", ""

    monkeypatch.setattr(bulk, "run", fake_run)

    docs = bulk.search_docs("AAPL", "US", "10q")

    assert [year for year, _ in docs] == ["2026", "2025", "2025"]


def test_search_docs_keeps_chinese_year_format(monkeypatch):
    bulk = load_bulk_module()

    def fake_run(cmd, timeout=120):
        return 0, "1. 2024 年年度报告\n2. 2023 年年度报告", ""

    monkeypatch.setattr(bulk, "run", fake_run)

    assert [year for year, _ in bulk.search_docs("0700", "HK", "annual_report")] == ["2024", "2023"]


def test_resolve_target_matches_us_suffix_keys():
    bulk = load_bulk_module()
    adr_map = {
        "use_hk_source": {"MNSO.US": {"hk_code": "9896", "name": "名创优品"}},
        "use_sec_20f_only": {"NVO.US": "20-F only"},
    }

    assert bulk.resolve_target("MNSO", "US", adr_map) == ("9896", "HK", True, False)
    assert bulk.resolve_target("MNSO.US", "US", adr_map) == ("9896", "HK", True, False)
    assert bulk.resolve_target("NVO", "US", adr_map) == ("NVO", "US", False, True)


def test_us_quarterly_map_attempts_10q_and_6k():
    bulk = load_bulk_module()

    assert bulk.QUARTERLY_MAP["US"] == [("10q", "Q1-Q3"), ("6k", "Q1-Q3")]
    assert bulk.QUARTERLY_MAP["US_20F"] == [("6k", "Q1-Q3")]


def test_us_20f_quarterly_search_uses_us_market():
    bulk = load_bulk_module()

    assert bulk.quarterly_search_market(False, True, "US_20F") == "US"
    assert bulk.quarterly_search_market(False, False, "US") == "US"
    assert bulk.quarterly_search_market(True, False, "HK") == "HK"


def test_quarterly_unavailable_does_not_overwrite_failure():
    bulk = load_bulk_module()
    state = {
        "quarterly": {
            "2025": {
                "Q1-Q3": {"status": "ima_failed", "form_type": "10q", "file": "downloads/m/AAP/AAPL_2025_10Q.pdf"}
            }
        }
    }

    assert bulk.should_skip_quarterly_candidate(state, "2025", "Q1-Q3") is False
    assert bulk.should_mark_quarterly_unavailable(state, "2025", "Q1-Q3", "6k") is False
    assert bulk.should_mark_quarterly_unavailable(state, "2025", "Q1-Q3", "10q") is True


def test_quarterly_skipped_can_be_retried_by_fallback_form():
    bulk = load_bulk_module()
    state = {
        "quarterly": {
            "2025": {"Q1-Q3": {"status": "skipped", "reason": "not available"}}
        }
    }

    assert bulk.should_skip_quarterly_candidate(state, "2025", "Q1-Q3") is False
    assert bulk.should_mark_quarterly_unavailable(state, "2025", "Q1-Q3", "6k") is True
