"""
Regression tests for #60: 6-K 季度选择 bug — 目标季度窗口内无 filing 时
size fallback 无视季度 → 选到错误季度财报.

真实事故: DB id=136, PDD 2026Q2 实际下成 2025Q4 财报 (3/26 6-K).
根因链:
1. 6-K exhibit 描述全是通用 "EXHIBIT 99.1" → 无法区分季度 → 打分 0
2. 目标季度窗口 (2026Q2 → 7/14~9/23) 内无 filing (8/21 财报缓存未收录)
   → 窗口逻辑被跳过
3. 核心 bug: 窗口失效后回退 max(filings, key=size) 选 size 最大 → 完全无视
   季度 → 选中 3/26 (293067 > 212733)
4. verifier 只验"是不是财报"不验"是不是这个季度" → 错误静默放行

修复 (unified-downloader 侧):
- _pick_earnings_6k 窗口边界硬约束: 有 report_period 且窗口内无 filing → 返回
  None, 不再落到跨季度的旧逻辑
- _download_form: _quarter_window_missed 命中时明确返回
  NO_FILINGS_IN_QUARTER_WINDOW 失败 (含单条 6-K 情形), 绝不回退跨季度选文件

这些测试只测选择/失败语义, 不下载真实文件.
"""
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import (  # noqa: E402
    MStockAdapter,
    _quarter_window,
    _quarter_window_missed,
)


def _mk_filing(accession: str, size: int, filed_at: str,
               exhibits=None, name: str = ""):
    """构造一条 filing dict（模拟 _search_edgar 返回, 通用 EXHIBIT 99.1 exhibit）."""
    return {
        "ticker": "PDD",
        "formType": "6-K",
        "filedAt": filed_at,
        "accessionNo": accession,
        "cik": "1737806",
        "companyName": name or accession,
        "linkToTxt": f"https://example.com/{accession}/index.html",
        "linkToHtml": f"https://example.com/{accession}/index.html",
        "source": "edgar",
        "size": size,
        "_exhibits": exhibits
        or [{"url": f"https://example.com/{accession}/ex99.htm",
             "description": "EXHIBIT 99.1",
             "document": "tm1_ex99-1.htm"}],
    }


@pytest.fixture
def adapter():
    return MStockAdapter(MagicMock(), [])


# ═══ 窗口工具函数 ═══


class TestQuarterWindowHelpers:
    def test_quarter_window_q2(self):
        assert _quarter_window("2026Q2") == (date(2026, 7, 14), date(2026, 9, 23))

    def test_quarter_window_none_for_no_period(self):
        assert _quarter_window(None) is None
        assert _quarter_window("nonsense") is None

    def test_quarter_window_missed_true_when_empty(self):
        filings = [
            _mk_filing("MAR-Q4", 293067, "2026-03-26"),  # 窗口外 (2025Q4 财报, size 更大)
            _mk_filing("JUL-PR", 60000, "2026-07-08"),   # 窗口外 (窗口 7/14 才开)
        ]
        assert _quarter_window_missed(filings, "2026Q2") is True

    def test_quarter_window_missed_false_when_hit(self):
        filings = [_mk_filing("AUG-Q2", 212733, "2026-08-13")]  # 窗口内
        assert _quarter_window_missed(filings, "2026Q2") is False

    def test_quarter_window_missed_false_without_period(self):
        # 无 report_period → 无窗口约束 → 不算 miss (保持旧 size 回退语义)
        filings = [_mk_filing("A", 1000, "2026-03-26")]
        assert _quarter_window_missed(filings, None) is False


# ═══ _pick_earnings_6k 窗口硬约束 ═══


class TestPickEarningsWindowHardConstraint:
    def test_window_empty_returns_none_not_cross_quarter(self, adapter):
        """#60 核心: 窗口内无 filing → 返回 None, 绝不落到策略 2 选窗口外的 Q4.

        修复前: 窗口为空 → 旧逻辑对 3/26 (293067) 等窗口外 exhibit 打分/回退,
        选错季度. 修复后: 直接 None, 由 _download_form 明确失败.
        """
        filings = [
            _mk_filing("MAR-Q4", 293067, "2026-03-26"),  # 2025Q4 财报, size 更大
            _mk_filing("JUN-PR", 50000, "2026-06-15"),
            _mk_filing("JUL-PR", 212733, "2026-07-08"),  # 窗口 7/14 前
        ]
        picked = adapter._pick_earnings_6k(filings, report_period="2026Q2")
        assert picked is None

    def test_window_hit_picks_in_window_even_if_outside_larger(self, adapter):
        """窗口内命中 → 选窗口内 filing, 窗口外 size 更大的 3/26 不能赢."""
        filings = [
            _mk_filing("AUG-Q2", 212733, "2026-08-13"),   # 窗口内 (真 Q2)
            _mk_filing("MAR-Q4", 293067, "2026-03-26"),   # 窗口外, size 更大
            _mk_filing("JUL-PR", 50000, "2026-07-08"),
        ]
        picked = adapter._pick_earnings_6k(filings, report_period="2026Q2")
        assert picked is not None
        assert picked["accessionNo"] == "AUG-Q2"

    def test_no_report_period_preserves_old_logic(self, adapter):
        """无 report_period → 完全旧逻辑 (兼容 RACE/NVO)."""
        filings = [
            _mk_filing("A", 1000, "2026-08-13"),
            _mk_filing("B", 2000, "2026-07-08"),
        ]
        assert adapter._pick_earnings_6k(filings) is None  # 全通用 exhibit → None


