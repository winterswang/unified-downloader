"""枚举定义"""

from enum import Enum


class Market(str, Enum):
    """市场枚举"""

    A = "a"  # A股
    M = "m"  # 美股
    H = "h"  # 港股
    UNKNOWN = "unknown"


class DocumentType(str, Enum):
    """文档类型枚举"""

    ANNUAL_REPORT = "annual_report"  # 年度报告
    INTERIM_REPORT = "interim_report"  # 中期报告
    PROSPECTUS = "prospectus"  # 招股说明书
    TEN_K = "10k"  # 美股10-K年报
    S1 = "s1"  # 美股S-1招股书
    S1A = "s1a"  # 美股S-1A修正


class TaskStatus(str, Enum):
    """任务状态枚举"""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    """审计事件类型"""

    DOWNLOAD_START = "download_start"
    DOWNLOAD_COMPLETE = "download_complete"
    DOWNLOAD_FAILED = "download_failed"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    CIRCUIT_OPEN = "circuit_open"
    CIRCUIT_CLOSE = "circuit_close"
    RATE_LIMIT = "rate_limit"
    RETRY = "retry"
