"""
Unit tests for HStockAdapter 季度业绩类别空手 → 标题关键词回退 (9992, 2026-11-02).

Regression: 泡泡玛特 (09992) 2026Q3《二零二六年第三季度最新業務狀況》挂在普通
公告类别, 披露易「季度业绩」类别 (t2code=13600) 检索恒空 → NO_FILINGS_FOUND,
morning-brief 下载重试 3/3 烧完后未闭环. 修复: 类别空手时回退同 stockId +
季度标题关键词检索 (限本股、排除翌日披露报表/月报表噪音).
"""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.h_stock import HStockAdapter, HKExAPI  # noqa: E402
from unified_downloader.models.entities import DownloadResult  # noqa: E402


def _doc(title="二零二六年第三季度最新業務狀況", code="09992",
         link="https://www1.hkexnews.hk/listedco/listconews/sehk/q3.pdf"):
    return {
        "news_id": "N1",
        "stock_code": code,
        "stock_name": "泡泡瑪特",
        "title": title,
        "file_type": "PDF",
        "file_info": "1MB",
        "file_link": link,
        "date_time": "27/10/2026 16:30",
    }


@pytest.fixture
def adapter():
    adapter = HStockAdapter(MagicMock(), [])
    adapter._rate_limiter = MagicMock()
    return adapter


class TestQuarterlyTitleFallback:
    """季度业绩类别空手 → 标题关键词回退"""

    @staticmethod
    def _patch_stock(monkeypatch, stock_id="1000068054"):
        monkeypatch.setattr(
            "adapters.h_stock.HStockAdapter._get_stock_id",
            lambda self, code: stock_id,
        )

    def test_category_miss_falls_back_to_title_hit(self, adapter, monkeypatch):
        """类别检索空 → 标题回退命中 → 走 _download_from_doc."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        # 第一次调用 (季度业绩类别) 空, 第二次 (标题 '第三季度') 命中
        api.search_documents.side_effect = [[], [_doc()]]
        with patch.object(adapter, "_get_api", return_value=api):
            sentinel = DownloadResult(success=True, file_path="/tmp/x.pdf",
                                      file_size=1, source="hkex")
            with patch.object(adapter, "_download_from_doc",
                              return_value=sentinel) as mock_dl:
                result = adapter.download(
                    "9992", 2026, "quarterly",
                    checkpoint=None, on_progress=None,
                    report_period="2026Q3",
                )
        assert result is sentinel
        mock_dl.assert_called_once()
        args = mock_dl.call_args.args
        assert args[0]["title"] == "二零二六年第三季度最新業務狀況"
        assert args[2] == "quarterly_report"

    def test_q3_period_searches_third_quarter_first(self, adapter, monkeypatch):
        """report_period=2026Q3 → 先查 '第三季度', 再兜底 'Q3'."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        api.search_documents.side_effect = [[], [], []]
        with patch.object(adapter, "_get_api", return_value=api):
            adapter._download_quarterly_report(
                "9992", 2026, None, None, None, report_period="2026Q3")
        calls = api.search_documents.call_args_list
        assert calls[0].kwargs["doc_type"] == HKExAPI.QUARTERLY_RESULTS
        titles = [c.kwargs["title"] for c in calls[1:]]
        assert titles == ["第三季度", "Q3"]

    def test_unknown_period_defaults_to_q1_q3_zh(self, adapter, monkeypatch):
        """无 report_period → 默认查 ['第三季度', '第一季度']."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        api.search_documents.side_effect = [[], [], []]
        with patch.object(adapter, "_get_api", return_value=api):
            adapter._download_quarterly_report("9992", 2026, None, None, None)
        titles = [c.kwargs["title"] for c in
                  api.search_documents.call_args_list[1:]]
        assert titles == ["第三季度", "第一季度"]

    def test_noise_announcements_filtered(self, adapter, monkeypatch):
        """翌日披露报表/月报表混入标题结果时被滤掉, 下载真季报."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        api.search_documents.side_effect = [
            [],  # 季度业绩类别检索: 空 (触发标题回退)
            [    # 标题 '第三季度' 回退: 混入噪音 + 真季报
                _doc(title="翌日披露報表 - [股份購回]"),
                _doc(title="截至二零二六年九月三十日止月份之股份發行人的證券變動月報表"),
                _doc(),
            ],
        ]
        with patch.object(adapter, "_get_api", return_value=api):
            sentinel = DownloadResult(success=True, file_path="/tmp/x.pdf",
                                      file_size=1, source="hkex")
            with patch.object(adapter, "_download_from_doc",
                              return_value=sentinel) as mock_dl:
                adapter._download_quarterly_report(
                    "9992", 2026, None, None, None, report_period="2026Q3")
        args = mock_dl.call_args.args
        assert args[0]["title"] == "二零二六年第三季度最新業務狀況"

    def test_category_hit_skips_title_fallback(self, adapter, monkeypatch):
        """类别检索直接命中时不发标题回退请求 (行为保持不变)."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        api.search_documents.side_effect = None
        api.search_documents.return_value = [_doc(title="二零二六年第三季度業績公告")]
        with patch.object(adapter, "_get_api", return_value=api), \
             patch.object(adapter, "_download_report") as mock_report:
            mock_report.return_value = DownloadResult(
                success=True, file_path="/tmp/x.pdf", file_size=1, source="hkex")
            adapter._download_quarterly_report(
                "9992", 2026, None, None, None, report_period="2026Q3")
        assert api.search_documents.call_count == 0

    def test_title_fallback_also_miss_keeps_original_failure(self, adapter, monkeypatch):
        """类别与标题回退都空 → 维持原 NO_FILINGS_FOUND."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        api.search_documents.side_effect = [[], [], []]
        with patch.object(adapter, "_get_api", return_value=api):
            result = adapter._download_quarterly_report(
                "9992", 2026, None, None, None, report_period="2026Q3")
        assert not result.success
        assert result.error_code == "NO_FILINGS_FOUND"

    def test_fallback_search_scoped_to_year_and_stock(self, adapter, monkeypatch):
        """标题回退检索必须带 stockId + 年度窗口 + 全类别."""
        self._patch_stock(monkeypatch)
        api = MagicMock()
        api.search_documents.side_effect = [[], [], []]
        with patch.object(adapter, "_get_api", return_value=api):
            adapter._download_quarterly_report(
                "9992", 2026, None, None, None, report_period="2026Q3")
        for c in api.search_documents.call_args_list:
            kw = c.kwargs
            assert kw["stock_id"] == "1000068054"
            assert kw["from_date"] == date(2026, 1, 1)
            assert kw["to_date"] == date(2026, 12, 31)
        # 全类别参数单独核对第一与第二次调用
        first = api.search_documents.call_args_list[0].kwargs
        second = api.search_documents.call_args_list[1].kwargs
        assert first["doc_type"] == {"t1code": "10000", "t2Gcode": "3",
                                     "t2code": "13600"}
        assert second["doc_type"] == {"t1code": "-2", "t2Gcode": "-2",
                                      "t2code": "-2"}
