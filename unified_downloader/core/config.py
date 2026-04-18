"""配置管理"""

import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

import yaml

from unified_downloader.models.enums import Market


@dataclass
class DataSourceConfig:
    """数据源配置"""

    name: str
    base_url: str
    priority: int = 1
    timeout: int = 30
    retry_times: int = 3
    enabled: bool = True


@dataclass
class DownloadConfig:
    """下载配置"""

    download_dir: Path = Path("downloads")
    max_workers: int = 5
    chunk_size: int = 8192
    checkpoint_enabled: bool = True
    cache_enabled: bool = True
    cache_ttl_days: int = 30
    cache_max_size_gb: float = 10.0


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""

    failure_threshold: int = 5
    success_threshold: int = 3
    timeout_seconds: int = 30
    half_open_max_calls: int = 3


@dataclass
class Config:
    """全局配置"""

    # 数据源配置
    datasources: Dict[str, List[DataSourceConfig]] = field(default_factory=dict)

    # 下载配置
    download: DownloadConfig = field(default_factory=DownloadConfig)

    # 熔断器配置
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    # 审计配置
    audit_dir: Path = Path("data/audit")
    audit_enabled: bool = True

    # 缓存配置
    cache_dir: Path = Path("data/cache")

    # 断点续传配置
    checkpoint_dir: Path = Path("data/checkpoint")

    # SEC API配置
    sec_api_key: Optional[str] = None

    @classmethod
    def from_file(cls, path: Path | str) -> "Config":
        """从YAML文件加载配置"""
        path = Path(path)

        if not path.exists():
            return cls()

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        return cls.from_dict(data or {})

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """从字典加载配置"""
        # 解析数据源
        datasources = {}
        for market_str, ds_list in data.get("datasources", {}).items():
            datasources[market_str] = [
                DataSourceConfig(
                    name=ds.get("name", ""),
                    base_url=ds.get("base_url", ""),
                    priority=ds.get("priority", 1),
                    timeout=ds.get("timeout", 30),
                    retry_times=ds.get("retry_times", 3),
                    enabled=ds.get("enabled", True),
                )
                for ds in ds_list
            ]

        # 解析下载配置
        download_data = data.get("download", {})
        download = DownloadConfig(
            download_dir=Path(download_data.get("download_dir", "downloads")),
            max_workers=download_data.get("max_workers", 5),
            chunk_size=download_data.get("chunk_size", 8192),
            checkpoint_enabled=download_data.get("checkpoint_enabled", True),
            cache_enabled=download_data.get("cache_enabled", True),
            cache_ttl_days=download_data.get("cache_ttl_days", 30),
            cache_max_size_gb=download_data.get("cache_max_size_gb", 10.0),
        )

        # 解析熔断器配置
        cb_data = data.get("circuit_breaker", {})
        circuit_breaker = CircuitBreakerConfig(
            failure_threshold=cb_data.get("failure_threshold", 5),
            success_threshold=cb_data.get("success_threshold", 3),
            timeout_seconds=cb_data.get("timeout_seconds", 30),
            half_open_max_calls=cb_data.get("half_open_max_calls", 3),
        )

        return cls(
            datasources=datasources,
            download=download,
            circuit_breaker=circuit_breaker,
            audit_dir=Path(data.get("audit_dir", "data/audit")),
            audit_enabled=data.get("audit_enabled", True),
            cache_dir=Path(data.get("cache_dir", "data/cache")),
            checkpoint_dir=Path(data.get("checkpoint_dir", "data/checkpoint")),
        )

    @classmethod
    def from_env(cls) -> "Config":
        """从环境变量加载配置"""
        # 尝试从环境变量加载
        download_dir = os.environ.get("DOWNLOAD_DIR", "downloads")

        return cls(
            download=DownloadConfig(download_dir=Path(download_dir)),
        )

    def get_datasources(self, market: Market) -> List[Dict[str, Any]]:
        """获取指定市场的数据源配置"""
        market_key = market.value
        if market_key not in self.datasources:
            # 返回默认配置
            return self._get_default_datasources(market)
        return [
            {
                "name": ds.name,
                "base_url": ds.base_url,
                "priority": ds.priority,
                "timeout": ds.timeout,
                "retry_times": ds.retry_times,
                "enabled": ds.enabled,
            }
            for ds in self.datasources[market_key]
        ]

    def _get_default_datasources(self, market: Market) -> List[Dict[str, Any]]:
        """获取默认数据源配置"""
        if market == Market.A:
            return [
                {
                    "name": "cninfo",
                    "base_url": "http://www.cninfo.com.cn",
                    "priority": 1,
                },
            ]
        elif market == Market.M:
            return [
                {
                    "name": "sec_api",
                    "base_url": "https://api.sec-api.io",
                    "priority": 1,
                },
            ]
        elif market == Market.H:
            return [
                {"name": "hkex", "base_url": "https://www1.hkexnews.hk", "priority": 1},
            ]
        return []


# 默认配置实例
_default_config: Optional[Config] = None


def get_default_config() -> Config:
    """获取默认配置"""
    global _default_config
    if _default_config is None:
        _default_config = Config()
    return _default_config


def set_default_config(config: Config) -> None:
    """设置默认配置"""
    global _default_config
    _default_config = config
