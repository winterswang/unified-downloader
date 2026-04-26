"""速率限制器"""

import time
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class RateLimiter:
    """简单速率限制器，控制请求频率"""

    def __init__(self, min_interval: float = 1.0):
        """
        Args:
            min_interval: 最小请求间隔（秒），默认1秒
        """
        self.min_interval = min_interval
        self._last_request_time: Dict[str, float] = {}

    def wait(self, key: str = "default") -> None:
        """等待直到可以发送下一个请求"""
        now = time.time()
        last_time = self._last_request_time.get(key, 0)
        elapsed = now - last_time

        if elapsed < self.min_interval:
            wait_time = self.min_interval - elapsed
            logger.debug(f"Rate limiter: waiting {wait_time:.2f}s for {key}")
            time.sleep(wait_time)

        self._last_request_time[key] = time.time()

    def reset(self, key: Optional[str] = None) -> None:
        """重置速率限制器"""
        if key:
            self._last_request_time.pop(key, None)
        else:
            self._last_request_time.clear()
