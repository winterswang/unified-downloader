"""
Regression tests for 6-K 季度窗口三项修复 (W40-#50 P2).

1. H1/H2 半年报之前按 Q1/Q2 映射 month_end (H1→3-31, H2→6-30), 窗口错
   一个季度 — RACE 等 7 月底发布的中报完全落在窗口外
2. Q4/FY 财报发布窗口在次年 1-3 月, 搜索层按日历年过滤提前丢弃 →
   窗口逻辑对 Q4 永远空转 (include_next_year)
3. report_period 在 10q (FPI→6-K) 路径被 **kwargs 静默吞掉

这些测试只测选择/传参逻辑, 不下载真实文件 (真实 SEC 验证见 PR 描述).
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import (  # noqa: E402
    MStockAdapter,
    _report_period_target_end,
)


def _mk_filing(accession: str, size: int, filed_at: str, exhibits=None):
    return {
        "ticker": "TEST",
        "formType": "6-K",
        "filedAt": filed_at,
        "accessionNo": accession,
        "cik": "1",
        "companyName": accession,
        "linkToTxt": f"https://example.com/{accession}",
        "linkToHtml": f"https://example.com/{accession}",
        "source": "edgar",
        "size": size,
        "_exhibits": exhibits or [],
    }


@pytest.fixture
def adapter():
    return MStockAdapter(MagicMock(), [])


# ═══ 1. H1/H2 映射 ═══


class TestReportPeriodTargetEnd:
    def test_quarters(self):
        assert _report_period_target_end("2026Q1") == date(2026, 3, 31)
        assert _report_period_target_end("2026Q2") == date(2026, 6, 30)
        assert _report_period_target_end("2026Q3") == date(2026, 9, 30)
        assert _report_period_target_end("2026Q4") == date(2026, 12, 31)

    def test_half_year_fixed(self):
        # 回归核心: 之前 H1→3-31 / H2→6-30 (按 Q1/Q2 映射), 错一个季度
        assert _report_period_target_end("2026H1") == date(2026, 6, 30)
        assert _report_period_target_end("2026H2") == date(2026, 12, 31)

    def test_invalid_half_year(self):
        assert _report_period_target_end("2026H3") is None
        assert _report_period_target_end("2026H4") is None

    def test_fy_and_garbage(self):
        assert _report_period_target_end("2026FY") == date(2026, 12, 31)
        assert _report_period_target_end("2026") == date(2026, 12, 31)
        assert _report_period_target_end("nonsense") is None
        assert _report_period_target_end(None) is None

    def test_lowercase_input(self):
        assert _report_period_target_end("2026h1") == date(2026, 6, 30)


class TestHalfYearWindow:
    def test_h1_window_catches_july_interim(self, adapter):
        """RACE 式 H1 中报 7-30 发布 — 修复前窗口 4-25~6-24 落空"""
        filings = [
            _mk_filing("PR-JUN", 50000, "2026-06-20"),     # 窗口外噪音
            _mk_filing("INTERIM-H1", 209000, "2026-07-30",
                       exhibits=[{"url": "https://x/a.htm",
                                  "description": "Interim report EXHIBIT 99.1",
                                  "document": "tm1_ex99-1.htm"}]),
        ]

        picked = adapter._pick_earnings_6k(filings, report_period="2026H1")

        assert picked is not None
        assert picked["accessionNo"] == "INTERIM-H1"


# ═══ 2. Q4 跨年搜索 ═══


class TestQ4CrossYear:
    def test_q4_window_picks_next_january_filing(self, adapter):
        """2025Q4 财报 2026-02 发布 — 修复前搜索层把它过滤掉"""
        filings = [
            _mk_filing("Q4-EARNINGS", 400000, "2026-02-10",
                       exhibits=[{"url": "https://x/q4.htm",
                                  "description": "Fourth quarter results",
                                  "document": "tm2_ex99-1.htm"}]),
            _mk_filing("OLD-PR", 30000, "2025-03-15"),
        ]

        picked = adapter._pick_earnings_6k(filings, report_period="2025Q4")

        assert picked is not None
        assert picked["accessionNo"] == "Q4-EARNINGS"

    def test_download_form_passes_include_next_year_for_q4(self, adapter):
        """_download_form 对 Q4/FY/H2 的 6-K 搜索要放行次年 filing"""
        captured = {}

        def fake_search(ticker, form_type, year, size=10, include_next_year=False):
            captured["include_next_year"] = include_next_year
            return [_mk_filing("Q4", 400000, "2026-02-10")]

        ok_result = MagicMock()
        ok_result.success = True
        with patch.object(adapter, "_search_filings", side_effect=fake_search), \
             patch.object(adapter, "_download_filing", return_value=ok_result):
            adapter._download_form(
                "TEST", "6-K", 2025, None, None, report_period="2025Q4"
            )

        assert captured["include_next_year"] is True

    def test_download_form_no_flag_for_q2(self, adapter):
        captured = {}

        def fake_search(ticker, form_type, year, size=10, include_next_year=False):
            captured["include_next_year"] = include_next_year
            return [_mk_filing("Q2", 200000, "2025-08-13")]

        ok_result = MagicMock()
        ok_result.success = True
        with patch.object(adapter, "_search_filings", side_effect=fake_search), \
             patch.object(adapter, "_download_filing", return_value=ok_result):
            adapter._download_form(
                "TEST", "6-K", 2025, None, None, report_period="2025Q2"
            )

        assert captured["include_next_year"] is False

    def test_download_form_no_flag_without_report_period(self, adapter):
        captured = {}

        def fake_search(ticker, form_type, year, size=10, include_next_year=False):
            captured["include_next_year"] = include_next_year
            return [_mk_filing("X", 1000, "2025-05-01")]

        ok_result = MagicMock()
        ok_result.success = True
        with patch.object(adapter, "_search_filings", side_effect=fake_search), \
             patch.object(adapter, "_download_filing", return_value=ok_result):
            adapter._download_form("TEST", "6-K", 2025, None, None)

        assert captured["include_next_year"] is False

    def test_sec_api_query_spans_next_year(self, adapter):
        adapter._api_key = "test-key"
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"filings": []}

        def fake_post(url, json=None, headers=None):
            captured["payload"] = json
            return FakeResp()

        with patch.object(adapter._http_client, "post", side_effect=fake_post), \
             patch.object(adapter, "_get_datasource", return_value=None):
            adapter._search_sec_api(
                "TEST", "6-K", 2025, size=10, include_next_year=True
            )
            query = captured["payload"]["query"]["query_string"]["query"]
            # 上界收到次年 6-30: filings 新→旧返回 + size 截断, 上界太宽
            # 会让次年后半年淹没 Q4 发布窗口 (TSM 2024Q4 真实数据验证)
            assert "filedAt:[2025-01-01 TO 2026-06-30]" in query

            adapter._search_sec_api("TEST", "6-K", 2025, size=10)
            query2 = captured["payload"]["query"]["query_string"]["query"]
            assert "filedAt:[2025-01-01 TO 2025-12-31]" in query2


# ═══ 3. 10q 入口 report_period 透传 ═══


class TestTenQReportPeriodPassthrough:
    def test_10q_forwards_report_period_to_form(self, adapter):
        """FPI 公司 10q → 6-K: report_period 之前被 **kwargs 吞掉"""
        captured = {}

        def fake_form(code, form_type, year, checkpoint, on_progress,
                      report_period=None):
            captured["form_type"] = form_type
            captured["report_period"] = report_period
            return MagicMock(success=True)

        with patch.object(
            adapter, "get_quarterly_form_type", return_value="6-K"
        ), patch.object(adapter, "_download_form", side_effect=fake_form):
            adapter._download_10q(
                "XNET", 2026, None, None, None, report_period="2026Q2"
            )

        assert captured["form_type"] == "6-K"
        assert captured["report_period"] == "2026Q2"

    def test_10q_without_report_period_still_works(self, adapter):
        with patch.object(
            adapter, "get_quarterly_form_type", return_value="10-Q"
        ), patch.object(adapter, "_download_form") as mock_form:
            mock_form.return_value = MagicMock(success=True)
            adapter._download_10q("AAPL", 2026, None, None, None)

        mock_form.assert_called_once()
        assert mock_form.call_args.kwargs.get("report_period") is None
