"""
Unit tests for MStockAdapter 6-K report_period window matching.

Regression: 2026-08-14 XNET — 迅雷 6-K exhibit 描述全是通用 "EXHIBIT 99.1",
document 全是 tmXXX_ex99-1.htm, 不含财报关键词 → _pick_earnings_6k 全 0 分
→ 回退按 size 选最大, 误选 3-12 的 Q4+FY2025 (size=244676) 而非 8-13 的 Q2
(size=206088).

修复: _pick_earnings_6k 接受 report_period (如 "2026Q2"), 用 filedAt 日期
窗口优先匹配目标季度 (季度结束日 + 25~85 天). 窗口内优先选 exhibit 打分最高,
全 0 分则选 size 最大.

这些测试只测选择逻辑, 不下载真实文件.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import MStockAdapter  # noqa: E402


def _mk_filing(accession: str, size: int, filed_at: str = "2026-08-04",
               exhibits=None, name: str = ""):
    """构造一条 filing dict（模拟 _search_edgar 返回）。"""
    return {
        "ticker": "XNET",
        "formType": "6-K",
        "filedAt": filed_at,
        "accessionNo": accession,
        "cik": "1510593",
        "companyName": name or accession,
        "linkToTxt": f"https://example.com/{accession}/index.html",
        "linkToHtml": f"https://example.com/{accession}/index.html",
        "source": "edgar",
        "size": size,
        "_exhibits": exhibits or [],
    }


# XNET 真实 6-K 列表 (2026, 按日期倒序, 2026-08-14 从 SEC submissions 拉取)
_XNET_2026_FILINGS = [
    _mk_filing("0001104659-26-095490", 206088, "2026-08-13",
               exhibits=[{"url": "https://x/tm2623122d1_ex99-1.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm2623122d1_ex99-1.htm"}]),
    _mk_filing("0001104659-26-078005", 17272, "2026-06-26",
               exhibits=[{"url": "https://x/tm2619070d1_ex99-1.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm2619070d1_ex99-1.htm"}]),
    _mk_filing("0001104659-26-067217", 215461, "2026-05-28",
               exhibits=[{"url": "https://x/tm2615874d1_ex99-1.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm2615874d1_ex99-1.htm"}]),
    _mk_filing("0001104659-26-028066", 17632, "2026-03-16",
               exhibits=[{"url": "https://x/tm268900d1_ex99-1.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm268900d1_ex99-1.htm"}]),
    _mk_filing("0001104659-26-026698", 244676, "2026-03-12",
               exhibits=[{"url": "https://x/tm268655d1_ex99-1.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm268655d1_ex99-1.htm"}]),
    _mk_filing("0001104659-26-022569", 45913, "2026-03-03",
               exhibits=[{"url": "https://x/tm267857d1_ex99-1.htm",
                          "description": "EXHIBIT 99.1",
                          "document": "tm267857d1_ex99-1.htm"}]),
]


class TestSixKReportPeriodWindow:
    """6-K report_period 季度窗口匹配"""

    @pytest.fixture
    def adapter(self):
        http_client = MagicMock()
        datasources: list = []
        return MStockAdapter(http_client, datasources)

    def test_report_period_q2_selects_aug_q2_not_march_q4(self, adapter):
        """XNET 2026Q2: 应选 8-13 的 Q2, 而非 3-12 的 Q4+FY2025 (size更大)."""
        picked = adapter._pick_earnings_6k(_XNET_2026_FILINGS, report_period="2026Q2")
        assert picked is not None
        assert picked["accessionNo"] == "0001104659-26-095490"

    def test_report_period_q1_selects_may_q1(self, adapter):
        """XNET 2026Q1: 应选 5-28 的 Q1, 而非 3-12 的 Q4."""
        picked = adapter._pick_earnings_6k(_XNET_2026_FILINGS, report_period="2026Q1")
        assert picked is not None
        assert picked["accessionNo"] == "0001104659-26-067217"

    def test_report_period_fy_falls_back_to_old_logic(self, adapter):
        """FY 无季度窗口 → 回退旧逻辑 (exhibit 打分 / size)."""
        # 所有 exhibit 描述通用 → 全 0 分 → 旧逻辑返回 None
        picked = adapter._pick_earnings_6k(_XNET_2026_FILINGS, report_period="2026FY")
        # 旧逻辑: 无 exhibit 可打分 → None
        assert picked is None

    def test_no_report_period_preserves_old_behavior(self, adapter):
        """不传 report_period → 完全旧逻辑 (兼容 RACE/NVO 现有 case)."""
        picked = adapter._pick_earnings_6k(_XNET_2026_FILINGS)
        # 全通用 exhibit → 旧逻辑 None
        assert picked is None

    def test_q2_window_prefers_exhibit_score_when_available(self, adapter):
        """窗口内优先选 exhibit 打分最高者."""
        earnings = _mk_filing(
            "Q2-EARN", 150000, "2026-08-10",
            exhibits=[{"url": "https://x/result.htm",
                       "description": "Second Quarter 2026 Results",
                       "document": "xq2results.htm"}],
        )
        generic = _mk_filing("Q2-GENERIC", 300000, "2026-08-13",
                             exhibits=[{"url": "https://x/ex99.htm",
                                        "description": "EXHIBIT 99.1",
                                        "document": "tm123_ex99-1.htm"}])
        filings = [generic, earnings]
        picked = adapter._pick_earnings_6k(filings, report_period="2026Q2")
        assert picked is not None
        assert picked["accessionNo"] == "Q2-EARN"

    def test_q2_window_generic_only_selects_largest_in_window(self, adapter):
        """窗口内全是通用 exhibit → 选窗口内 size 最大 (Q2 8-13 vs Q4 3-12)."""
        picked = adapter._pick_earnings_6k(_XNET_2026_FILINGS, report_period="2026Q2")
        assert picked["accessionNo"] == "0001104659-26-095490"
        # 窗口外 (3-12, size 更大) 不能赢过窗口内
        assert picked["size"] == 206088

    def test_download_form_size_fallback_selects_largest(self, adapter):
        """_download_form: _pick_earnings_6k 返回 None 时选 size 最大 filing.

        PR #48 review 建议: 覆盖真实 size 回退路径 (_download_form L490-505),
        而非只测 Python max() builtin.
        """
        from unittest.mock import patch

        # 通用 exhibit → _pick_earnings_6k 全 0 分 → None → 触发 size 回退
        filings = [
            _mk_filing("ACC-SMALL", 50000, "2026-08-13",
                       exhibits=[{"url": "https://x/ex99.htm",
                                  "description": "EXHIBIT 99.1",
                                  "document": "tm1_ex99-1.htm"}]),
            _mk_filing("ACC-BIG", 300000, "2026-07-30",
                       exhibits=[{"url": "https://x/ex99.htm",
                                  "description": "EXHIBIT 99.1",
                                  "document": "tm2_ex99-1.htm"}]),
            _mk_filing("ACC-MID", 100000, "2026-08-01",
                       exhibits=[{"url": "https://x/ex99.htm",
                                  "description": "EXHIBIT 99.1",
                                  "document": "tm3_ex99-1.htm"}]),
        ]
        captured = {}

        def fake_download_filing(filing, ticker, form_type, year, on_progress, checkpoint):
            captured["filing"] = filing
            from unified_downloader.models.entities import DownloadResult
            return DownloadResult(success=True, file_path="/tmp/x.html",
                                  file_size=0)

        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing", side_effect=fake_download_filing):
            result = adapter._download_form("XNET", "6-K", 2026, None, None)

        assert result.success
        assert captured["filing"]["accessionNo"] == "ACC-BIG"
        assert captured["filing"]["size"] == 300000
