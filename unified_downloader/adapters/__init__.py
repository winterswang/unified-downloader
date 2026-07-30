"""Adapters package"""

from unified_downloader.adapters.base import BaseStockAdapter, USFormResolver
from unified_downloader.adapters.a_stock import AStockAdapter
from unified_downloader.adapters.m_stock import MStockAdapter
from unified_downloader.adapters.h_stock import HStockAdapter
# W36: 欧股适配器 skeleton (PR #50a). 实际下载逻辑是 PR #50b.
from unified_downloader.adapters.eu_stock import EuStockAdapter

__all__ = [
    "BaseStockAdapter",
    "USFormResolver",
    "AStockAdapter",
    "MStockAdapter",
    "HStockAdapter",
    "EuStockAdapter",
]
