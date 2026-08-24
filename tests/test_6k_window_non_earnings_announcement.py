"""
Regression tests for #62: 6-K 季度窗口内只有非财报公告时不再下载公告.

真实事故 (2026-08-24 实测): 修复 #60 后重跑 PDD 2026Q2, 窗口 (7/14~9/23)
命中 8/21 的「董事去世公告」(6-K, 11.1KB, exhibit 描述通用 "EXHIBIT 99.1" 全
0 分) → 旧逻辑"全 0 分选窗口内 size 最大"把它下下来 ( downloads/m/PDD/
PDD_2026_6K.html 11.1KB ), 依赖 morning-brief verifier 兜底拦截.

修复: 窗口内 exhibit 全 0 分时, size 最大者必须 ≥ MIN_EARNINGS_FILING_SIZE
(50KB, 真实财报 200KB+ vs 公告 <45KB) 才接受; 全部低于下限 = 窗口内只有
非财报公告 → _pick_earnings_6k 返回 None → _download_form 明确失败
NO_FILINGS_IN_QUARTER_WINDOW (等真财报), 而不是下载公告.

这些测试只测选择/失败语义, 不下载真实文件.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import (  # noqa: E402
    MStockAdapter,
    MIN_EARNINGS_FILING_SIZE,
)


def _mk_filing(accession: str, size: int, filed_at: str,
               exhibits=None, name: str = ""):
    """构造一条 filing dict（模拟 _search_edgar 返回, 默认通用 EXHIBIT 99.1）."""
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


class TestPickEarningsNonEarningsGate:
    def test_window_only_announcement_returns_none(self, adapter):
        """#62 核心: 窗口内只有 11.1KB 董事去世公告 → 返回 None, 不下载."""
        filings = [
            _mk_filing("AUG-DEATH-NOTICE", 11366, "2026-08-21"),  # 董事去世公告
            _mk_filing("JUL-PR", 30000, "2026-07-20"),            # 窗口内小 PR
        ]
        picked = adapter._pick_earnings_6k(filings, report_period="2026Q2")
        assert picked is None

    def test_window_announcement_plus_large_generic_picks_large(self, adapter):
        """窗口内公告 + 大的通用 exhibit 财报 → 选大的那个 (旧语义保留)."""
        filings = [
            _mk_filing("AUG-DEATH-NOTICE", 11366, "2026-08-21"),
            _mk_filing("AUG-Q2-EARN", 212733, "2026-08-21"),  # 真财报, 同日
        ]
        picked = adapter._pick_earnings_6k(filings, report_period="2026Q2")
        assert picked is not None
        assert picked["accessionNo"] == "AUG-Q2-EARN"

    def test_keyword_scored_tiny_filing_still_accepted(self, adapter):
        """exhibit 有关键词命中 (打分 >0) → 即使 size 小也接受 (关键词可信)."""
        scored_tiny = _mk_filing(
            "AUG-EARN-PR", 15000, "2026-08-21",
            exhibits=[{
                "url": "https://x/q2results.htm",
                "description": "Second Quarter 2026 Financial Results",
                "document": "q2results.htm",
            }],
        )
        picked = adapter._pick_earnings_6k([scored_tiny], report_period="2026Q2")
        assert picked is not None
        assert picked["accessionNo"] == "AUG-EARN-PR"

    def test_size_unknown_fails_closed(self, adapter):
        """size 未知 (sec-api 兜底路径无 size 字段) → fail-closed 返回 None.

        PR #63 review 实证: sec-api filings 无 size/_exhibits, size=0 被当
        "未知→接受" 会让 PDD 8/21 公告场景在该路径照旧下载 — 无法证明"像
        财报"时宁可失败等可证数据, 不赌下载.
        """
        filings = [_mk_filing("A", 0, "2026-08-21")]
        picked = adapter._pick_earnings_6k(filings, report_period="2026Q2")
        assert picked is None

    def test_sec_api_shaped_filing_without_size_not_downloaded(self, adapter):
        """#62 review 复现: sec-api 形态 filing (无 size 无 _exhibits) 在窗口内
        → _download_form 明确失败, 不下载."""
        sec_api_filing = {
            "ticker": "PDD",
            "formType": "6-K",
            "filedAt": "2026-08-21T00:00:00",
            "accessionNo": "0001193125-26-000123",
            "cik": "1737806",
            "companyName": "PDD Holdings Inc.",
            "description": "Announcement of death of director",
            "linkToTxt": "https://example.com/x.htm",
            "linkToHtml": "https://example.com/x.htm",
            "source": "sec_api",
        }
        with patch.object(adapter, "_search_filings", return_value=[sec_api_filing]), \
             patch.object(adapter, "_download_filing") as mock_dl:
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is False
        assert result.error_code == "NO_FILINGS_IN_QUARTER_WINDOW"
        mock_dl.assert_not_called()

    def test_threshold_boundary(self, adapter):
        """下限边界: 低于 MIN_EARNINGS_FILING_SIZE 拒绝, 等于则接受."""
        below = _mk_filing("BELOW", MIN_EARNINGS_FILING_SIZE - 1, "2026-08-21")
        assert adapter._pick_earnings_6k([below], report_period="2026Q2") is None

        at_min = _mk_filing("AT-MIN", MIN_EARNINGS_FILING_SIZE, "2026-08-21")
        picked = adapter._pick_earnings_6k([at_min], report_period="2026Q2")
        assert picked is not None
        assert picked["accessionNo"] == "AT-MIN"


