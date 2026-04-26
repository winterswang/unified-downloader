"""统一下载器"""

import re
import time
from typing import Optional, List, Dict, Any, Callable
import concurrent.futures

from unified_downloader.models.enums import Market, DocumentType, EventType
from unified_downloader.models.entities import (
    DownloadResult,
    BatchResult,
    TaskInfo,
    TaskStatus,
)
from unified_downloader.models.callbacks import ProgressCallbackType

from unified_downloader.adapters import (
    AStockAdapter,
    MStockAdapter,
    HStockAdapter,
    BaseStockAdapter,
)
from unified_downloader.infra import (
    HTTPClient,
    CacheManager,
    CheckpointManager,
    CircuitBreakerManager,
    AuditLogger,
)
from unified_downloader.core.config import Config, get_default_config

from unified_downloader.exceptions import (
    MarketUnrecognizedError,
    CircuitBreakerOpenError,
)


class UnifiedDownloader:
    """
    统一下载器

    支持A股、美股、港股年报和IPO文档下载

    Example:
        >>> downloader = UnifiedDownloader()
        >>> result = downloader.download("600519", 2023)
        >>> print(f"文件保存至: {result.file_path}")

        >>> # 批量下载
        >>> tasks = [
        ...     {"code": "600519", "year": 2023},
        ...     {"code": "AAPL", "year": 2023},
        ... ]
        >>> batch_result = downloader.batch_download(tasks)
        >>> print(f"成功率: {batch_result.success_rate}")
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_default_config()

        # 初始化HTTP客户端
        self._http_client = HTTPClient()

        # 初始化适配器
        self._adapters: Dict[Market, BaseStockAdapter] = {
            Market.A: AStockAdapter(
                self._http_client, self.config.get_datasources(Market.A)
            ),
            Market.M: MStockAdapter(
                self._http_client, self.config.get_datasources(Market.M)
            ),
            Market.H: HStockAdapter(
                self._http_client, self.config.get_datasources(Market.H)
            ),
        }

        # 初始化基础设施
        self._cache_manager = CacheManager(
            cache_dir=self.config.cache_dir,
            ttl_days=self.config.download.cache_ttl_days,
            max_size_gb=self.config.download.cache_max_size_gb,
        )

        self._checkpoint_manager = CheckpointManager(
            checkpoint_dir=self.config.checkpoint_dir,
        )

        self._circuit_breaker_manager = CircuitBreakerManager()

        self._audit_logger = (
            AuditLogger(
                audit_dir=self.config.audit_dir,
            )
            if self.config.audit_enabled
            else None
        )

    def download(
        self,
        code: str,
        year: Optional[int] = None,
        document_type: str = "annual_report",
        market: Optional[Market] = None,
        use_cache: bool = True,
        on_progress: Optional[ProgressCallbackType] = None,
        **kwargs,
    ) -> DownloadResult:
        """
        下载文档

        Args:
            code: 股票代码或CIK编号
            year: 年份（可选，美股S-1招股说明书不需要）
            document_type: 文档类型 (annual_report, interim_report, prospectus, 10k, s1)
            market: 市场类型，None表示自动识别
            use_cache: 是否使用缓存
            on_progress: 进度回调函数
            **kwargs: 其他参数

        Returns:
            DownloadResult: 下载结果

        Raises:
            MarketUnrecognizedError: 无法识别股票代码所属市场
        """
        start_time = time.time()

        # 识别市场
        if market is None:
            market = self._detect_market(code)

        # 检查缓存
        if use_cache and self.config.download.cache_enabled:
            cached_path = self._cache_manager.get(
                market.value, code, year, document_type
            )
            if cached_path:
                duration_ms = int((time.time() - start_time) * 1000)
                self._log_event(
                    EventType.CACHE_HIT,
                    market,
                    code,
                    year,
                    document_type,
                    True,
                    duration_ms=duration_ms,
                )
                return DownloadResult(
                    success=True,
                    file_path=cached_path,
                    cached=True,
                    duration_ms=duration_ms,
                )

        # 生成任务ID
        task_id = self._generate_task_id(market, code, year, document_type)

        # 检查断点
        checkpoint = None
        if self.config.download.checkpoint_enabled:
            checkpoint = self._checkpoint_manager.resume(task_id, f"{market}:{code}")

        # 检查熔断器
        breaker = self._circuit_breaker_manager.get_breaker(market.value)
        if breaker.is_open:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_event(
                EventType.CIRCUIT_OPEN,
                market,
                code,
                year,
                document_type,
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

        # 获取适配器
        adapter = self._adapters.get(market)
        if not adapter:
            raise MarketUnrecognizedError(code)

        # 执行下载
        self._log_event(
            EventType.DOWNLOAD_START, market, code, year, document_type, True
        )

        try:
            result = adapter.download(
                code=code,
                year=year,
                document_type=document_type,
                checkpoint=checkpoint,
                on_progress=on_progress,
                **kwargs,
            )

            duration_ms = int((time.time() - start_time) * 1000)
            result.duration_ms = duration_ms

            # 更新熔断器状态
            if result.success:
                breaker.record_success()
                self._log_event(
                    EventType.DOWNLOAD_COMPLETE,
                    market,
                    code,
                    year,
                    document_type,
                    True,
                    duration_ms=duration_ms,
                    file_size=result.file_size,
                    source=result.source,
                )

                # 添加到缓存
                if (
                    use_cache
                    and self.config.download.cache_enabled
                    and result.file_path
                ):
                    self._cache_manager.put(
                        market.value,
                        code,
                        year,
                        document_type,
                        file_path=result.file_path,
                    )

                # 清理断点
                if self.config.download.checkpoint_enabled:
                    self._checkpoint_manager.delete(task_id)
            else:
                breaker.record_failure()
                self._log_event(
                    EventType.DOWNLOAD_FAILED,
                    market,
                    code,
                    year,
                    document_type,
                    False,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    duration_ms=duration_ms,
                )

            return result

        except CircuitBreakerOpenError as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_event(
                EventType.CIRCUIT_OPEN,
                market,
                code,
                year,
                document_type,
                False,
                error_code="CIRCUIT_BREAKER_OPEN",
                duration_ms=duration_ms,
            )
            return DownloadResult(
                success=False,
                error_code="CIRCUIT_BREAKER_OPEN",
                error_message=str(e),
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            breaker.record_failure()
            self._log_event(
                EventType.DOWNLOAD_FAILED,
                market,
                code,
                year,
                document_type,
                False,
                error_code="UNEXPECTED_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            return DownloadResult(
                success=False,
                error_code="UNEXPECTED_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )

    def batch_download(
        self,
        tasks: List[Dict[str, Any]],
        max_workers: Optional[int] = None,
        on_task_complete: Optional[Callable[[TaskInfo], None]] = None,
        on_task_progress: Optional[
            Callable[[TaskInfo, ProgressCallbackType], None]
        ] = None,
    ) -> BatchResult:
        """
        批量下载

        Args:
            tasks: 任务列表，每个任务包含:
                   - code: 股票代码
                   - year: 年份（可选）
                   - document_type: 文档类型（可选，默认annual_report）
                   - market: 市场类型（可选，自动识别）
            max_workers: 最大并发数
            on_task_complete: 任务完成回调
            on_task_progress: 任务进度回调

        Returns:
            BatchResult: 批量下载结果
        """
        start_time = time.time()
        max_workers = max_workers or self.config.download.max_workers

        results: List[DownloadResult] = []
        succeeded = 0
        failed = 0

        def process_task(task: Dict[str, Any]) -> DownloadResult:
            """处理单个任务"""
            code = task.get("code", "")
            year = task.get("year")
            doc_type = task.get("document_type", "annual_report")
            market = task.get("market")

            def progress_callback(progress: Dict[str, Any]):
                if on_task_progress:
                    task_info = TaskInfo(
                        task_id=self._generate_task_id(
                            market or self._detect_market(code), code, year, doc_type
                        ),
                        market=market or self._detect_market(code),
                        code=code,
                        year=year,
                        document_type=DocumentType(doc_type)
                        if isinstance(doc_type, str)
                        else doc_type,
                        status=TaskStatus.DOWNLOADING,
                        progress=progress.get("downloaded", 0)
                        / max(progress.get("total", 1), 1),
                    )
                    on_task_progress(task_info, progress)

            result = self.download(
                code=code,
                year=year,
                document_type=doc_type,
                market=market,
                on_progress=progress_callback,
            )

            if on_task_complete:
                task_info = TaskInfo(
                    task_id=self._generate_task_id(
                        market or self._detect_market(code), code, year, doc_type
                    ),
                    market=market or self._detect_market(code),
                    code=code,
                    year=year,
                    document_type=DocumentType(doc_type)
                    if isinstance(doc_type, str)
                    else doc_type,
                    status=TaskStatus.COMPLETED
                    if result.success
                    else TaskStatus.FAILED,
                    result=result,
                )
                on_task_complete(task_info)

            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_task, task) for task in tasks]

            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                    if result.success:
                        succeeded += 1
                    else:
                        failed += 1
                except Exception as e:
                    results.append(
                        DownloadResult(
                            success=False,
                            error_code="TASK_ERROR",
                            error_message=str(e),
                        )
                    )
                    failed += 1

        duration_ms = int((time.time() - start_time) * 1000)

        return BatchResult(
            total=len(tasks),
            succeeded=succeeded,
            failed=failed,
            results=results,
            duration_ms=duration_ms,
        )

    def _detect_market(self, code: str) -> Market:
        """
        自动识别股票代码所属市场

        Args:
            code: 股票代码

        Returns:
            Market: 市场枚举

        Raises:
            MarketUnrecognizedError: 无法识别时抛出
        """
        code = code.strip()

        # A股: 6位数字
        if re.match(r"^[0-9]{6}$", code):
            return Market.A

        # 港股: 5位数字且0开头 (如 00700, 09988)
        if re.match(r"^0[0-9]{4}$", code):
            return Market.H

        # 美股: 英文字母代码
        if re.match(r"^[A-Za-z]{1,5}$", code):
            return Market.M

        # 美股CIK: 10位数字
        if re.match(r"^[0-9]{10}$", code):
            return Market.M

        raise MarketUnrecognizedError(code)

    def _generate_task_id(
        self,
        market: Market,
        code: str,
        year: Optional[int],
        document_type: str,
    ) -> str:
        """生成唯一任务ID"""
        parts = [market.value, code, document_type]
        if year:
            parts.append(str(year))
        return "_".join(parts)

    def _log_event(
        self,
        event_type: EventType,
        market: Market,
        code: str,
        year: Optional[int],
        document_type: str,
        success: bool,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: Optional[int] = None,
        file_size: Optional[int] = None,
        source: Optional[str] = None,
    ) -> None:
        """记录审计日志"""
        if self._audit_logger:
            try:
                self._audit_logger.log(
                    event_type=event_type,
                    success=success,
                    market=market.value,
                    code=code,
                    year=year,
                    document_type=document_type,
                    error_code=error_code,
                    error_message=error_message,
                    duration_ms=duration_ms,
                    file_size=file_size,
                    source=source,
                )
            except Exception:
                pass  # 审计日志失败不影响主流程

    def get_circuit_status(self) -> Dict[str, Any]:
        """获取熔断器状态"""
        return self._circuit_breaker_manager.get_all_status()

    def reset_circuit_breakers(self) -> None:
        """重置所有熔断器"""
        self._circuit_breaker_manager.reset_all()

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        return self._cache_manager.get_stats()

    def clear_cache(self, older_than_days: Optional[int] = None) -> int:
        """清理缓存"""
        return self._cache_manager.clear(older_than_days=older_than_days)

    def close(self) -> None:
        """关闭资源"""
        self._http_client.close()
        self._cache_manager.close()
        if self._audit_logger:
            self._audit_logger.close()
