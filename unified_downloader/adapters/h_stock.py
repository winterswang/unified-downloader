"""港股适配器 - 使用 HKEx API"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any, Callable


from unified_downloader.adapters.base import BaseStockAdapter
from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult, DataSource
from unified_downloader.infra.http_client import HTTPClient, AsyncHTTPClient

logger = logging.getLogger(__name__)


class HKExAPI:
    """港交所API客户端"""

    BASE_URL = "https://www1.hkexnews.hk"
    STOCK_SEARCH = "/search/prefix.do"
    DOCUMENT_SEARCH = "/search/titleSearchServlet.do"

    # 文档类型
    ANNUAL_RESULTS = {"t1code": "10000", "t2Gcode": "3", "t2code": "13300"}
    INTERIM_RESULTS = {"t1code": "10000", "t2Gcode": "3", "t2code": "13400"}
    QUARTERLY_RESULTS = {"t1code": "10000", "t2Gcode": "3", "t2code": "13600"}
    IPO_PROSPECTUS = {"t1code": "91000", "t2Gcode": "-2", "t2code": "91200"}

    def __init__(self, http_client: HTTPClient):
        self._http = http_client

    def search_stock(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        搜索股票信息

        Returns:
            {'id': '7609', 'code': '00700', 'name': '騰訊控股'}
        """
        timestamp = str(int(datetime.now().timestamp() * 1000))
        params = {
            "callback": "callback",
            "lang": "ZH",
            "type": "A",
            "name": stock_code,
            "market": "SEHK",
            "_": timestamp,
        }

        url = f"{self.BASE_URL}{self.STOCK_SEARCH}"
        response = self._http.get(url, params=params)
        response.raise_for_status()

        # 解析JSONP响应
        text = response.text
        match = re.search(r"callback\((.*)\)", text)
        if not match:
            return None

        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and "stockInfo" in data:
                stock_info = data["stockInfo"]
                if stock_info:
                    return {
                        "id": str(stock_info[0].get("stockId", "")),
                        "code": stock_info[0].get("code", ""),
                        "name": stock_info[0].get("name", ""),
                    }
        except (json.JSONDecodeError, IndexError):
            pass

        return None

    def search_documents(
        self,
        from_date: date,
        to_date: date,
        stock_id: str = "-1",
        doc_type: Dict[str, str] = None,
        title: str = "",
        row_range: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        搜索文档

        Args:
            from_date: 开始日期
            to_date: 结束日期
            stock_id: 股票ID
            doc_type: 文档类型配置
            title: 标题关键词
            row_range: 返回记录数
        """
        if doc_type is None:
            doc_type = self.ANNUAL_RESULTS

        params = {
            "sortDir": "0",
            "sortByOptions": "DateTime",
            "category": "0",
            "market": "SEHK",
            "stockId": stock_id,
            "documentType": "-1",
            "fromDate": from_date.strftime("%Y%m%d"),
            "toDate": to_date.strftime("%Y%m%d"),
            "title": title,
            "searchType": "1",
            "t1code": doc_type["t1code"],
            "t2Gcode": doc_type["t2Gcode"],
            "t2code": doc_type["t2code"],
            "rowRange": str(row_range),
            "lang": "zh",
        }

        url = f"{self.BASE_URL}{self.DOCUMENT_SEARCH}"
        response = self._http.get(url, params=params)
        response.raise_for_status()

        data = response.json()
        documents = []

        result_data = data.get("result", [])
        if isinstance(result_data, str):
            try:
                result_data = json.loads(result_data)
            except json.JSONDecodeError:
                result_data = []

        if isinstance(result_data, list):
            for item in result_data:
                doc = {
                    "news_id": item.get("NEWS_ID", ""),
                    "stock_code": item.get("STOCK_CODE", ""),
                    "stock_name": item.get("STOCK_NAME", ""),
                    "title": item.get("TITLE", ""),
                    "file_type": item.get("FILE_TYPE", ""),
                    "file_info": item.get("FILE_INFO", ""),
                    "file_link": item.get("FILE_LINK", ""),
                    "date_time": item.get("DATE_TIME", ""),
                    "t1_code": item.get("T1_CODE"),
                    "t2_code": item.get("T2_CODE"),
                    "t2_gcode": item.get("T2_GCODE"),
                }
                documents.append(doc)

        return documents

    def download_document(self, file_link: str, save_path: str) -> bool:
        """下载文档"""
        if file_link.startswith("http"):
            url = file_link
        else:
            url = f"{self.BASE_URL}{file_link}"

        result = self._http.download_file(url, save_path)
        return result.get("file_size", 0) > 0


class HStockAdapter(BaseStockAdapter):
    """
    港股下载适配器

    支持港交所披露易数据源
    """

    market = Market.H

    def __init__(
        self,
        http_client: HTTPClient,
        datasources: List[Dict],
        rate_limit_interval: float = 2.0,
    ):
        super().__init__(http_client, datasources)
        self._stock_code_cache: Dict[str, str] = {}
        self._rate_limiter_interval = rate_limit_interval
        self._last_request_time = 0.0

    def _wait_rate_limit(self) -> None:
        """速率限制"""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._rate_limiter_interval:
            time.sleep(self._rate_limiter_interval - elapsed)
        self._last_request_time = time.time()

    def _get_api(self) -> HKExAPI:
        """获取HKEx API实例"""
        return HKExAPI(self._http_client)

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
        """下载港股文档"""
        doc_type_lower = document_type.lower()

        if doc_type_lower in ["annual_report", "年报", "年度报告", "annual report"]:
            return self._download_annual_report(
                code, year, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["interim_report", "中期报告", "中期"]:
            return self._download_interim_report(
                code, year, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["prospectus", "招股说明书", "s1"]:
            return self._download_prospectus(code, datasource, checkpoint, on_progress)
        elif doc_type_lower in ["quarterly", "季度"]:
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
        """异步下载港股文档"""
        # 目前同步实现
        return self.download(
            code, year, document_type, datasource, checkpoint, on_progress
        )

    def _get_stock_id(self, stock_code: str) -> Optional[str]:
        """获取股票ID"""
        if stock_code in self._stock_code_cache:
            return self._stock_code_cache[stock_code]

        self._wait_rate_limit()
        api = self._get_api()
        stock_info = api.search_stock(stock_code)

        if stock_info:
            self._stock_code_cache[stock_code] = stock_info["id"]
            return stock_info["id"]

        return None

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
            code, year, "annual_report", HKExAPI.ANNUAL_RESULTS, checkpoint, on_progress
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
            code,
            year,
            "interim_report",
            HKExAPI.INTERIM_RESULTS,
            checkpoint,
            on_progress,
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
        return self._download_report(
            code,
            year,
            "quarterly_report",
            HKExAPI.QUARTERLY_RESULTS,
            checkpoint,
            on_progress,
        )

    def _download_prospectus(
        self,
        code: str,
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载招股说明书"""
        return self._download_report(
            code, None, "prospectus", HKExAPI.IPO_PROSPECTUS, checkpoint, on_progress
        )

    def _download_report(
        self,
        code: str,
        year: Optional[int],
        doc_type_name: str,
        doc_type_config: Dict[str, str],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载报告通用逻辑"""
        stock_code = code.upper().zfill(5)

        # 获取股票ID
        stock_id = self._get_stock_id(stock_code)
        if not stock_id:
            return DownloadResult(
                success=False,
                error_code="STOCK_NOT_FOUND",
                error_message=f"未找到股票 {stock_code}",
            )

        # 确定日期范围
        if year:
            from_date = date(year, 1, 1)
            to_date = date(year, 12, 31)
        else:
            to_date = date.today()
            from_date = to_date - timedelta(days=365)

        # 搜索文档
        self._wait_rate_limit()
        api = self._get_api()
        documents = api.search_documents(
            from_date=from_date,
            to_date=to_date,
            stock_id=stock_id,
            doc_type=doc_type_config,
        )

        if not documents:
            return DownloadResult(
                success=False,
                error_code="NO_FILINGS_FOUND",
                error_message=f"未找到 {stock_code} {year or '最新'} 的报告",
            )

        # 获取第一个文档
        doc = documents[0]
        file_link = doc.get("file_link", "")
        if not file_link:
            return DownloadResult(
                success=False,
                error_code="URL_NOT_FOUND",
                error_message="无法获取文档链接",
            )

        # 确定文件扩展名
        ext = ".pdf" if file_link.lower().endswith(".pdf") else ".html"

        # 构建保存路径
        file_year = year
        if not file_year:
            try:
                dt = datetime.strptime(doc.get("date_time", ""), "%d/%m/%Y %H:%M")
                file_year = dt.year
            except (ValueError, TypeError):
                file_year = date.today().year

        file_path = self._build_file_path(
            stock_code, file_year, doc_type_name.upper(), ext
        )

        # 下载文档
        try:
            # 构造完整URL
            if file_link.startswith("http"):
                url = file_link
            else:
                url = f"{HKExAPI.BASE_URL}{file_link}"

            result = self._http_client.download_file(
                url, file_path, on_progress=on_progress, checkpoint=checkpoint
            )

            return DownloadResult(
                success=True,
                file_path=result["file_path"],
                file_size=result["file_size"],
                source="hkex",
                metadata={
                    "ticker": stock_code,
                    "stock_name": doc.get("stock_name", ""),
                    "title": doc.get("title", ""),
                    "doc_type": doc_type_name,
                    "date_time": doc.get("date_time", ""),
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
        stock_code = code.upper().zfill(5)
        stock_id = self._get_stock_id(stock_code)

        if not stock_id:
            return []

        if year:
            from_date = date(year, 1, 1)
            to_date = date(year, 12, 31)
        else:
            to_date = date.today()
            from_date = to_date - timedelta(days=365)

        doc_type_config = HKExAPI.ANNUAL_RESULTS
        if document_type:
            doc_type_lower = document_type.lower()
            if "interim" in doc_type_lower or "中期" in doc_type_lower:
                doc_type_config = HKExAPI.INTERIM_RESULTS
            elif "quarterly" in doc_type_lower or "季度" in doc_type_lower:
                doc_type_config = HKExAPI.QUARTERLY_RESULTS
            elif "prospectus" in doc_type_lower or "招股" in doc_type_lower:
                doc_type_config = HKExAPI.IPO_PROSPECTUS

        self._wait_rate_limit()
        api = self._get_api()
        return api.search_documents(
            from_date=from_date,
            to_date=to_date,
            stock_id=stock_id,
            doc_type=doc_type_config,
        )

    def get_available_years(
        self, code: str, document_type: Optional[str] = None
    ) -> List[int]:
        """获取可用年份"""
        documents = self.search(code, document_type=document_type)
        years = set()

        for doc in documents:
            date_time = doc.get("date_time", "")
            try:
                dt = datetime.strptime(date_time, "%d/%m/%Y %H:%M")
                years.add(dt.year)
            except (ValueError, TypeError):
                pass

        return sorted(list(years), reverse=True)
