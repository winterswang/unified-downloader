"""
Unit tests for MStockAdapter download()/async_download() doc_type 分发.

Regression (W40-#50, review finding: 静默错误数据):
- quarterly/interim_report/q1_report/q3_report 是 CLI -t 允许的类型,
  之前在 sync 分发里落到 else 分支被当 10-K 年报下载 (要季报给年报,
  文件名标 20F, 成功无告警)。
- sync "8k" 之前跟 "6k" 一起进 _download_6k (内部硬编码 "6-K"),
  而 async 路径正确下载 8-K — 同一调用两个入口行为相反。
- 未知类型之前静默按 10-K 下载, 现在显式抛 UnsupportedOperationError。

这些测试只测分发路由, 不下载真实文件。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import MStockAdapter  # noqa: E402
# 注意: 异常类必须走 unified_downloader.exceptions 导入 (m_stock 内部同一路径),
# 否则 sys.path 双入口会产生两个模块实例, pytest.raises 接不住
from unified_downloader.exceptions import UnsupportedOperationError  # noqa: E402


@pytest.fixture
def adapter():
    """构造一个 MStockAdapter mock 桩 (只测分发, 不下载)"""
    return MStockAdapter(MagicMock(), [])


class TestSyncDispatch:
    """download() 的 doc_type → 内部方法路由"""

    @pytest.mark.parametrize(
        "doc_type",
        ["quarterly", "interim_report", "q1_report", "q3_report", "10q", "ten_q"],
    )
    def test_quarterly_family_routes_to_10q(self, adapter, doc_type):
        """季报家族必须路由到 _download_10q, 绝不能落 10-K (回归核心)"""
        with patch.object(adapter, "_download_10q") as mock_q, \
             patch.object(adapter, "_download_10k") as mock_k:
            mock_q.return_value = "SENTINEL_10Q"
            result = adapter.download("AAPL", 2026, doc_type)

            assert result == "SENTINEL_10Q"
            mock_q.assert_called_once()
            mock_k.assert_not_called(), (
                f"{doc_type} 之前落 else 分支被当 10-K 年报下载 (W40-#50 回归点)"
            )

    @pytest.mark.parametrize("doc_type", ["10k", "ten_k", "annual_report"])
    def test_annual_family_routes_to_10k(self, adapter, doc_type):
        """annual_report 是 CLI/MCP 默认类型, 必须显式路由到 10-K"""
        with patch.object(adapter, "_download_10k") as mock_k:
            mock_k.return_value = "SENTINEL_10K"
            result = adapter.download("AAPL", 2026, doc_type)

            assert result == "SENTINEL_10K"
            mock_k.assert_called_once()

    def test_8k_routes_to_8k_form_not_6k(self, adapter):
        """"8k" 必须下载 8-K, 不能进 _download_6k (内部硬编码 6-K)"""
        with patch.object(adapter, "_download_form") as mock_form, \
             patch.object(adapter, "_download_6k") as mock_6k:
            mock_form.return_value = "SENTINEL_8K"
            result = adapter.download("AAPL", 2026, "8k")

            assert result == "SENTINEL_8K"
            mock_6k.assert_not_called(), (
                "sync '8k' 之前进 _download_6k 实际下载 6-K (W40-#50 回归点)"
            )
            mock_form.assert_called_once()
            assert mock_form.call_args.args[1] == "8-K"

    def test_6k_routes_to_6k(self, adapter):
        """"6k" 保持走 _download_6k (带 report_period 财报窗口逻辑)"""
        with patch.object(adapter, "_download_6k") as mock_6k:
            mock_6k.return_value = "SENTINEL_6K"
            result = adapter.download("AAPL", 2026, "6k", report_period="2026Q2")

            assert result == "SENTINEL_6K"
            mock_6k.assert_called_once()
            assert mock_6k.call_args.kwargs.get("report_period") == "2026Q2"

    def test_unknown_type_raises(self, adapter):
        """未知类型显式报错, 不再静默按 10-K 下载 (错误数据入库)"""
        with patch.object(adapter, "_download_10k") as mock_k:
            with pytest.raises(UnsupportedOperationError):
                adapter.download("AAPL", 2026, "bogus_type")
            mock_k.assert_not_called()


class TestAsyncDispatch:
    """async_download() 的 doc_type 路由必须与 sync 一致"""

    def _run(self, adapter, doc_type):
        return asyncio.run(
            adapter.async_download(MagicMock(), "AAPL", 2026, doc_type)
        )

    @pytest.mark.parametrize(
        "doc_type", ["quarterly", "interim_report", "q1_report", "q3_report"]
    )
    def test_quarterly_family_routes_to_async_10q(self, adapter, doc_type):
        with patch.object(
            adapter, "_async_download_10q", new_callable=AsyncMock
        ) as mock_q, patch.object(
            adapter, "_async_download_10k", new_callable=AsyncMock
        ) as mock_k:
            mock_q.return_value = "SENTINEL_A10Q"
            result = self._run(adapter, doc_type)

            assert result == "SENTINEL_A10Q"
            mock_q.assert_awaited_once()
            mock_k.assert_not_awaited()

    def test_8k_routes_to_8k_form(self, adapter):
        with patch.object(
            adapter, "_async_download_form", new_callable=AsyncMock
        ) as mock_form:
            mock_form.return_value = "SENTINEL_A8K"
            result = self._run(adapter, "8k")

            assert result == "SENTINEL_A8K"
            mock_form.assert_awaited_once()
            assert mock_form.call_args.args[2] == "8-K"

    def test_unknown_type_raises(self, adapter):
        with pytest.raises(UnsupportedOperationError):
            self._run(adapter, "bogus_type")


class TestDispatchConstants:
    """分发表常量与 CLI 允许集的一致性 (cli.py click.Choice 的 m_stock 相关类型)"""

    def test_all_cli_doc_types_are_routable(self):
        """cli.py -t 允许的每个类型都必须命中某条分发分支, 不许落 else 报错"""
        cli_types = {
            "annual_report", "interim_report", "quarterly", "q1_report",
            "q3_report", "prospectus", "10k", "10q", "s1", "s1a", "f1",
            "424b4", "6k", "8k", "20f",
        }
        routable = (
            MStockAdapter._ANNUAL_DOC_TYPES
            | MStockAdapter._QUARTERLY_DOC_TYPES
            | MStockAdapter._PROSPECTUS_DOC_TYPES
            | MStockAdapter._TWENTY_F_DOC_TYPES
            | {"6k", "8k"}
        )
        unroutable = cli_types - routable
        assert not unroutable, (
            f"CLI 允许但 m_stock 分发不认识的类型: {unroutable} "
            f"(会在运行时抛 UnsupportedOperationError)"
        )