class TestDownloadFormNonEarningsGate:
    def test_window_only_announcement_fails_not_downloads(self, adapter):
        """#62 核心事故复现: 窗口内只有 8/21 公告 → 明确失败, 不下载公告."""
        filings = [
            _mk_filing("AUG-DEATH-NOTICE", 11366, "2026-08-21"),
            _mk_filing("JUL-PR", 30000, "2026-07-20"),
        ]
        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing") as mock_dl:
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is False
        assert result.error_code == "NO_FILINGS_IN_QUARTER_WINDOW"
        # 区分 #60 (窗口内无 filing) vs #62 (窗口内只有公告)
        assert "非财报公告" in result.error_message
        mock_dl.assert_not_called()  # 关键: 公告没被下载

    def test_window_miss_message_distinguishes_empty_window(self, adapter):
        """#60 场景报"窗口内无 filing", 与 #62 的"只有非财报公告"区分."""
        filings = [
            _mk_filing("MAR-Q4", 293067, "2026-03-26"),  # 窗口外
            _mk_filing("JUL-PR", 30000, "2026-07-08"),   # 窗口 (7/14) 前
        ]
        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing") as mock_dl:
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is False
        assert result.error_code == "NO_FILINGS_IN_QUARTER_WINDOW"
        assert "窗口内无 filing" in result.error_message
        mock_dl.assert_not_called()

    def test_real_earnings_in_window_still_downloads(self, adapter):
        """窗口内出现真财报 (200KB+) → 正常下载, 不被防御误伤."""
        filings = [
            _mk_filing("AUG-DEATH-NOTICE", 11366, "2026-08-21"),
            _mk_filing("AUG-Q2-EARN", 212733, "2026-08-21"),  # 财报盘后入库
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
        assert captured["filing"]["accessionNo"] == "AUG-Q2-EARN"

    def test_single_tiny_in_window_fails(self, adapter):
        """单条窗口内小公告 (真实场景: 财报未出, 缓存只有 8/21 公告) → 失败."""
        filings = [_mk_filing("AUG-DEATH-NOTICE", 11366, "2026-08-21")]
        with patch.object(adapter, "_search_filings", return_value=filings), \
             patch.object(adapter, "_download_filing") as mock_dl:
            result = adapter._download_form(
                "PDD", "6-K", 2026, None, None, report_period="2026Q2"
            )

        assert result.success is False
        assert result.error_code == "NO_FILINGS_IN_QUARTER_WINDOW"
        mock_dl.assert_not_called()
