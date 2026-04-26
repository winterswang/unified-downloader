"""异步下载器"""

import asyncio
from typing import Optional, List, Dict, Any, Callable

from unified_downloader.models.enums import Market
from unified_downloader.models.entities import (
    DownloadResult,
    BatchResult,
    TaskInfo,
    TaskStatus,
)
from unified_downloader.models.callbacks import ProgressCallbackType
from unified_downloader.core.downloader import UnifiedDownloader
from unified_downloader.core.config import Config, get_default_config
from unified_downloader.infra import AsyncHTTPClient


class AsyncUnifiedDownloader:
    """
    异步统一下载器

    支持异步并发下载

    Example:
        >>> async def main():
        ...     downloader = AsyncUnifiedDownloader()
        ...     result = await downloader.download("600519", 2023)
        ...     print(f"文件保存至: {result.file_path}")
        ...
        >>> asyncio.run(main())
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._downloader = UnifiedDownloader(config)
        self._async_http_client = AsyncHTTPClient()

    async def download(
        self,
        code: str,
        year: Optional[int] = None,
        document_type: str = "annual_report",
        market: Optional[Market] = None,
        use_cache: bool = True,
        on_progress: Optional[ProgressCallbackType] = None,
        **kwargs,
    ) -> DownloadResult:
        """异步下载文档"""
        # 如果不使用缓存，直接调用适配器的异步方法
        if not use_cache or not self.config.download.cache_enabled:
            return await self._download_direct(
                code, year, document_type, market, on_progress, **kwargs
            )

        # 检查缓存
        if market is None:
            market = self._downloader._detect_market(code)

        cached_path = self._downloader._cache_manager.get(
            market.value, code, year, document_type
        )
        if cached_path:
            return DownloadResult(success=True, file_path=cached_path, cached=True)

        # 执行下载
        return await self._download_direct(
            code, year, document_type, market, on_progress, **kwargs
        )

    async def _download_direct(
        self,
        code: str,
        year: Optional[int],
        document_type: str,
        market: Optional[Market],
        on_progress: Optional[ProgressCallbackType],
        **kwargs,
    ) -> DownloadResult:
        """直接下载（不检查缓存）"""
        if market is None:
            market = self._downloader._detect_market(code)

        adapter = self._downloader._adapters.get(market)
        if not adapter:
            return DownloadResult(
                success=False,
                error_code="MARKET_UNRECOGNIZED",
                error_message="无法识别市场",
            )

        return await adapter.async_download(
            http_client=self._async_http_client,
            code=code,
            year=year,
            document_type=document_type,
            on_progress=on_progress,
            **kwargs,
        )

    async def batch_download(
        self,
        tasks: List[Dict[str, Any]],
        max_concurrency: int = 10,
        on_task_complete: Optional[Callable[[TaskInfo], None]] = None,
    ) -> BatchResult:
        """
        异步批量下载

        Args:
            tasks: 任务列表
            max_concurrency: 最大并发数
            on_task_complete: 任务完成回调

        Returns:
            BatchResult: 批量下载结果
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def process_task(task: Dict[str, Any]) -> DownloadResult:
            async with semaphore:
                code = task.get("code", "")
                year = task.get("year")
                doc_type = task.get("document_type", "annual_report")
                market = task.get("market")

                result = await self.download(
                    code=code,
                    year=year,
                    document_type=doc_type,
                    market=market,
                )

                if on_task_complete:
                    detected_market = market or self._downloader._detect_market(code)
                    task_id = self._downloader._generate_task_id(
                        detected_market, code, year, doc_type
                    )
                    task_info = TaskInfo(
                        task_id=task_id,
                        market=detected_market,
                        code=code,
                        year=year,
                        document_type=doc_type,
                        status=TaskStatus.COMPLETED
                        if result.success
                        else TaskStatus.FAILED,
                        result=result,
                    )
                    on_task_complete(task_info)

                return result

        results = await asyncio.gather(
            *[process_task(t) for t in tasks], return_exceptions=True
        )

        batch_results = []
        succeeded = 0
        failed = 0

        for r in results:
            if isinstance(r, Exception):
                batch_results.append(
                    DownloadResult(
                        success=False, error_code="TASK_ERROR", error_message=str(r)
                    )
                )
                failed += 1
            else:
                batch_results.append(r)
                if r.success:
                    succeeded += 1
                else:
                    failed += 1

        return BatchResult(
            total=len(tasks),
            succeeded=succeeded,
            failed=failed,
            results=batch_results,
        )

    async def close(self) -> None:
        """关闭资源"""
        await self._async_http_client.close()
        self._downloader.close()
