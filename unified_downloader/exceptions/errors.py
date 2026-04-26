"""异常定义"""


class DownloadError(Exception):
    """下载异常基类"""

    def __init__(self, message: str, error_code: str = "DOWNLOAD_ERROR"):
        self.message = message
        self.error_code = error_code
        super().__init__(self.message)


class NetworkError(DownloadError):
    """网络错误"""

    def __init__(self, message: str):
        super().__init__(message, "NETWORK_ERROR")


class FileNotFoundDownloadError(DownloadError):
    """文件不存在"""

    def __init__(self, message: str):
        super().__init__(message, "FILE_NOT_FOUND")


class RateLimitError(DownloadError):
    """请求频率限制"""

    def __init__(self, message: str):
        super().__init__(message, "RATE_LIMIT")


class WebsiteStructureChangedError(DownloadError):
    """网站结构变更"""

    def __init__(self, message: str):
        super().__init__(message, "WEBSITE_STRUCTURE_CHANGED")


class MarketUnrecognizedError(DownloadError):
    """市场无法识别"""

    def __init__(self, code: str):
        message = f"无法识别股票代码: {code}，请检查代码格式"
        super().__init__(message, "MARKET_UNRECOGNIZED")
        self.code = code


class CircuitBreakerOpenError(DownloadError):
    """熔断器开启"""

    def __init__(self, market: str):
        message = f"市场 {market} 的熔断器已开启，请稍后再试"
        super().__init__(message, "CIRCUIT_BREAKER_OPEN")
        self.market = market


class ValidationError(DownloadError):
    """参数校验错误"""

    def __init__(self, message: str):
        super().__init__(message, "VALIDATION_ERROR")


class CacheError(DownloadError):
    """缓存错误"""

    def __init__(self, message: str):
        super().__init__(message, "CACHE_ERROR")


class CheckpointError(DownloadError):
    """断点续传错误"""

    def __init__(self, message: str):
        super().__init__(message, "CHECKPOINT_ERROR")


class AuthenticationError(DownloadError):
    """认证错误"""

    def __init__(self, message: str):
        super().__init__(message, "AUTH_ERROR")


class TimeoutError(DownloadError):
    """请求超时"""

    def __init__(self, message: str):
        super().__init__(message, "TIMEOUT")


class FileIntegrityError(DownloadError):
    """文件完整性校验失败"""

    def __init__(self, message: str):
        super().__init__(message, "FILE_INTEGRITY_ERROR")


class DataSourceError(DownloadError):
    """数据源错误"""

    def __init__(self, message: str, source: str):
        super().__init__(message, "DATA_SOURCE_ERROR")
        self.source = source


class UnsupportedOperationError(DownloadError):
    """不支持的操作"""

    def __init__(self, message: str):
        super().__init__(message, "UNSUPPORTED_OPERATION")
