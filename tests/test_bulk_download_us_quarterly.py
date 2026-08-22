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


def test_set_doc_status_persists_quarterly_form_type_for_fallback_protection():
    bulk = load_bulk_module()
    state = {"code": "AAPL", "annual": {}, "quarterly": {}}

    bulk.set_doc_status(
        state,
        "quarterly",
        "2025",
        "Q1-Q3",
        "ima_failed",
        filepath="downloads/m/AAP/AAPL_2025_10Q.pdf",
        fsize=1234567,
        form_type="10q",
    )

    entry = state["quarterly"]["2025"]["Q1-Q3"]
    assert entry["form_type"] == "10q"
    assert entry["ima"] == "failed"
    assert bulk.should_mark_quarterly_unavailable(state, "2025", "Q1-Q3", "6k") is False
    assert bulk.should_mark_quarterly_unavailable(state, "2025", "Q1-Q3", "10q") is True


def test_download_one_us_uses_html_not_pdf(monkeypatch, tmp_path):
    """W38-#50e: 美股下载不再 --pdf (commit 38b9f16 7-12 改: IMA 支持 HTML 上传).

    旧版期望 '--pdf' in calls, 实际 source code 早就不 append. 跟 W36 PR #42 同样
    test/source 不一致, 改 test 删 '--pdf' 期望, 加 'download' 'single' 验证基本 cmd 存在.
    """
    bulk = load_bulk_module()
    bulk.log = __import__("logging").getLogger("test-bulk")
    output = tmp_path / "downloads/m/AAP/AAPL_2025_10Q.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
    rel = output.relative_to(tmp_path)
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)
    calls = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        return 0, f"✓ 下载成功\n  文件: {rel}", ""

    monkeypatch.setattr(bulk, "run", fake_run)

    assert bulk.download_one("AAPL", "US", "2025", "10q") == (output, None)
    # W38: IMA 现在直接支持 HTML 上传, 美股不再 --pdf (commit 38b9f16 7-12 改)
    assert "--pdf" not in calls[0], "W38: 美股不再 --pdf (commit 38b9f16 7-12 改: IMA HTML 支持)"
    assert "--no-cache" not in calls[0]
    assert "download" in calls[0] and "single" in calls[0]


def test_ima_sync_script_path_can_be_overridden(monkeypatch):
    monkeypatch.setenv("IMA_SYNC_SCRIPT_PATH", "/tmp/custom-sync-to-ima.sh")
    bulk = load_bulk_module()

    assert str(bulk.IMA_SYNC_SCRIPT) == "/tmp/custom-sync-to-ima.sh"
