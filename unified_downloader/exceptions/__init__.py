"""Exceptions package"""

from unified_downloader.exceptions.errors import (
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
    AuthenticationError,
    TimeoutError,
    FileIntegrityError,
    DataSourceError,
    UnsupportedOperationError,
)

__all__ = [
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
    "AuthenticationError",
    "TimeoutError",
    "FileIntegrityError",
    "DataSourceError",
    "UnsupportedOperationError",
]
