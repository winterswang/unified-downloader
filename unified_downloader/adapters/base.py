"""适配器基类"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Callable
from pathlib import Path

from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult, DataSource
from unified_downloader.infra.http_client import HTTPClient, AsyncHTTPClient


class BaseStockAdapter(ABC):
    """
    股票适配器基类

    定义各市场适配器的通用接口
    """

    market: Market

    def __init__(
        self,
        http_client: HTTPClient,
        datasources: List[Dict[str, Any]],
    ):
        self._http_client = http_client
        self._datasources = [
            DataSource(
                name=ds.get("name", ""),
                base_url=ds.get("base_url", ""),
                priority=ds.get("priority", 1),
                timeout=ds.get("timeout", 30),
                retry_times=ds.get("retry_times", 3),
                enabled=ds.get("enabled", True),
            )
            for ds in datasources
        ]
        self._datasources.sort(key=lambda x: x.priority)

    @property
    def enabled_datasources(self) -> List[DataSource]:
        """获取启用的数据源列表"""
        return [ds for ds in self._datasources if ds.enabled]

    def _get_datasource(self, name: str) -> Optional[DataSource]:
        """根据名称获取数据源"""
        for ds in self._datasources:
            if ds.name == name:
                return ds
        return None

    def _build_file_path(
        self,
        code: str,
        year: Optional[int],
        doc_type: str,
        extension: str,
        market_prefix: Optional[str] = None,
    ) -> Path:
        """
        构建保存路径

        Args:
            code: 股票代码
            year: 年份
            doc_type: 文档类型
            extension: 文件扩展名
            market_prefix: 市场前缀

        Returns:
            保存路径
        """
        if market_prefix is None:
            market_prefix = self.market.value

        base_dir = Path("downloads") / market_prefix / code[:3]
        base_dir.mkdir(parents=True, exist_ok=True)

        if year:
            filename = f"{code}_{year}_{doc_type}{extension}"
        else:
            filename = f"{code}_{doc_type}{extension}"

        return base_dir / filename

    @abstractmethod
    def download(
        self,
        code: str,
        year: Optional[int],
        document_type: str,
        datasource: Optional[DataSource] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Callable] = None,
        **kwargs,
    ) -> DownloadResult:
        """
        下载文档

        Args:
            code: 股票代码
            year: 年份
            document_type: 文档类型
            datasource: 指定数据源，None表示按优先级自动选择
            checkpoint: 断点信息
            on_progress: 进度回调
            **kwargs: 其他参数

        Returns:
            下载结果
        """

    @abstractmethod
    async def async_download(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        year: Optional[int],
        document_type: str,
        datasource: Optional[DataSource] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Callable] = None,
        **kwargs,
    ) -> DownloadResult:
        """
        异步下载文档

        Args:
            http_client: 异步HTTP客户端
            code: 股票代码
            year: 年份
            document_type: 文档类型
            datasource: 指定数据源
            checkpoint: 断点信息
            on_progress: 进度回调
            **kwargs: 其他参数

        Returns:
            下载结果
        """

    def search(
        self,
        code: str,
        year: Optional[int] = None,
        document_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        搜索可用文档

        Args:
            code: 股票代码
            year: 年份
            document_type: 文档类型

        Returns:
            文档列表
        """
        raise NotImplementedError("search方法需由子类实现")

    def get_available_years(
        self,
        code: str,
        document_type: Optional[str] = None,
    ) -> List[int]:
        """
        获取可用年份

        Args:
            code: 股票代码
            document_type: 文档类型

        Returns:
            可用年份列表
        """
        raise NotImplementedError("get_available_years方法需由子类实现")
