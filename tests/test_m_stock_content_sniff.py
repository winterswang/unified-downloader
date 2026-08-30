"""
Unit tests for 6-K 内容嗅探选择 (#68, 2026-08-30).

Regression: #66 修复后 BABA 2027Q1 财年窗口正确命中 2026-07-14~09-23
(含 8/20 真财报), 但窗口内 10 条 filing 的 exhibit 元数据全是通用描述
("EXHIBIT 99.1") -> _exhibit_score 全 0 分 -> "size 最大" 回退选中 8/6 的
月度股份申报 (Monthly Return, 1.24MB) - 它比真财报 (8/20, 481KB) 还大,
MIN_EARNINGS_FILING_SIZE (50KB) 拦不住大号非财报申报.

修复: 窗口内 ≥ 下限候选多条且元数据全 0 分时, _pick_by_content_sniff 拉
各候选 exhibit 头部内容做 _EARNINGS_KEYWORDS 计数, 密度最高者胜出
(实测 8/20 真财报 92 次, 月度申报 0 次). 全部拉取失败退回 size 最大.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import MStockAdapter  # noqa: E402


def _mk_filing(accession: str, size: int, filed_at: str, exhibits=None):
    """构造一条 filing dict（模拟 _search_edgar 返回, 通用 EXHIBIT 99.1）."""
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
        "_exhibits": exhibits if exhibits is not None else [
            {"url": f"https://example.com/{accession}/ex99-1.htm",
             "description": "EXHIBIT 99.1",
             "document": f"{accession}_ex99-1.htm"}],
    }


class _FakeResp:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


_EARNINGS_TEXT = (
    "Total revenue was RMB 247,652 million for the quarter ended June 30, "
    "2026. Net income was RMB 24,932 million and operating income grew. "
    "Diluted earnings per share were RMB 10.41. "
) * 20

_MONTHLY_RETURN_TEXT = (
    "Monthly Return with The Stock Exchange of Hong Kong Limited in "
    "relation to the movements in the issuer's issued shares for July 2026. "
) * 20


class TestContentSniff:
    """#68: 窗口内多条大 filing 元数据全 0 分时按内容关键词密度选择"""

    @pytest.fixture
    def adapter(self):
        adapter = MStockAdapter(MagicMock(), [])
        adapter._sec_user_agent = "test-agent@example.com"
        return adapter

    def test_sniff_picks_real_earnings_over_bigger_monthly_return(self, adapter):
        """BABA 场景: 1.24MB 月度申报 vs 481KB 真财报 -> 内容密度胜出."""
        filings = [
            _mk_filing("BABA-MONTHLY", 1244217, "2026-08-06"),
            _mk_filing("BABA-REAL", 481124, "2026-08-20"),
        ]
        urls = {
            "https://example.com/BABA-MONTHLY/ex99-1.htm": _FakeResp(
                _MONTHLY_RETURN_TEXT.encode()),
            "https://example.com/BABA-REAL/ex99-1.htm": _FakeResp(
                _EARNINGS_TEXT.encode()),
        }
        adapter._http_client = MagicMock()
        adapter._http_client.get.side_effect = lambda url, headers=None: urls[url]

        picked = adapter._pick_earnings_6k(
            filings, report_period="2027Q1", fiscal_end_month=3)
        assert picked is not None
        assert picked["accessionNo"] == "BABA-REAL"

    def test_sniff_all_fetch_fail_falls_back_to_size_max(self, adapter):
        """全部拉取失败 -> 退回 size 最大 (旧行为, fail-open)."""
        filings = [
            _mk_filing("BABA-MONTHLY", 1244217, "2026-08-06"),
            _mk_filing("BABA-REAL", 481124, "2026-08-20"),
        ]
        adapter._http_client = MagicMock()
        adapter._http_client.get.side_effect = ConnectionError("network down")

        picked = adapter._pick_earnings_6k(
            filings, report_period="2027Q1", fiscal_end_month=3)
        assert picked is not None
        # 退回 size 最大 (旧行为)
        assert picked["accessionNo"] == "BABA-MONTHLY"

    def test_single_candidate_skips_sniff(self, adapter):
        """窗口内只有一条大候选 -> 不嗅探, 直接 size 最大."""
        filings = [_mk_filing("BABA-ONLY", 481124, "2026-08-20")]
        adapter._http_client = MagicMock()
        adapter._http_client.get.side_effect = AssertionError("不应发起嗅探请求")

        picked = adapter._pick_earnings_6k(
            filings, report_period="2027Q1", fiscal_end_month=3)
        assert picked is not None
        assert picked["accessionNo"] == "BABA-ONLY"

    def test_non_200_response_scores_zero(self, adapter):
        """403/404 响应计 0 分 -> 退回 size 最大."""
        filings = [
            _mk_filing("BABA-MONTHLY", 1244217, "2026-08-06"),
            _mk_filing("BABA-REAL", 481124, "2026-08-20"),
        ]
        adapter._http_client = MagicMock()
        adapter._http_client.get.return_value = _FakeResp(b"", status_code=403)

        picked = adapter._pick_earnings_6k(
            filings, report_period="2027Q1", fiscal_end_month=3)
        assert picked is not None
        assert picked["accessionNo"] == "BABA-MONTHLY"

    def test_sniff_bounded_to_top_candidates(self, adapter):
        """候选多于 CONTENT_SNIFF_MAX_CANDIDATES 时只嗅探 size top-N."""
        from adapters.m_stock import CONTENT_SNIFF_MAX_CANDIDATES
        filings = [
            _mk_filing(f"FILING-{i}", 100_000 + i, f"2026-08-{i+10:02d}")
            for i in range(CONTENT_SNIFF_MAX_CANDIDATES + 3)
        ]
        # 全部塞真财报文本 -> 若只嗅探 top-N, 请求次数应 ≤ N
        adapter._http_client = MagicMock()
        adapter._http_client.get.return_value = _FakeResp(
            _EARNINGS_TEXT.encode())

        picked = adapter._pick_earnings_6k(
            filings, report_period="2027Q1", fiscal_end_month=3)
        assert picked is not None
        assert adapter._http_client.get.call_count <= CONTENT_SNIFF_MAX_CANDIDATES
