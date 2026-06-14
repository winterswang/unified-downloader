"""Adapters package"""

from unified_downloader.adapters.base import BaseStockAdapter, USFormResolver
from unified_downloader.adapters.a_stock import AStockAdapter
from unified_downloader.adapters.m_stock import MStockAdapter
from unified_downloader.adapters.h_stock import HStockAdapter

__all__ = [
    "BaseStockAdapter",
    "USFormResolver",
    "AStockAdapter",
    "MStockAdapter",
    "HStockAdapter",
]
