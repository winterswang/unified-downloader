"""美股适配器 - 使用 edgartools (主) + sec-api (兜底)"""

import logging
import os
import time
from typing import Optional, List, Dict, Any, Callable

from unified_downloader.adapters.base import BaseStockAdapter
from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult, DataSource
from unified_downloader.infra.http_client import HTTPClient, AsyncHTTPClient
from unified_downloader.exceptions import (
    NetworkError,
)

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


# 全局速率限制器
_rate_limiter = RateLimiter(min_interval=1.0)


class MStockAdapter(BaseStockAdapter):
    """
    美股下载适配器

    优先使用 edgartools (免费开源)，失败时回退到 sec-api (付费)
    支持速率限制和重试逻辑
    """

    market = Market.M

    # 重试配置
    MAX_RETRIES = 3
    RETRY_BACKOFF = 2.0  # 秒

    def __init__(
        self,
        http_client: HTTPClient,
        datasources: List[Dict],
        api_key: Optional[str] = None,
        rate_limit_interval: float = 5.0,
    ):
        super().__init__(http_client, datasources)
        self._api_key = api_key or self._get_api_key()
        self._ticker_cache: Dict[str, str] = {}
        self._edgar_identity: Optional[str] = None
        self._rate_limiter = RateLimiter(min_interval=rate_limit_interval)

    def _get_api_key(self) -> str:
        """获取SEC API Key"""
        api_key = os.environ.get("SEC_API_KEY", "")
        if not api_key:
            from unified_downloader.core.config import get_default_config

            cfg = get_default_config()
            api_key = getattr(cfg, "sec_api_key", "") or os.environ.get(
                "SEC_API_KEY", ""
            )
        return api_key

    def _get_edgar_identity(self) -> str:
        """获取EDGAR Identity (邮箱)"""
        if self._edgar_identity:
            return self._edgar_identity

        identity = os.environ.get(
            "EDGAR_IDENTITY", "UnifiedDownloader unified-downloader@example.com"
        )
        self._edgar_identity = identity
        return identity

    def _init_edgar(self) -> bool:
        """初始化edgartools"""
        try:
            from edgar import set_identity

            identity = self._get_edgar_identity()
            set_identity(identity)
            return True
        except Exception as e:
            logger.warning(f"edgartools初始化失败: {e}")
            return False

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
        """下载美股文档"""
        doc_type_lower = document_type.lower()

        if doc_type_lower in ["10k", "ten_k"]:
            return self._download_10k(code, year, datasource, checkpoint, on_progress)
        elif doc_type_lower in ["10q", "ten_q"]:
            return self._download_10q(code, year, datasource, checkpoint, on_progress)
        elif doc_type_lower in ["s1", "s1a", "prospectus"]:
            return self._download_s1(
                code, document_type, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["6k", "8k"]:
            return self._download_6k(code, year, datasource, checkpoint, on_progress)
        else:
            return self._download_10k(code, year, datasource, checkpoint, on_progress)

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
        """异步下载美股文档"""
        doc_type_lower = document_type.lower()

        if doc_type_lower in ["10k", "ten_k"]:
            return await self._async_download_10k(
                http_client, code, year, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["10q", "ten_q"]:
            return await self._async_download_10q(
                http_client, code, year, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["s1", "s1a", "prospectus"]:
            return await self._async_download_s1(
                http_client, code, document_type, datasource, checkpoint, on_progress
            )
        else:
            return await self._async_download_10k(
                http_client, code, year, datasource, checkpoint, on_progress
            )

    def _search_edgar(
        self,
        ticker: str,
        form_type: str,
        year: Optional[int],
        size: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        使用edgartools搜索SEC filings

        Returns:
            filings列表，每项包含标准化字段
        """
        from datetime import date
        from edgar import Company

        try:
            company = Company(ticker.upper())
            edgar_filings = company.get_filings(form=form_type)

            results = []
            for filing in edgar_filings:
                # 过滤年份 (filing_date是date对象)
                if year:
                    try:
                        filing_year = filing.filing_date.year
                        if filing_year != year:
                            continue
                    except (ValueError, IndexError):
                        pass

                # 标准化为dict格式
                filed_at = (
                    filing.filing_date.isoformat()
                    if isinstance(filing.filing_date, date)
                    else str(filing.filing_date)
                )

                results.append(
                    {
                        "ticker": ticker.upper(),
                        "formType": filing.form,
                        "filedAt": filed_at,
                        "accessionNo": filing.accession_number,
                        "cik": filing.cik,
                        "companyName": str(filing.company)
                        if hasattr(filing, "company")
                        else ticker,
                        "linkToTxt": filing.filing_url
                        if hasattr(filing, "filing_url")
                        else None,
                        "linkToHtml": filing.filing_url
                        if hasattr(filing, "filing_url")
                        else None,
                        "source": "edgar",
                    }
                )

                # 如果结果已达到size限制，停止遍历
                if len(results) >= size:
                    break

            return results

        except Exception as e:
            logger.warning(f"edgartools搜索失败 {ticker} {form_type}: {e}")
            raise

    def _search_sec_api(
        self,
        ticker: str,
        form_type: str,
        year: Optional[int],
        size: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        使用sec-api搜索SEC filings

        Returns:
            filings列表，每项包含:
            - id, accessionNo, cik, ticker, companyName
            - formType, description, filedAt
            - linkToTxt, linkToHtml, linkToFilingDetails
        """
        if not self._api_key:
            raise NetworkError("SEC_API_KEY未配置，请设置环境变量 SEC_API_KEY")

        ds = self._get_datasource("sec_api")
        base_url = ds.base_url if ds else "https://api.sec-api.io"

        # 构建查询
        parts = [f"ticker:{ticker.upper()}"]
        if form_type:
            parts.append(f'formType:"{form_type}"')
        if year:
            date_from = f"{year}-01-01"
            date_to = f"{year}-12-31"
            parts.append(f"filedAt:[{date_from} TO {date_to}]")

        query = {"query_string": {"query": " AND ".join(parts)}}

        payload = {
            "query": query,
            "from": 0,
            "size": size,
            "sort": [{"filedAt": {"order": "desc"}}],
        }

        try:
            response = self._http_client.post(
                base_url,
                json=payload,
                headers={"Authorization": self._api_key},
            )
            response.raise_for_status()

            data = response.json()
            filings = data.get("filings", [])
            return filings

        except Exception as e:
            logger.error(f"sec-api搜索失败: {ticker} {form_type} {year} - {e}")
            if "401" in str(e) or "403" in str(e):
                raise NetworkError("SEC API认证失败，请检查SEC_API_KEY是否正确")
            raise NetworkError(f"sec-api搜索失败: {e}")

    def _search_filings(
        self,
        ticker: str,
        form_type: str,
        year: Optional[int],
        size: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        搜索SEC filings (优先edgartools，失败则sec-api)

        支持速率限制和重试逻辑
        """
        # 应用速率限制
        self._rate_limiter.wait("edgar_search")
        self._rate_limiter.wait("sec_api_search")

        last_error = None

        # 优先使用edgartools (免费)，带重试
        for attempt in range(self.MAX_RETRIES):
            try:
                if self._init_edgar():
                    return self._search_edgar(ticker, form_type, year, size)
            except Exception as e:
                last_error = e
                logger.warning(
                    f"edgartools搜索失败 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}"
                )
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_BACKOFF * (2**attempt))

        # edgartools失败，回退到sec-api (付费)，带重试
        if self._api_key:
            for attempt in range(self.MAX_RETRIES):
                try:
                    return self._search_sec_api(ticker, form_type, year, size)
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"sec-api搜索失败 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}"
                    )
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(self.RETRY_BACKOFF * (2**attempt))

        raise NetworkError(
            f"edgartools和sec-api均不可用: {last_error}"
            if last_error
            else "edgartools不可用且SEC_API_KEY未配置"
        )

    def _get_ticker_cik(self, ticker: str) -> Optional[str]:
        """通过ticker获取CIK"""
        if ticker in self._ticker_cache:
            return self._ticker_cache[ticker]

        try:
            filings = self._search_filings(ticker, "10-K", None, size=1)
            if filings:
                cik = filings[0].get("cik")
                if cik:
                    self._ticker_cache[ticker] = cik
                    return cik
        except Exception as e:
            logger.warning(f"获取CIK失败 {ticker}: {e}")

        return None

    def _download_10k(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载10-K年报"""
        ticker = code.upper()

        try:
            filings = self._search_filings(ticker, "10-K", year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year} 年的10-K文件",
                )

            filing = filings[0]
            return self._download_filing(
                filing, ticker, "10-K", year, on_progress, checkpoint
            )

        except NetworkError as e:
            return DownloadResult(
                success=False, error_code=e.error_code, error_message=str(e)
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    def _download_10q(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载10-Q季报"""
        ticker = code.upper()

        try:
            filings = self._search_filings(ticker, "10-Q", year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year} 年的10-Q文件",
                )

            filing = filings[0]
            return self._download_filing(
                filing, ticker, "10-Q", year, on_progress, checkpoint
            )

        except NetworkError as e:
            return DownloadResult(
                success=False, error_code=e.error_code, error_message=str(e)
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    def _download_s1(
        self,
        code: str,
        document_type: str,
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载S-1招股说明书"""
        ticker = code.upper()
        form_type = "S-1" if document_type.lower() == "s1" else "S-1/A"

        try:
            filings = self._search_filings(ticker, form_type, None, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} 的{form_type}文件",
                )

            filing = filings[0]
            return self._download_filing(
                filing, ticker, form_type, None, on_progress, checkpoint
            )

        except NetworkError as e:
            return DownloadResult(
                success=False, error_code=e.error_code, error_message=str(e)
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    def _download_6k(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载6-K报告"""
        ticker = code.upper()

        try:
            filings = self._search_filings(ticker, "6-K", year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year} 年的6-K文件",
                )

            filing = filings[0]
            return self._download_filing(
                filing, ticker, "6-K", year, on_progress, checkpoint
            )

        except NetworkError as e:
            return DownloadResult(
                success=False, error_code=e.error_code, error_message=str(e)
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    def _download_filing(
        self,
        filing: Dict[str, Any],
        ticker: str,
        form_type: str,
        year: Optional[int],
        on_progress: Optional[Callable],
        checkpoint: Optional[Dict[str, Any]],
    ) -> DownloadResult:
        """下载filing文档，带速率限制和重试"""
        # edgartools格式: linkToTxt/html_url, sec-api格式: linkToTxt/linkToHtml
        link = (
            filing.get("linkToTxt")
            or filing.get("html_url")
            or filing.get("linkToHtml")
            or filing.get("linkToFilingDetails")
        )

        if not link:
            return DownloadResult(
                success=False,
                error_code="URL_NOT_FOUND",
                error_message="无法获取文档链接",
            )

        # 解析filedAt获取年份
        filed_at = filing.get("filedAt", "") or filing.get("filing_date", "")
        file_year = year
        if isinstance(filed_at, str) and not file_year:
            try:
                file_year = int(filed_at[:4])
            except (ValueError, IndexError):
                pass

        # 确定文件扩展名
        ext = ".txt"
        if link.endswith(".htm") or " exhibit" in link.lower():
            ext = ".html"

        # 构建保存路径
        file_path = self._build_file_path(
            ticker, file_year, form_type.replace("-", ""), ext
        )

        # SEC要求特定User-Agent头
        sec_ua = os.environ.get(
            "SEC_USER_AGENT", "UnifiedDownloader/1.0 (Financial Document Downloader)"
        )
        headers = {
            "User-Agent": sec_ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        # 带重试的下载
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            # 应用速率限制
            self._rate_limiter.wait("edgar_download")

            try:
                result = self._http_client.download_file(
                    link,
                    file_path,
                    on_progress=on_progress,
                    checkpoint=checkpoint,
                    headers=headers,
                )

                return DownloadResult(
                    success=True,
                    file_path=result["file_path"],
                    file_size=result["file_size"],
                    source=filing.get("source", "edgar"),
                    metadata={
                        "ticker": ticker,
                        "form_type": form_type,
                        "filed_at": filed_at,
                        "accession_no": filing.get("accessionNo")
                        or filing.get("accession_number"),
                    },
                )

            except Exception as e:
                last_error = e
                logger.warning(f"下载失败 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_BACKOFF * (2**attempt))

        return DownloadResult(
            success=False,
            error_code="DOWNLOAD_ERROR",
            error_message=f"下载失败: {last_error}",
        )

    async def _async_download_10k(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """异步下载10-K年报"""
        ticker = code.upper()

        try:
            # 尝试edgartools
            if self._init_edgar():
                filings = self._search_edgar(ticker, "10-K", year, size=1)
            else:
                filings = self._search_sec_api(ticker, "10-K", year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year} 年的10-K",
                )

            filing = filings[0]
            return await self._download_filing_async(
                http_client, filing, ticker, "10-K", year, on_progress, checkpoint
            )

        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    async def _async_download_10q(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """异步下载10-Q季报"""
        ticker = code.upper()

        try:
            if self._init_edgar():
                filings = self._search_edgar(ticker, "10-Q", year, size=1)
            else:
                filings = self._search_sec_api(ticker, "10-Q", year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year} 年的10-Q",
                )

            filing = filings[0]
            return await self._download_filing_async(
                http_client, filing, ticker, "10-Q", year, on_progress, checkpoint
            )

        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    async def _async_download_s1(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        document_type: str,
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """异步下载S-1招股说明书"""
        ticker = code.upper()
        form_type = "S-1" if document_type.lower() == "s1" else "S-1/A"

        try:
            if self._init_edgar():
                filings = self._search_edgar(ticker, form_type, None, size=1)
            else:
                filings = self._search_sec_api(ticker, form_type, None, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} 的{form_type}",
                )

            filing = filings[0]
            return await self._download_filing_async(
                http_client, filing, ticker, form_type, None, on_progress, checkpoint
            )

        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    async def _download_filing_async(
        self,
        http_client: AsyncHTTPClient,
        filing: Dict[str, Any],
        ticker: str,
        form_type: str,
        year: Optional[int],
        on_progress: Optional[Callable],
        checkpoint: Optional[Dict[str, Any]],
    ) -> DownloadResult:
        """异步下载filing文档"""
        link = (
            filing.get("linkToTxt")
            or filing.get("html_url")
            or filing.get("linkToHtml")
            or filing.get("linkToFilingDetails")
        )
        if not link:
            return DownloadResult(
                success=False,
                error_code="URL_NOT_FOUND",
                error_message="无法获取文档链接",
            )

        filed_at = filing.get("filedAt", "") or filing.get("filing_date", "")
        file_year = year
        if isinstance(filed_at, str) and not file_year:
            try:
                file_year = int(filed_at[:4])
            except (ValueError, IndexError):
                pass

        ext = ".txt"
        if link.endswith(".htm"):
            ext = ".html"

        file_path = self._build_file_path(
            ticker, file_year, form_type.replace("-", ""), ext
        )

        try:
            result = await http_client.download_file(
                link, file_path, on_progress=on_progress, checkpoint=checkpoint
            )
            return DownloadResult(
                success=True,
                file_path=result["file_path"],
                file_size=result["file_size"],
                source=filing.get("source", "edgar"),
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    def _find_best_datasource(self) -> DataSource:
        """查找最佳数据源"""
        for ds in self.enabled_datasources:
            if ds.name == "sec_api":
                return ds
        return (
            self.enabled_datasources[0]
            if self.enabled_datasources
            else DataSource(
                name="sec_api",
                base_url="https://api.sec-api.io",
            )
        )

    def search(
        self, code: str, year: Optional[int] = None, document_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """搜索可用文档"""
        ticker = code.upper()
        form_type = document_type or "10-K"

        try:
            return self._search_filings(ticker, form_type, year, size=20)
        except Exception:
            return []

    def get_available_years(
        self, code: str, document_type: Optional[str] = None
    ) -> List[int]:
        """获取可用年份"""
        filings = self.search(code, document_type=document_type or "10-K")
        years = set()
        for filing in filings:
            filed_at = filing.get("filedAt", "") or filing.get("filing_date", "")
            if isinstance(filed_at, str):
                try:
                    years.add(int(filed_at[:4]))
                except (ValueError, IndexError):
                    pass
        return sorted(list(years), reverse=True)
