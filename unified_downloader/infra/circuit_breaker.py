"""熔断器实现"""

import threading
from enum import Enum
from typing import Optional, Callable, Any, Dict
from dataclasses import dataclass
from datetime import datetime

from unified_downloader.exceptions import CircuitBreakerOpenError


class CircuitState(Enum):
    """熔断器状态"""

    CLOSED = "closed"  # 正常状态
    OPEN = "open"  # 熔断状态
    HALF_OPEN = "half_open"  # 半开状态


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""

    failure_threshold: int = 5  # 失败次数阈值
    success_threshold: int = 3  # 半开后成功次数阈值
    timeout: int = 30  # 熔断持续时间（秒）
    half_open_max_calls: int = 3  # 半开状态最大尝试次数


class CircuitBreaker:
    """
    熔断器实现

    基于状态机模式，防止系统过载
    """

    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        # W40-#50 P1: 之前全文件无锁 — batch_download 的线程池并发调
        # record_failure (_failure_count += 1 非原子) / record_success
        # (= 0 直接清零) 互相覆盖, 失败计数可能永远到不了阈值;
        # state property 在读操作里做状态迁移同样有竞态。RLock 允许
        # property 与 mutating method 嵌套。
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._last_state_change = datetime.now()
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        """获取当前状态"""
        with self._lock:
            if self._state == CircuitState.OPEN:
                # 检查是否应该转换到半开状态
                if self._last_failure_time:
                    elapsed = (datetime.now() - self._last_failure_time).total_seconds()
                    if elapsed >= self.config.timeout:
                        self._transition_to(CircuitState.HALF_OPEN)
            return self._state

    @property
    def is_closed(self) -> bool:
        """是否关闭"""
        return self.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """是否开启"""
        return self.state == CircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        """是否半开"""
        return self.state == CircuitState.HALF_OPEN

    def record_success(self) -> None:
        """记录成功调用"""
        with self._lock:
            self._record_success_unlocked()

    def _record_success_unlocked(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.config.success_threshold:
                self._transition_to(CircuitState.CLOSED)
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """记录失败调用"""
        with self._lock:
            self._record_failure_unlocked()

    def _record_failure_unlocked(self) -> None:
        self._failure_count += 1
        self._last_failure_time = datetime.now()

        if self._state == CircuitState.HALF_OPEN:
            # 半开状态下失败，立即开启
            self._transition_to(CircuitState.OPEN)
        elif self._state == CircuitState.CLOSED:
            if self._failure_count >= self.config.failure_threshold:
                self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """状态转换"""
        self._state = new_state
        self._last_state_change = datetime.now()

        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.OPEN:
            self._half_open_calls = 0
        elif new_state == CircuitState.HALF_OPEN:
            # Reset failure count when entering half-open to start fresh
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0

    def can_execute(self) -> bool:
        """检查是否可以执行"""
        with self._lock:
            return self._can_execute_unlocked()

    def _can_execute_unlocked(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.HALF_OPEN:
            if self._half_open_calls < self.config.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

        return False

    def execute(self, func: Callable, *args, **kwargs) -> Any:
        """
        执行函数，带熔断保护

        Args:
            func: 要执行的函数
            *args, **kwargs: 函数参数

        Returns:
            函数返回值

        Raises:
            CircuitBreakerOpenError: 熔断器开启时抛出
        """
        if not self.can_execute():
            raise CircuitBreakerOpenError(self.name)

        try:
            result = func(*args, **kwargs)
            with self._lock:
                self._record_success_unlocked()
            return result
        except Exception:
            with self._lock:
                self._record_failure_unlocked()
            raise

    def get_status(self) -> Dict[str, Any]:
        """获取熔断器状态"""
        with self._lock:
            return self._get_status_unlocked()

    def _get_status_unlocked(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure_time": self._last_failure_time.isoformat()
            if self._last_failure_time
            else None,
            "last_state_change": self._last_state_change.isoformat(),
        }

    def reset(self) -> None:
        """重置熔断器"""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)


class CircuitBreakerManager:
    """
    熔断器管理器

    管理多个市场的熔断器
    """

    def __init__(self, default_config: Optional[CircuitBreakerConfig] = None):
        self._breakers: Dict[str, CircuitBreaker] = {}
        # W40-#50 P1: 之前 Config.from_dict 精心解析的 circuit_breaker
        # 配置因 manager/get_breaker 都不传参被静默丢弃 (YAML 设
        # failure_threshold: 10 实际仍是默认 5)
        self._default_config = default_config

    def get_breaker(
        self, market: str, config: Optional[CircuitBreakerConfig] = None
    ) -> CircuitBreaker:
        """获取或创建熔断器"""
        if market not in self._breakers:
            self._breakers[market] = CircuitBreaker(
                market, config or self._default_config
            )
        return self._breakers[market]

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有熔断器状态"""
        return {name: breaker.get_status() for name, breaker in self._breakers.items()}

    def reset_all(self) -> None:
        """重置所有熔断器"""
        for breaker in self._breakers.values():
            breaker.reset()
