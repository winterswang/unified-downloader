"""
统一年报及IPO文件下载工具

支持A股、美股、港股三个市场的年报和IPO文档下载
"""

from unified_downloader.models import (
    Market,
    DocumentType,
    TaskStatus,
    DownloadResult,
    BatchResult,
    TaskInfo,
    DataSource,
    ProgressCallbackType,
)
from unified_downloader.core import (
    Config,
    get_default_config,
    set_default_config,
    UnifiedDownloader,
    AsyncUnifiedDownloader,
)
from unified_downloader.exceptions import (
    DownloadError,
    NetworkError,
    FileNotFoundDownloadError,
    RateLimitError,
    WebsiteStructureChangedError,
    MarketUnrecognizedError,
    CircuitBreakerOpenError,
    ValidationError,
    CacheError,
    CheckpointError,
)

__version__ = "1.0.0"

__all__ = [
    # Models
    "Market",
    "DocumentType",
    "TaskStatus",
    "DownloadResult",
    "BatchResult",
    "TaskInfo",
    "DataSource",
    "ProgressCallbackType",
    # Core
    "Config",
    "get_default_config",
    "set_default_config",
    "UnifiedDownloader",
    "AsyncUnifiedDownloader",
    # Exceptions
    "DownloadError",
    "NetworkError",
    "FileNotFoundDownloadError",
    "RateLimitError",
    "WebsiteStructureChangedError",
    "MarketUnrecognizedError",
    "CircuitBreakerOpenError",
    "ValidationError",
    "CacheError",
    "CheckpointError",
]
