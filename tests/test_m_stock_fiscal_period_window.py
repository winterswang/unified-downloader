"""
Unit tests for 财年制公司 6-K 期别窗口 (#66, 2026-08-30).

Regression: BABA 是财年制公司 (财年 3 月底结束), "2027Q1" = 财年 2027 的
Q1 = 日历 2026年4-6月季度 (June Quarter 2026, 8/20 发布). 之前
_report_period_target_end 一律按自然年映射 → 目标期算成 2027-03-31 →
窗口 2027-04-14~06-24 错位一年 → 8/20 真财报完全落在窗口外, 永远选不到.

修复:
1. _FISCAL_YEAR_END_MONTH 财年末月份映射 (BABA=3 / NVDA=1 / AAPL=9),
   fiscal_end_month 给定时按财年解析目标期; =12 或未列公司退化回自然年.
2. 搜索层 prev_year_floor: 财年制公司标签年 > 发布年 (BABA "2027Q1"
   发布于 2026-08), 搜索要放开上一年, 下界收到目标期结束日.

这些测试只测选择/窗口逻辑, 不下载真实文件.
"""
import datetime
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import (  # noqa: E402
    MStockAdapter,
    _quarter_window,
    _quarter_window_missed,
    _report_period_target_end,
    fiscal_year_end_month,
)


def _mk_filing(accession: str, size: int, filed_at: str, exhibits=None):
    """构造一条 filing dict（模拟 _search_edgar 返回）。"""
    return {
        "ticker": "BABA",
        "formType": "6-K",
        "filedAt": filed_at,
        "accessionNo": accession,
        "cik": "1577552",
        "companyName": accession,
        "linkToTxt": f"https://example.com/{accession}/index.html",
        "linkToHtml": f"https://example.com/{accession}/index.html",
        "source": "edgar",
        "size": size,
        "_exhibits": exhibits or [],
    }


# BABA 真实 6-K 场景 (2026-08-30 issue #66): 8/20 "June Quarter 2026"
# 真财报 463KB 在列, 但按自然年窗口 (2027-04~06) 永远选不到
_BABA_2026_FILINGS = [
    _mk_filing("BABA-Q1FY27", 463000, "2026-08-20",
               exhibits=[{"url": "https://x/baba_ex99.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm26_ex99-1.htm"}]),
    _mk_filing("BABA-PR-JUL", 17272, "2026-07-06",
               exhibits=[{"url": "https://x/pr.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm25_ex99-1.htm"}]),
    _mk_filing("BABA-Q4FY26", 215461, "2026-05-14",
               exhibits=[{"url": "https://x/q4.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm24_ex99-1.htm"}]),
]


class TestFiscalTargetEnd:
    """财年制 report_period → 目标报告期结束日"""

    def test_baba_fy_march(self):
        # BABA 财年 2027 止 2027-03-31; Q1 止 2026-06-30 (June Quarter 2026)
        assert _report_period_target_end("2027Q1", 3) == datetime.date(2026, 6, 30)
        assert _report_period_target_end("2027Q2", 3) == datetime.date(2026, 9, 30)
        assert _report_period_target_end("2027Q3", 3) == datetime.date(2026, 12, 31)
        assert _report_period_target_end("2027Q4", 3) == datetime.date(2027, 3, 31)
        assert _report_period_target_end("2027FY", 3) == datetime.date(2027, 3, 31)
        assert _report_period_target_end("2027H1", 3) == datetime.date(2026, 9, 30)
        assert _report_period_target_end("2027H2", 3) == datetime.date(2027, 3, 31)

    def test_nvda_fy_january(self):
        # NVDA 财年止 1 月底: FY2026 Q4 → 2026-01; FY2027 Q1 → 2026-04
        assert _report_period_target_end("2026Q4", 1) == datetime.date(2026, 1, 31)
        assert _report_period_target_end("2027Q1", 1) == datetime.date(2026, 4, 30)

    def test_aapl_fy_september(self):
        # AAPL 财年止 9 月底: FY2026 Q4 → 2026-09; FY2026 Q1 → 2025-12
        assert _report_period_target_end("2026Q4", 9) == datetime.date(2026, 9, 30)
        assert _report_period_target_end("2026Q1", 9) == datetime.date(2025, 12, 31)

    def test_no_fiscal_month_keeps_calendar_behavior(self):
        """未列公司 / 不传 fiscal_end_month → 自然年映射 (旧行为不变)."""
        assert _report_period_target_end("2027Q1") == datetime.date(2027, 3, 31)
        assert _report_period_target_end("2027Q1", None) == datetime.date(2027, 3, 31)
        assert _report_period_target_end("2026Q2", 12) == datetime.date(2026, 6, 30)

    def test_invalid_period_returns_none(self):
        assert _report_period_target_end("2027H3", 3) is None
        assert _report_period_target_end("garbage", 3) is None
        assert _report_period_target_end(None, 3) is None

    def test_fiscal_year_end_month_lookup(self):
        assert fiscal_year_end_month("BABA") == 3
        assert fiscal_year_end_month("baba") == 3
        assert fiscal_year_end_month("NVDA") == 1
        assert fiscal_year_end_month("AAPL") == 9
        assert fiscal_year_end_month("PDD") is None
        assert fiscal_year_end_month("") is None
        assert fiscal_year_end_month(None) is None


