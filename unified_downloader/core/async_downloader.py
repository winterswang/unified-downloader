"""异步下载器"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from unified_downloader.models.enums import Market, EventType
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

logger = logging.getLogger(__name__)


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
        # W40-#50 P1: 之前 self.config 用裸 Config() (不读 config.yaml),
        # 而 UnifiedDownloader(config=None) 内部又用 get_default_config() —
        # 两个不同配置实例, cache_enabled 等检查项与实际行为不一致。
        # 统一为同一个 config (无参时读默认配置)。
        self.config = config or get_default_config()
        self._downloader = UnifiedDownloader(self.config)
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
                code, year, document_type, market, on_progress,
                use_cache=use_cache, **kwargs
            )

        # 检查缓存
        if market is None:
            market = self._downloader._detect_market(code)

        hit = self._downloader._cache_manager.get(
            market.value, code, year, document_type
        )
        if hit:
            # W40-#49: 与同步路径一致 — 语义路径直接返回; 老条目走还原。
            # 另补齐 V4: 要 PDF 而缓存是 HTML 时不短路缓存 (sync 已有,
            # async 之前缺失, 行为分叉)
            restored_path = hit.path
            if restored_path == hit.hash_path:
                restored_path = self._downloader._restore_semantic_cache_path(
                    market, code, year, document_type, hit.hash_path
                )
            wants_pdf = kwargs.get("convert_to_pdf") is True
            if not (
                market == Market.M
                and wants_pdf
                and Path(restored_path).suffix.lower() in (".html", ".htm")
            ):
                return DownloadResult(
                    success=True,
                    file_path=restored_path,
                    cached=True,
                    metadata={"cache_path": hit.hash_path},
                )
            # 显式要 PDF 但缓存是 HTML → 落到直接下载

        # 执行下载
        return await self._download_direct(
            code, year, document_type, market, on_progress,
            use_cache=use_cache, **kwargs
        )

    async def _download_direct(
        self,
        code: str,
        year: Optional[int],
        document_type: str,
        market: Optional[Market],
        on_progress: Optional[ProgressCallbackType],
        use_cache: bool = True,
        **kwargs,
    ) -> DownloadResult:
        """直接下载（不检查缓存）"""
        start_time = time.time()

        if market is None:
            market = self._downloader._detect_market(code)

        adapter = self._downloader._adapters.get(market)
        if not adapter:
            return DownloadResult(
                success=False,
                error_code="MARKET_UNRECOGNIZED",
                error_message="无法识别市场",
            )

        # W40-#50 P1: 之前异步路径完全不碰熔断器 (批量跑时熔断永不打开,
        # 故障源被无限打) 也不写审计日志。与同步路径对齐。
        breaker = self._downloader._circuit_breaker_manager.get_breaker(
            market.value
        )
        if not breaker.can_execute():
            duration_ms = int((time.time() - start_time) * 1000)
            self._downloader._log_event(
                EventType.CIRCUIT_OPEN,
                market, code, year, document_type,
                False,
                error_code="CIRCUIT_BREAKER_OPEN",
                duration_ms=duration_ms,
            )
            return DownloadResult(
                success=False,
                error_code="CIRCUIT_BREAKER_OPEN",
                error_message=f"市场 {market.value} 的熔断器已开启，请稍后再试",
                duration_ms=duration_ms,
            )

        self._downloader._log_event(
            EventType.DOWNLOAD_START, market, code, year, document_type, True
        )

        try:
            result = await adapter.async_download(
                http_client=self._async_http_client,
                code=code,
                year=year,
                document_type=document_type,
                on_progress=on_progress,
                **kwargs,
            )
        except Exception as e:
            # W40-#50: 之前 eu_stock 等骨架的 NotImplementedError 直接抛给
            # 调用方 (sync 路径却兜成失败结果), 单发/批量两种表现不一致
            duration_ms = int((time.time() - start_time) * 1000)
            breaker.record_failure()
            self._downloader._log_event(
                EventType.DOWNLOAD_FAILED,
                market, code, year, document_type,
                False,
                error_code="DOWNLOAD_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            return DownloadResult(
                success=False,
                error_code="DOWNLOAD_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )

        duration_ms = int((time.time() - start_time) * 1000)
        result.duration_ms = duration_ms

        if result.success:
            breaker.record_success()
            self._downloader._log_event(
                EventType.DOWNLOAD_COMPLETE,
                market, code, year, document_type,
                True,
                duration_ms=duration_ms,
                file_size=result.file_size,
                source=result.source,
            )
            # W40-#50 P1: 之前异步下载的成果永远进不了缓存 (无任何 put
            # 调用, 缓存只能命中同步路径写入的条目)
            if (
                use_cache
                and self.config.download.cache_enabled
                and result.file_path
            ):
                try:
                    self._downloader._cache_manager.put(
                        market.value,
                        code,
                        year,
                        document_type,
                        file_path=result.file_path,
                    )
                except Exception as cache_err:
                    logger.warning(
                        f"Cache write failed (download itself succeeded): {cache_err}"
                    )
        else:
            breaker.record_failure()
            self._downloader._log_event(
                EventType.DOWNLOAD_FAILED,
                market, code, year, document_type,
                False,
                error_code=result.error_code,
                error_message=result.error_message,
                duration_ms=duration_ms,
            )

        return result

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