# ═══ _download_form 失败语义 ═══


class TestDownloadFormQuarterWindowMiss:
    def test_window_miss_returns_failure_not_wrong_quarter(self, adapter):
        """#60 核心事故复现: PDD 2026Q2, 窗口内无 filing → 明确失败,
        绝不把 3/26 (2025Q4, size 更大) 下下来."""
        filings = [
            _mk_filing("MAR-Q4", 293067, "2026-03-26"),  # 旧逻辑会 max(size) 选它
            _mk_filing("JUL-PR", 212733, "2026-07-08"),  # 窗口 (7/14) 前
            _mk_filing("JUN-PR", 50000, "2026-06-15"),
        ]
        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing") as mock_dl:
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is False
        assert result.error_code == "NO_FILINGS_IN_QUARTER_WINDOW"
        mock_dl.assert_not_called()  # 关键: 什么都没下载

    def test_window_hit_still_downloads_in_window(self, adapter):
        """窗口内有 filing → 正常下载窗口内的财报 (8/13), 而非 size 更大的 3/26."""
        filings = [
            _mk_filing("AUG-Q2", 212733, "2026-08-13"),  # 真 Q2 财报
            _mk_filing("MAR-Q4", 293067, "2026-03-26"),  # size 更大但窗口外
            _mk_filing("JUL-PR", 50000, "2026-07-08"),
        ]
        captured = {}
        ok = MagicMock(success=True)

        def fake_dl(filing, *args, **kw):
            captured["filing"] = filing
            return ok

        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing", side_effect=fake_dl):
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is True
        assert captured["filing"]["accessionNo"] == "AUG-Q2"
        assert captured["filing"]["size"] == 212733

    def test_single_filing_outside_window_fails(self, adapter):
        """单条 6-K 也在窗口外 → 明确失败, 不硬下错误季度."""
        filings = [_mk_filing("MAR-Q4", 293067, "2026-03-26")]
        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing") as mock_dl:
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is False
        assert result.error_code == "NO_FILINGS_IN_QUARTER_WINDOW"
        mock_dl.assert_not_called()

    def test_single_filing_in_window_downloads(self, adapter):
        """单条 6-K 在窗口内 → 正常下载."""
        filings = [_mk_filing("AUG-Q2", 212733, "2026-08-13")]
        captured = {}
        ok = MagicMock(success=True)

        def fake_dl(filing, *args, **kw):
            captured["filing"] = filing
            return ok

        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing", side_effect=fake_dl):
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is True
        assert captured["filing"]["accessionNo"] == "AUG-Q2"

    def test_no_report_period_keeps_size_fallback(self, adapter):
        """无 report_period → 保持 NVO 式 size 回退 (回归保护)."""
        filings = [
            _mk_filing("PR-BUYBACK", 167508, "2026-08-10"),
            _mk_filing("FULL-EARN", 2784789, "2026-08-04"),  # size 最大
        ]
        captured = {}
        ok = MagicMock(success=True)

        def fake_dl(filing, *args, **kw):
            captured["filing"] = filing
            return ok

        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing", side_effect=fake_dl):
            result = adapter._download_form("NVO", "6-K", 2026, None, None)

        assert result.success is True
        assert captured["filing"]["accessionNo"] == "FULL-EARN"

    def test_unparseable_report_period_keeps_size_fallback(self, adapter):
        """report_period 无法解析出窗口 → 不启用硬约束, 保持 size 回退."""
        filings = [
            _mk_filing("PR-BUYBACK", 167508, "2026-08-10"),
            _mk_filing("FULL-EARN", 2784789, "2026-08-04"),
        ]
        captured = {}
        ok = MagicMock(success=True)

        def fake_dl(filing, *args, **kw):
            captured["filing"] = filing
            return ok

        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing", side_effect=fake_dl):
            result = adapter._download_form(
                "NVO", "6-K", 2026, None, None, report_period="not-a-period"
            )

        assert result.success is True
        assert captured["filing"]["accessionNo"] == "FULL-EARN"