class TestFiscalQuarterWindow:
    """财年制窗口: BABA 2027Q1 → 2026-07-14 ~ 2026-09-23, 8/20 真财报在列"""

    def test_baba_2027q1_window_contains_aug20_filing(self):
        window = _quarter_window("2027Q1", 3)
        assert window is not None
        lo, hi = window
        assert lo == datetime.date(2026, 7, 14)
        assert hi == datetime.date(2026, 9, 23)
        assert lo <= datetime.date(2026, 8, 20) <= hi

    def test_window_missed_detects_before_fix_would_miss(self):
        """修复前 (自然年) 窗口内无 filing → missed=True; 修复后命中."""
        assert _quarter_window_missed(
            _BABA_2026_FILINGS, "2027Q1") is True  # 自然年窗口 (错位)
        assert _quarter_window_missed(
            _BABA_2026_FILINGS, "2027Q1", 3) is False  # 财年窗口命中 8/20

    def test_pick_earnings_6k_baba_fiscal(self, ):
        adapter = MStockAdapter(MagicMock(), [])
        picked = adapter._pick_earnings_6k(
            _BABA_2026_FILINGS, report_period="2027Q1", fiscal_end_month=3)
        assert picked is not None
        assert picked["accessionNo"] == "BABA-Q1FY27"

    def test_pick_earnings_6k_without_fiscal_returns_none(self):
        """不传 fiscal_end_month → 自然年窗口内无 filing → None (复现 #66)."""
        adapter = MStockAdapter(MagicMock(), [])
        picked = adapter._pick_earnings_6k(
            _BABA_2026_FILINGS, report_period="2027Q1")
        assert picked is None


class TestDownloadFormFiscalThreading:
    """_download_form 把财年映射 / prev_year_floor 传给搜索与窗口层"""

    def _run_download_form(self, adapter, code, year, report_period):
        captured = {}

        def fake_search(ticker, form_type, yr, size=10,
                        include_next_year=False, prev_year_floor=None):
            captured["prev_year_floor"] = prev_year_floor
            return _BABA_2026_FILINGS

        ok_result = MagicMock()
        ok_result.success = True
        with patch.object(adapter, "_search_filings", side_effect=fake_search), \
             patch.object(adapter, "_download_filing", return_value=ok_result):
            result = adapter._download_form(
                code, "6-K", year, None, None, report_period=report_period
            )
        return result, captured

    def test_baba_prev_year_floor_set(self):
        """BABA 2027Q1 (year=2027): 目标期 2026-06-30 在上一年 → prev_year_floor."""
        adapter = MStockAdapter(MagicMock(), [])
        result, captured = self._run_download_form(
            adapter, "BABA", 2027, "2027Q1")
        assert result.success
        assert captured["prev_year_floor"] == datetime.date(2026, 6, 30)

    def test_calendar_filer_no_prev_year_floor(self):
        """PDD (自然年制): 不放行上一年搜索."""
        adapter = MStockAdapter(MagicMock(), [])
        result, captured = self._run_download_form(
            adapter, "PDD", 2026, "2026Q2")
        assert result.success
        assert captured["prev_year_floor"] is None

    def test_baba_q4_same_year_no_prev_year_floor(self):
        """BABA 2027Q4 目标期 2027-03-31 与标签同年 → 无需放开上一年."""
        adapter = MStockAdapter(MagicMock(), [])
        captured = {}

        def fake_search(ticker, form_type, yr, size=10,
                        include_next_year=False, prev_year_floor=None):
            captured["prev_year_floor"] = prev_year_floor
            # 财年 Q4 窗口 2027-04-14~06-24 内的 filing
            return [_mk_filing("BABA-Q4FY27", 300000, "2027-05-10")]

        ok_result = MagicMock()
        ok_result.success = True
        with patch.object(adapter, "_search_filings", side_effect=fake_search), \
             patch.object(adapter, "_download_filing", return_value=ok_result):
            result = adapter._download_form(
                "BABA", "6-K", 2027, None, None, report_period="2027Q4")
        assert result.success
        assert captured["prev_year_floor"] is None

    def test_baba_fiscal_window_picks_real_earnings(self):
        """端到端: BABA 2027Q1 搜索结果含 8/20 真财报 → 选中它下载."""
        adapter = MStockAdapter(MagicMock(), [])
        picked = {}

        def fake_search(ticker, form_type, yr, size=10,
                        include_next_year=False, prev_year_floor=None):
            return _BABA_2026_FILINGS

        def fake_download(filing, ticker, form_type, year, on_progress, checkpoint):
            picked["accessionNo"] = filing["accessionNo"]
            ok = MagicMock()
            ok.success = True
            return ok

        with patch.object(adapter, "_search_filings", side_effect=fake_search), \
             patch.object(adapter, "_download_filing", side_effect=fake_download):
            adapter._download_form("BABA", "6-K", 2027, None, None,
                                   report_period="2027Q1")
        assert picked["accessionNo"] == "BABA-Q1FY27"
