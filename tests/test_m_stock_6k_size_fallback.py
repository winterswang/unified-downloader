"""
Unit tests for MStockAdapter 6-K "size-largest fallback" behavior.

Regression: 2026-08-13 NVO — 诺和诺德 6-K 是"单文档结构"（正文=primary doc，
无独立 EX-99 exhibit），_search_edgar 提取的 _exhibits 为空 → _pick_earnings_6k
全部 0 分返回 None → 旧逻辑回退 filings[0]（最新一条），抓到 8-10 股票回购
公告 (24KB) 而非 8-4 完整 Q2 财报 (caq22026.htm 1.73MB) → SUSPICIOUS_TOO_SMALL。

修复: 回退时选 size 最大的 filing（财报正文通常远大于普通公告）。

这些测试只测选择逻辑，不下载真实文件。
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
        "ticker": "NVO",
        "formType": "6-K",
        "filedAt": filed_at,
        "accessionNo": accession,
        "cik": "353278",
        "companyName": name or accession,
        "linkToTxt": f"https://example.com/{accession}/index.html",
        "linkToHtml": f"https://example.com/{accession}/index.html",
        "source": "edgar",
        "size": size,
        "_exhibits": exhibits or [],
    }


class TestSixKSizeLargestFallback:
    """6-K 无 exhibit 时回退选 size 最大的 filing"""

    @pytest.fixture
    def adapter(self):
        http_client = MagicMock()
        datasources: list = []
        return MStockAdapter(http_client, datasources)

    def test_pick_earnings_returns_none_when_no_exhibits(self, adapter):
        """所有 filing 无 _exhibits → _pick_earnings_6k 返回 None"""
        filings = [
            _mk_filing("A", 167508),
            _mk_filing("B", 2784789),
            _mk_filing("C", 167154),
        ]
        picked = adapter._pick_earnings_6k(filings)
        assert picked is None  # 无 exhibit 可打分

    def test_fallback_selects_largest_size(self, adapter):
        """
        回退应选 size 最大的 filing（完整财报 2.78MB），
        而非 filings[0]（回购公告 167KB）。
        """
        filings = [
            _mk_filing("0001171843-26-005376", 167508, "2026-08-10"),  # 回购公告
            _mk_filing("0000353278-26-000023", 2784789, "2026-08-04"),  # 完整财报
            _mk_filing("0001171843-26-005184", 167154, "2026-08-04"),  # 摘要
        ]
        best = max(filings, key=lambda f: int(f.get("size") or 0))
        assert best["accessionNo"] == "0000353278-26-000023"
        assert best["size"] == 2784789

    def test_size_zero_falls_back_to_first(self, adapter):
        """所有 size=0 时，回退仍取 filings[0]（不 crash）"""
        filings = [
            _mk_filing("A", 0),
            _mk_filing("B", 0),
            _mk_filing("C", 0),
        ]
        best = max(filings, key=lambda f: int(f.get("size") or 0))
        assert best["accessionNo"] == "A"

    def test_large_exhibit_still_preferred(self, adapter):
        """有财报 exhibit 的仍优先于 size 回退（关键词打分优先）"""
        # RACE 场景: 财报 exhibit 含关键词 → 应被选中
        earnings = _mk_filing(
            "RACE-EARN", 20000, "2026-07-30",
            exhibits=[{
                "url": "https://x/ferrarinvinterimreport-063",
                "description": "Interim Report",
                "document": "ferrarinvinterimreport-063.htm",
            }],
        )
        pr = _mk_filing("RACE-PR", 50000, "2026-07-31")  # 更大但无财报 exhibit
        filings = [pr, earnings]
        picked = adapter._pick_earnings_6k(filings)
        assert picked is not None
        assert picked["accessionNo"] == "RACE-EARN"
