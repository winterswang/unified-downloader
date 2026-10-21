"""
Unit tests for AStockAdapter download()/async_download() doc_type 分发.

Regression (2026-10-21 财报日清单事故, 与 m_stock W40-#50 同类):
- q1_report/q3_report 是 CLI -t / MCP document_type 允许的 A 股类型,
  但 a_stock 分发表里没有这两个名字, 静默落 else 被当年报下载
  (2026-10-20 实锤: 300750/000933 2026Q3 要三季报, 下到
  *_ANNUAL_REPORT.PDF, morning-brief verifier 判 SUSPICIOUS_WRONG_PERIOD,
  IMA 覆盖率 0%, 重试耗尽前才定位)。
- 未知类型之前静默按年报下载, 现在显式抛 UnsupportedOperationError
  (与 m_stock W40-#50 同一先例: 静默错误数据比显式失败更危险)。

这些测试只测分发路由, 不下载真实文件。
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.a_stock import AStockAdapter  # noqa: E402

# 异常类必须走 unified_downloader.exceptions 导入 (a_stock 内部同一路径),
# 否则 sys.path 双入口会产生两个模块实例, pytest.raises 接不住
from unified_downloader.exceptions import UnsupportedOperationError  # noqa: E402


@pytest.fixture
def adapter():
    """构造一个 AStockAdapter mock 桩 (只测分发, 不下载)"""
    return AStockAdapter(MagicMock(), [])


class TestSyncDispatch:
    """download() 的 doc_type → 内部方法路由"""

    @pytest.mark.parametrize("doc_type", ["q3_report", "q3", "三季报", "quarterly_q3"])
    def test_q3_report_routes_to_sanjibao(self, adapter, doc_type):
        """q3_report 家族必须路由到 _download_report(三季报), 绝不能落年报"""
        with (
            patch.object(adapter, "_download_report") as mock_rep,
            patch.object(adapter, "_download_annual_report") as mock_annual,
        ):
            mock_rep.return_value = "SENTINEL_Q3"
            result = adapter.download("000933", 2026, doc_type)

            assert result == "SENTINEL_Q3"
            mock_rep.assert_called_once()
            assert mock_rep.call_args.args[2] == "三季报"
            assert mock_rep.call_args.args[3] == "quarterly_report"
            (
                mock_annual.assert_not_called(),
                (
                    f"{doc_type} 之前不在分发里, 静默落 else 被当年报下载 "
                    f"(2026-10-21 财报日清单回归点)"
                ),
            )

    @pytest.mark.parametrize("doc_type", ["q1_report", "q1", "一季报", "quarterly_q1"])
    def test_q1_report_routes_to_yijibao(self, adapter, doc_type):
        """q1_report 家族路由到 _download_report(一季报), 同样不能落年报"""
        with (
            patch.object(adapter, "_download_report") as mock_rep,
            patch.object(adapter, "_download_annual_report") as mock_annual,
        ):
            mock_rep.return_value = "SENTINEL_Q1"
            result = adapter.download("000933", 2026, doc_type)

            assert result == "SENTINEL_Q1"
            mock_rep.assert_called_once()
            assert mock_rep.call_args.args[2] == "一季报"
            mock_annual.assert_not_called()

    @pytest.mark.parametrize("doc_type", ["annual_report", "年报", "年度报告"])
    def test_annual_family_routes_to_annual(self, adapter, doc_type):
        with patch.object(adapter, "_download_annual_report") as mock_annual:
            mock_annual.return_value = "SENTINEL_ANNUAL"
            result = adapter.download("600519", 2026, doc_type)

            assert result == "SENTINEL_ANNUAL"
            mock_annual.assert_called_once()

    @pytest.mark.parametrize("doc_type", ["interim_report", "中期报告", "半年报"])
    def test_interim_family_routes_to_interim(self, adapter, doc_type):
        with patch.object(adapter, "_download_interim_report") as mock_interim:
            mock_interim.return_value = "SENTINEL_INTERIM"
            result = adapter.download("600519", 2026, doc_type)

            assert result == "SENTINEL_INTERIM"
            mock_interim.assert_called_once()

    @pytest.mark.parametrize("doc_type", ["quarterly", "季度"])
    def test_ambiguous_quarterly_routes_to_quarterly(self, adapter, doc_type):
        """歧义类型 "quarterly" 保持先一季报后三季报的尝试链"""
        with patch.object(adapter, "_download_quarterly_report") as mock_q:
            mock_q.return_value = "SENTINEL_QUARTERLY"
            result = adapter.download("600519", 2026, doc_type)

            assert result == "SENTINEL_QUARTERLY"
            mock_q.assert_called_once()

    @pytest.mark.parametrize("doc_type", ["prospectus", "招股说明书", "招股书", "s1"])
    def test_prospectus_family_routes_to_prospectus(self, adapter, doc_type):
        with patch.object(adapter, "_download_prospectus") as mock_s1:
            mock_s1.return_value = "SENTINEL_S1"
            result = adapter.download("300750", None, doc_type)

            assert result == "SENTINEL_S1"
            mock_s1.assert_called_once()

    def test_unknown_type_raises(self, adapter):
        """未知类型显式报错, 不再静默按年报下载 (错误数据入库)"""
        with patch.object(adapter, "_download_annual_report") as mock_annual:
            with pytest.raises(UnsupportedOperationError):
                adapter.download("600519", 2026, "bogus_type")
            mock_annual.assert_not_called()


class TestAsyncDispatch:
    """async_download() 委托 sync download, 路由必须一致"""

    def test_q3_report_routes_via_async(self, adapter):
        with patch.object(adapter, "_download_report") as mock_rep:
            mock_rep.return_value = "SENTINEL_AQ3"
            result = asyncio.run(
                adapter.async_download(MagicMock(), "000933", 2026, "q3_report")
            )

            assert result == "SENTINEL_AQ3"
            mock_rep.assert_called_once()
            assert mock_rep.call_args.args[2] == "三季报"

    def test_unknown_type_raises_via_async(self, adapter):
        with pytest.raises(UnsupportedOperationError):
            asyncio.run(
                adapter.async_download(MagicMock(), "600519", 2026, "bogus_type")
            )


class TestDispatchConstants:
    """分发表常量与 CLI 允许集的一致性 (cli.py click.Choice 的 A 股类型)"""

    def test_all_cli_a_share_doc_types_are_routable(self):
        """cli.py -t 允许的每个 A 股类型都必须命中某条分发分支"""
        cli_a_share_types = {
            "annual_report",
            "interim_report",
            "quarterly",
            "q1_report",
            "q3_report",
            "prospectus",
        }
        routable = (
            AStockAdapter._ANNUAL_DOC_TYPES
            | AStockAdapter._INTERIM_DOC_TYPES
            | AStockAdapter._Q1_DOC_TYPES
            | AStockAdapter._Q3_DOC_TYPES
            | AStockAdapter._AMBIGUOUS_QUARTERLY_DOC_TYPES
            | AStockAdapter._PROSPECTUS_DOC_TYPES
        )
        unroutable = cli_a_share_types - routable
        assert not unroutable, (
            f"CLI 允许但 a_stock 分发不认识的类型: {unroutable} "
            f"(会在运行时抛 UnsupportedOperationError)"
        )

    def test_semantic_labels_cover_all_routable_types(self):
        """_SEMANTIC_LABELS 必须覆盖全部分发类型 (W40-#49 与分发一致)"""
        routable = (
            AStockAdapter._ANNUAL_DOC_TYPES
            | AStockAdapter._INTERIM_DOC_TYPES
            | AStockAdapter._Q1_DOC_TYPES
            | AStockAdapter._Q3_DOC_TYPES
            | AStockAdapter._AMBIGUOUS_QUARTERLY_DOC_TYPES
            | AStockAdapter._PROSPECTUS_DOC_TYPES
        )
        unlabeled = routable - set(AStockAdapter._SEMANTIC_LABELS)
        assert not unlabeled, (
            f"分发认识但 _SEMANTIC_LABELS 缺 label 的类型: {unlabeled}"
        )
