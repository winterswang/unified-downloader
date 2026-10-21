"""
Unit tests for HStockAdapter download() doc_type 分发.

Regression (2026-10-22, 与 a_stock PR #72 / m_stock W40-#50 同类):
- q1_report/q3_report 是 CLI -t 与 MCP document_type 允许的类型, 但
  h_stock 分发表里没有, 静默落 else 被当年报下载。morning-brief 对港股
  目前只发 quarterly, 尚未踩到; 属同类隐患提前堵掉。
- 港交所「季度业绩」(QUARTERLY_RESULTS) 不区分 Q1/Q3, 这些名字统一
  路由到 _download_quarterly_report。
- 未知类型之前静默按年报下载, 现在显式抛 UnsupportedOperationError。

这些测试只测分发路由, 不下载真实文件。
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.h_stock import HStockAdapter  # noqa: E402

# 异常类必须走 unified_downloader.exceptions 导入 (h_stock 内部同一路径),
# 否则 sys.path 双入口会产生两个模块实例, pytest.raises 接不住
from unified_downloader.exceptions import UnsupportedOperationError  # noqa: E402


@pytest.fixture
def adapter():
    """构造一个 HStockAdapter mock 桩 (只测分发, 不下载)"""
    return HStockAdapter(MagicMock(), [])


class TestSyncDispatch:
    """download() 的 doc_type → 内部方法路由"""

    @pytest.mark.parametrize(
        "doc_type",
        [
            "q3_report",
            "q1_report",
            "quarterly",
            "quarterly_q1",
            "quarterly_q3",
            "q1",
            "q3",
            "季度",
        ],
    )
    def test_quarterly_family_routes_to_quarterly(self, adapter, doc_type):
        """季报家族必须路由到 _download_quarterly_report, 绝不能落年报"""
        with (
            patch.object(adapter, "_download_quarterly_report") as mock_q,
            patch.object(adapter, "_download_annual_report") as mock_annual,
        ):
            mock_q.return_value = "SENTINEL_Q"
            result = adapter.download("00700", 2026, doc_type)

            assert result == "SENTINEL_Q"
            mock_q.assert_called_once()
            (
                mock_annual.assert_not_called(),
                (
                    f"{doc_type} 之前不在分发里, 静默落 else 被当年报下载 "
                    f"(与 a_stock PR #72 同类回归点)"
                ),
            )

    @pytest.mark.parametrize(
        "doc_type", ["annual_report", "年报", "年度报告", "annual report"]
    )
    def test_annual_family_routes_to_annual(self, adapter, doc_type):
        with patch.object(adapter, "_download_annual_report") as mock_annual:
            mock_annual.return_value = "SENTINEL_ANNUAL"
            result = adapter.download("00700", 2026, doc_type)

            assert result == "SENTINEL_ANNUAL"
            mock_annual.assert_called_once()

    @pytest.mark.parametrize(
        "doc_type", ["interim_report", "中期报告", "中期", "半年报"]
    )
    def test_interim_family_routes_to_interim(self, adapter, doc_type):
        with patch.object(adapter, "_download_interim_report") as mock_interim:
            mock_interim.return_value = "SENTINEL_INTERIM"
            result = adapter.download("00700", 2026, doc_type)

            assert result == "SENTINEL_INTERIM"
            mock_interim.assert_called_once()

    @pytest.mark.parametrize("doc_type", ["prospectus", "招股说明书", "招股书", "s1"])
    def test_prospectus_family_routes_to_prospectus(self, adapter, doc_type):
        with patch.object(adapter, "_download_prospectus") as mock_p:
            mock_p.return_value = "SENTINEL_S1"
            result = adapter.download("00700", None, doc_type)

            assert result == "SENTINEL_S1"
            mock_p.assert_called_once()

    def test_unknown_type_raises(self, adapter):
        """未知类型显式报错, 不再静默按年报下载 (错误数据入库)"""
        with patch.object(adapter, "_download_annual_report") as mock_annual:
            with pytest.raises(UnsupportedOperationError):
                adapter.download("00700", 2026, "bogus_type")
            mock_annual.assert_not_called()


class TestAsyncDispatch:
    """async_download() 委托 sync download, 路由必须一致"""

    def test_q3_report_routes_via_async(self, adapter):
        with patch.object(adapter, "_download_quarterly_report") as mock_q:
            mock_q.return_value = "SENTINEL_AQ"
            result = asyncio.run(
                adapter.async_download(MagicMock(), "00700", 2026, "q3_report")
            )

            assert result == "SENTINEL_AQ"
            mock_q.assert_called_once()

    def test_unknown_type_raises_via_async(self, adapter):
        with pytest.raises(UnsupportedOperationError):
            asyncio.run(
                adapter.async_download(MagicMock(), "00700", 2026, "bogus_type")
            )


class TestDispatchConstants:
    """分发表常量与 CLI 允许集的一致性 (cli.py click.Choice 的港股类型)"""

    def test_all_cli_hk_doc_types_are_routable(self):
        """cli.py -t 允许的每个港股类型都必须命中某条分发分支"""
        cli_hk_types = {
            "annual_report",
            "interim_report",
            "quarterly",
            "q1_report",
            "q3_report",
            "prospectus",
        }
        routable = (
            HStockAdapter._ANNUAL_DOC_TYPES
            | HStockAdapter._INTERIM_DOC_TYPES
            | HStockAdapter._QUARTERLY_DOC_TYPES
            | HStockAdapter._PROSPECTUS_DOC_TYPES
        )
        unroutable = cli_hk_types - routable
        assert not unroutable, (
            f"CLI 允许但 h_stock 分发不认识的类型: {unroutable} "
            f"(会在运行时抛 UnsupportedOperationError)"
        )

    def test_semantic_labels_cover_all_routable_types(self):
        """_SEMANTIC_LABELS 必须覆盖全部分发类型 (W40-#49 与分发一致)"""
        routable = (
            HStockAdapter._ANNUAL_DOC_TYPES
            | HStockAdapter._INTERIM_DOC_TYPES
            | HStockAdapter._QUARTERLY_DOC_TYPES
            | HStockAdapter._PROSPECTUS_DOC_TYPES
        )
        unlabeled = routable - set(HStockAdapter._SEMANTIC_LABELS)
        assert not unlabeled, (
            f"分发认识但 _SEMANTIC_LABELS 缺 label 的类型: {unlabeled}"
        )
