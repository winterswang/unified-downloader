"""枚举定义"""

from enum import Enum


class Market(str, Enum):
    """市场枚举"""

    A = "a"  # A股
    M = "m"  # 美股
    H = "h"  # 港股
    E = "e"  # W36: 欧股 (Euronext Paris/Amsterdam/Milan + Frankfurt)
    UNKNOWN = "unknown"


class DocumentType(str, Enum):
    """文档类型枚举"""

    ANNUAL_REPORT = "annual_report"  # 年度报告
    INTERIM_REPORT = "interim_report"  # 中期报告
    QUARTERLY = "quarterly"  # 季度报告（一季报/三季报）
    Q1_REPORT = "q1_report"  # 一季报
    Q3_REPORT = "q3_report"  # 三季报
    PROSPECTUS = "prospectus"  # 招股说明书
    TEN_K = "10k"  # 美股10-K年报
    TEN_Q = "10q"  # 美股10-Q季报
    S1 = "s1"  # 美股S-1招股书
    S1A = "s1a"  # 美股S-1A修正
    F1 = "f1"  # 美股F-1外国公司招股书
    F424B4 = "424b4"  # 美股424B4最终招股书（定价版）
    SIX_K = "6k"  # 美股外国公司6-K临时报告
    EIGHT_K = "8k"  # 美股8-K重大事件报告
    TWENTY_F = "20f"  # 美股外国公司20-F年报


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
