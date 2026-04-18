"""A股适配器 - 使用 AKShare + 巨潮资讯"""

import logging
import time
import urllib.parse
from datetime import date
from typing import Optional, List, Dict, Any, Callable

import pandas as pd

from unified_downloader.adapters.base import BaseStockAdapter
from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult, DataSource
from unified_downloader.infra.http_client import HTTPClient, AsyncHTTPClient

logger = logging.getLogger(__name__)


class CninfoAPI:
    """巨潮资讯API封装"""

    # PDF下载基础URL
    PDF_BASE_URL = "http://static.cninfo.com.cn/finalpage"

    # 支持的报告类别
    CATEGORIES = {
        "年报": "年报",
        "半年报": "半年报",
        "一季报": "一季报",
        "三季报": "三季报",
        "业绩预告": "业绩预告",
        "权益分派": "权益分派",
    }

    @staticmethod
    def search_disclosure(
        symbol: str,
        market: str = "沪深京",
        category: str = "年报",
        start_date: str = "20200101",
        end_date: str = "20251231",
    ) -> pd.DataFrame:
        """
        搜索信息披露公告

        Args:
            symbol: 股票代码
            market: 市场 (沪深京/港股/三板/基金/债券等)
            category: 公告类别
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            DataFrame with columns: 代码, 简称, 公告标题, 公告时间, 公告链接
        """
        import akshare as ak

        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=symbol,
            market=market,
            category=category,
            start_date=start_date,
            end_date=end_date,
        )
        return df

    @staticmethod
    def extract_pdf_url(detail_url: str) -> Optional[str]:
        """
        从公告详情URL提取PDF下载URL

        Args:
            detail_url: 公告详情页URL

        Returns:
            PDF下载URL or None
        """
        try:
            parsed = urllib.parse.urlparse(detail_url)
            params = urllib.parse.parse_qs(parsed.query)

            announcement_id = params.get("announcementId", [""])[0]
            announcement_time = params.get("announcementTime", [""])[0]

            if announcement_id and announcement_time:
                # 格式化日期: 2025-03-15 (保持横杠格式)
                date_str = announcement_time.split(" ")[0]
                pdf_url = f"{CninfoAPI.PDF_BASE_URL}/{date_str}/{announcement_id}.PDF"
                return pdf_url
        except Exception as e:
            logger.warning(f"解析PDF URL失败: {e}")

        return None

    @staticmethod
    def download_pdf(pdf_url: str, save_path: str, http_client: HTTPClient) -> bool:
        """
        下载PDF文件

        Args:
            pdf_url: PDF URL
            save_path: 保存路径
            http_client: HTTP客户端

        Returns:
            是否成功
        """
        try:
            result = http_client.download_file(pdf_url, save_path)
            return result.get("file_size", 0) > 0
        except Exception as e:
            logger.error(f"PDF下载失败: {e}")
            return False


class AStockAdapter(BaseStockAdapter):
    """
    A股下载适配器

    使用 AKShare + 巨潮资讯 API
    """

    market = Market.A

    def __init__(
        self,
        http_client: HTTPClient,
        datasources: List[Dict],
        rate_limit_interval: float = 2.0,
    ):
        super().__init__(http_client, datasources)
        self._search_cache: Dict[str, List[Dict]] = {}
        self._rate_limiter_interval = rate_limit_interval
        self._last_request_time = 0.0

    def _wait_rate_limit(self) -> None:
        """速率限制"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._rate_limiter_interval:
            time.sleep(self._rate_limiter_interval - elapsed)
        self._last_request_time = time.time()

    def _format_date(self, dt: date) -> str:
        """格式化日期为YYYYMMDD"""
        return dt.strftime("%Y%m%d")

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
        """下载A股文档"""
        doc_type_lower = document_type.lower()

        if doc_type_lower in ["annual_report", "年报", "年度报告"]:
            return self._download_annual_report(
                code, year, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["interim_report", "中期报告", "半年报"]:
            return self._download_interim_report(
                code, year, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["quarterly", "季度", "一季报", "三季报"]:
            return self._download_quarterly_report(
                code, year, datasource, checkpoint, on_progress
            )
        else:
            return self._download_annual_report(
                code, year, datasource, checkpoint, on_progress
            )

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
        """异步下载A股文档"""
        return self.download(
            code, year, document_type, datasource, checkpoint, on_progress
        )

    def _download_annual_report(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载年度报告"""
        return self._download_report(
            code, year, "年报", "annual_report", checkpoint, on_progress
        )

    def _download_interim_report(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载中期报告"""
        return self._download_report(
            code, year, "半年报", "interim_report", checkpoint, on_progress
        )

    def _download_quarterly_report(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载季度报告"""
        # 默认下载一季报
        return self._download_report(
            code, year, "一季报", "quarterly_report", checkpoint, on_progress
        )

    def _download_report(
        self,
        code: str,
        year: Optional[int],
        category: str,
        doc_type_name: str,
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载报告通用逻辑"""
        # 标准化股票代码
        symbol = code.strip().upper()
        if symbol.startswith("SH"):
            symbol = symbol[2:]
        elif symbol.startswith("SZ"):
            symbol = symbol[2:]

        # 确定日期范围
        if year:
            start_date = f"{year}0101"
            end_date = f"{year}1231"
        else:
            end_date = self._format_date(date.today())
            start_date = self._format_date(
                date.today().replace(year=date.today().year - 1)
            )

        # 搜索公告
        self._wait_rate_limit()
        try:
            df = CninfoAPI.search_disclosure(
                symbol=symbol,
                category=category,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            logger.error(f"搜索公告失败: {e}")
            return DownloadResult(
                success=False,
                error_code="SEARCH_ERROR",
                error_message=f"搜索公告失败: {e}",
            )

        if df is None or df.empty:
            return DownloadResult(
                success=False,
                error_code="NO_FILINGS_FOUND",
                error_message=f"未找到 {symbol} {year or '最新'} 的{category}",
            )

        # 取第一条记录
        row = df.iloc[0]
        announcement_title = row.get("公告标题", "")
        announcement_time = row.get("公告时间", "")
        detail_url = row.get("公告链接", "")

        # 提取PDF URL
        pdf_url = CninfoAPI.extract_pdf_url(detail_url)
        if not pdf_url:
            return DownloadResult(
                success=False,
                error_code="URL_NOT_FOUND",
                error_message="无法获取PDF下载链接",
            )

        # 确定文件年份
        file_year = year
        if not file_year and announcement_time:
            try:
                file_year = int(announcement_time[:4])
            except (ValueError, TypeError):
                file_year = date.today().year

        # 构建保存路径
        file_path = self._build_file_path(
            symbol.zfill(6), file_year, doc_type_name.upper(), ".PDF"
        )

        # 下载PDF
        try:
            result = self._http_client.download_file(
                pdf_url, file_path, on_progress=on_progress, checkpoint=checkpoint
            )

            return DownloadResult(
                success=True,
                file_path=result["file_path"],
                file_size=result["file_size"],
                source="cninfo",
                metadata={
                    "symbol": symbol,
                    "category": category,
                    "title": announcement_title,
                    "announcement_time": announcement_time,
                },
            )

        except Exception as e:
            logger.error(f"下载失败: {e}")
            return DownloadResult(
                success=False,
                error_code="DOWNLOAD_ERROR",
                error_message=f"下载失败: {e}",
            )

    def search(
        self, code: str, year: Optional[int] = None, document_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """搜索可用文档"""
        symbol = code.strip().upper()
        if symbol.startswith("SH") or symbol.startswith("SZ"):
            symbol = symbol[2:]

        if year:
            start_date = f"{year}0101"
            end_date = f"{year}1231"
        else:
            end_date = self._format_date(date.today())
            start_date = self._format_date(
                date.today().replace(year=date.today().year - 1)
            )

        category = "年报"
        if document_type:
            doc_type_lower = document_type.lower()
            if (
                "interim" in doc_type_lower
                or "中期" in doc_type_lower
                or "半年" in doc_type_lower
            ):
                category = "半年报"
            elif (
                "quarterly" in doc_type_lower
                or "季度" in doc_type_lower
                or "一季" in doc_type_lower
            ):
                category = "一季报"
            elif "三季" in doc_type_lower:
                category = "三季报"

        self._wait_rate_limit()
        try:
            df = CninfoAPI.search_disclosure(
                symbol=symbol,
                category=category,
                start_date=start_date,
                end_date=end_date,
            )

            results = []
            for _, row in df.iterrows():
                results.append(
                    {
                        "symbol": row.get("代码", ""),
                        "name": row.get("简称", ""),
                        "title": row.get("公告标题", ""),
                        "time": row.get("公告时间", ""),
                        "url": row.get("公告链接", ""),
                    }
                )
            return results

        except Exception as e:
            logger.error(f"搜索失败: {e}")
            return []

    def get_available_years(
        self, code: str, document_type: Optional[str] = None
    ) -> List[int]:
        """获取可用年份"""
        documents = self.search(code, document_type=document_type)
        years = set()

        for doc in documents:
            time_str = doc.get("time", "")
            try:
                if time_str:
                    years.add(int(time_str[:4]))
            except (ValueError, TypeError):
                pass

        return sorted(list(years), reverse=True)
