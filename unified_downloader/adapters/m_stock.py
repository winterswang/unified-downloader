"""美股适配器 - 使用 edgartools (主) + sec-api (兜底)"""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable

from unified_downloader.adapters.base import BaseStockAdapter
from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult, DataSource
from unified_downloader.infra.http_client import HTTPClient, AsyncHTTPClient
from unified_downloader.infra.rate_limiter import RateLimiter
from unified_downloader.infra.converter import HTMLToPDFConverter
from unified_downloader.infra.translator import PDFTranslator
from unified_downloader.exceptions import (
    NetworkError,
    ConversionError,
    TranslationError,
)

logger = logging.getLogger(__name__)


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
        convert_to_pdf: bool = False,
        keep_original_html: bool = True,
        sec_user_agent: Optional[str] = None,
        edgar_identity: Optional[str] = None,
        translate_enabled: bool = False,
        translate_api_key: Optional[str] = None,
        translate_model: str = "MiniMax-M2.7",
        translate_base_url: str = "https://api.minimaxi.com/v1",
        translate_qps: int = 4,
    ):
        super().__init__(http_client, datasources)
        self._api_key = api_key or self._get_api_key()
        self._ticker_cache: Dict[str, str] = {}
        self._edgar_identity = edgar_identity
        self._sec_user_agent = sec_user_agent
        self._rate_limiter = RateLimiter(min_interval=rate_limit_interval)
        self._convert_to_pdf = convert_to_pdf
        self._keep_original_html = keep_original_html
        self._translate_enabled = translate_enabled
        self._translate_api_key = translate_api_key
        self._translate_model = translate_model
        self._translate_base_url = translate_base_url
        self._translate_qps = translate_qps
        self._use_translate_cache = True

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
        """获取EDGAR Identity (邮箱)，优先级: 构造参数 > 配置文件 > 环境变量 > 默认值"""
        if self._edgar_identity:
            return self._edgar_identity

        # 尝试从配置文件读取
        from unified_downloader.core.config import get_default_config
        cfg = get_default_config()
        if cfg.edgar_identity:
            self._edgar_identity = cfg.edgar_identity
            return self._edgar_identity

        identity = os.environ.get(
            "EDGAR_IDENTITY", "UnifiedDownloader unified-downloader@example.com"
        )
        self._edgar_identity = identity
        return identity

    def _init_edgar(self) -> bool:
        """初始化edgartools（兼容 edgar 4.x 和 5.x）"""
        try:
            identity = self._get_edgar_identity()
            # edgar 5.x: set_identity(name, email)
            # edgar 4.x: set_identity(email)
            import edgar as edgar_module
            if hasattr(edgar_module, "set_identity"):
                try:
                    edgar_module.set_identity("UnifiedDownloader", identity)
                except TypeError:
                    # Fallback to 4.x single-arg signature
                    edgar_module.set_identity(identity)
            else:
                # Older versions: from edgar import set_identity
                from edgar import set_identity  # type: ignore[attr-defined]
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
            return self._download_10k(code, year, datasource, checkpoint, on_progress, **kwargs)
        elif doc_type_lower in ["10q", "ten_q"]:
            return self._download_10q(code, year, datasource, checkpoint, on_progress, **kwargs)
        elif doc_type_lower in ["s1", "s1a", "f1", "424b4", "prospectus"]:
            return self._download_prospectus(
                code, document_type, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["6k", "8k"]:
            return self._download_6k(code, year, datasource, checkpoint, on_progress)
        elif doc_type_lower in ["20f", "20-f", "twenty_f"]:
            return self._download_form(code, "20-F", year, checkpoint, on_progress)
        else:
            return self._download_10k(code, year, datasource, checkpoint, on_progress, **kwargs)

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
                http_client, code, year, datasource, checkpoint, on_progress, **kwargs
            )
        elif doc_type_lower in ["10q", "ten_q"]:
            return await self._async_download_10q(
                http_client, code, year, datasource, checkpoint, on_progress, **kwargs
            )
        elif doc_type_lower in ["s1", "s1a", "f1", "424b4", "prospectus"]:
            return await self._async_download_prospectus(
                http_client, code, document_type, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ["6k", "8k"]:
            return await self._async_download_form(
                http_client, code, "6-K" if doc_type_lower == "6k" else "8-K", year, checkpoint, on_progress
            )
        elif doc_type_lower in ["20f", "20-f", "twenty_f"]:
            return await self._async_download_form(
                http_client, code, "20-F", year, checkpoint, on_progress
            )
        else:
            return await self._async_download_10k(
                http_client, code, year, datasource, checkpoint, on_progress, **kwargs
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
            # edgar 5.x: Company(name, cik) with keyword or positional args
            # edgar 4.x: Company(cik) with single arg
            try:
                company = Company(ticker.upper())
            except TypeError:
                company = Company(ticker.upper(), ticker.upper())

            # edgar 5.x: get_all_filings(); edgar 4.x: get_filings()
            if hasattr(company, "get_all_filings"):
                edgar_filings = company.get_all_filings(form=form_type)
            else:
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

                result_entry = {
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

                # 6-K is just a cover page; extract exhibit URLs for the real content
                if filing.form == "6-K" and hasattr(filing, "attachments"):
                    try:
                        exhibits = []
                        primary_doc = getattr(filing, "primary_document", "")
                        acc_no = filing.accession_number.replace("-", "")
                        for doc in filing.attachments:
                            doc_name = str(getattr(doc, "document", ""))
                            if not doc_name or doc_name == primary_doc:
                                continue
                            if doc_name.endswith((".htm", ".html")):
                                exhibits.append({
                                    "url": f"https://www.sec.gov/Archives/edgar/data/{filing.cik}/{acc_no}/{doc_name}",
                                    "description": str(getattr(doc, "description", ""))[:200],
                                    "document": doc_name,
                                })
                        if exhibits:
                            result_entry["_exhibits"] = exhibits[:10]
                    except Exception:
                        pass  # Non-critical; proceed without exhibits

                results.append(result_entry)

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

    def _download_form(
        self,
        code: str,
        form_type: str,
        year: Optional[int],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载指定类型SEC文档的通用方法"""
        ticker = code.upper()

        try:
            filings = self._search_filings(ticker, form_type, year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year or ''} 的{form_type}文件",
                )

            filing = filings[0]
            return self._download_filing(
                filing, ticker, form_type, year, on_progress, checkpoint
            )

        except NetworkError as e:
            return DownloadResult(
                success=False, error_code=e.error_code, error_message=str(e)
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    def _download_10k(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
        **kwargs,
    ) -> DownloadResult:
        """下载年度报告：FPI用20-F，本土用10-K"""
        form_type = self.get_annual_form_type(code)
        return self._download_form(code, form_type, year, checkpoint, on_progress)

    def get_annual_form_type(self, code: str) -> str:
        """判断公司的年度报告SEC归档表格类型。

        外国私人发行人(FPI)如中概股/ADR，年度用20-F
        美国本土公司用10-K

        作为 USFormResolver 协议成员被 UnifiedDownloader 跨模块调用。
        """
        try:
            if self._init_edgar():
                from edgar import Company
                company = Company(code.upper())
                if company.is_foreign:
                    logger.info(f"{code} is FPI, using 20-F for annual report")
                    return "20-F"
        except Exception as e:
            logger.debug(f"FPI check failed for {code}: {e}, defaulting to 10-K")
        return "10-K"

    def _download_10q(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
        **kwargs,
    ) -> DownloadResult:
        """下载季度报告：外国私人发行人(FPI)使用6-K，美国本土公司使用10-Q"""
        form_type = self.get_quarterly_form_type(code)
        return self._download_form(code, form_type, year, checkpoint, on_progress)

    def get_quarterly_form_type(self, code: str) -> str:
        """判断公司的季度报告SEC归档表格类型。

        外国私人发行人(Foreign Private Issuer)如中概股，季度用6-K
        美国本土公司使用10-Q

        作为 USFormResolver 协议成员被 UnifiedDownloader 跨模块调用。
        """
        try:
            if self._init_edgar():
                from edgar import Company
                company = Company(code.upper())
                if company.is_foreign:
                    logger.info(f"{code} is FPI, using 6-K for quarterly report")
                    return "6-K"
        except Exception as e:
            logger.debug(f"FPI check failed for {code}: {e}, defaulting to 10-Q")
        return "10-Q"

    def _download_prospectus(
        self,
        code: str,
        document_type: str,
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载美股招股说明书

        搜索策略（级联）：
        1. 用户明确指定 form type → 直接用
        2. prospectus/s1/s1a → S-1 → F-1 → 424B4 依次尝试
        """
        doc_lower = document_type.lower()

        # 用户明确指定 form type
        form_map = {
            "s1": "S-1",
            "s1a": "S-1/A",
            "f1": "F-1",
            "424b4": "424B4",
        }
        if doc_lower in form_map:
            return self._download_form(
                code, form_map[doc_lower], None, checkpoint, on_progress
            )

        # prospectus: 级联搜索 S-1 → F-1 → 424B4
        ticker = code.upper()
        for form in ["S-1", "F-1", "424B4"]:
            try:
                filings = self._search_filings(ticker, form, None, size=1)
                if filings:
                    logger.info(
                        f"找到 {ticker} 的 {form} 招股书"
                    )
                    return self._download_form(
                        code, form, None, checkpoint, on_progress
                    )
            except Exception as e:
                logger.debug(f"搜索 {ticker} {form} 失败: {e}")
                continue

        return DownloadResult(
            success=False,
            error_code="NO_FILINGS_FOUND",
            error_message=f"未找到 {ticker} 的招股书（已尝试 S-1, F-1, 424B4）",
        )

    def _download_6k(
        self,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """下载6-K报告（含展品合并）"""
        return self._download_form(code, "6-K", year, checkpoint, on_progress)

    def _merge_6k_exhibits(
        self,
        main_file: Path,
        exhibits: List[Dict[str, str]],
        ticker: str,
        file_year: Optional[int],
        form_type: str,
        headers: Dict[str, str],
    ) -> Path:
        """下载6-K展品并合并到主文件中。
        
        6-K本身只是SEC封面页，真正的季报/公告内容在EX-99展品中。
        这个方法下载所有HTML展品，提取body内容，合并为一个HTML文件。
        
        Returns:
            合并后的HTML文件路径
        """
        exhibit_files = [main_file]  # Start with the cover
        
        for i, ex in enumerate(exhibits):
            ex_url = ex.get("url", "")
            ex_desc = ex.get("description", f"exhibit_{i}")
            if not ex_url:
                continue
            
            # Build exhibit file path
            ex_path = main_file.parent / f"{ticker}_{file_year}_{form_type}_ex{i}.html"
            
            try:
                self._rate_limiter.wait("edgar_download")
                logger.info(f"Downloading 6-K exhibit {i}: {ex_desc[:80]}")
                
                ex_result = self._http_client.download_file(
                    url=ex_url, file_path=str(ex_path), headers=headers
                )
                if os.path.exists(str(ex_path)) and os.path.getsize(str(ex_path)) > 100:
                    exhibit_files.append(ex_path)
                    logger.info(f"  → {ex_path} ({os.path.getsize(str(ex_path))} bytes)")
                else:
                    logger.debug(f"  → Exhibit {i} too small or missing, skipped")
            except Exception as e:
                logger.warning(f"  → Exhibit {i} download failed: {e}")
        
        if len(exhibit_files) == 1:
            return main_file  # No exhibits were downloaded
        
        # Merge all HTML files into one
        import re
        combined_parts = []
        for f in exhibit_files:
            try:
                with open(str(f), "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                body_match = re.search(
                    r"<body[^>]*>(.*?)</body>", content, re.DOTALL | re.IGNORECASE
                )
                if body_match:
                    combined_parts.append(body_match.group(1))
                else:
                    combined_parts.append(content)
            except Exception as e:
                logger.warning(f"Failed to read exhibit {f}: {e}")
        
        combined_html = (
            '<html><head><meta charset="utf-8">'
            '<style>body{font-family:sans-serif;margin:20px}'
            'hr{page-break-after:always;border:none}'
            '.exhibit-label{color:#666;font-size:12px;margin-bottom:10px}</style>'
            '</head><body>'
            + '<hr>'.join(combined_parts)
            + '</body></html>'
        )
        
        merged_path = main_file.parent / f"{ticker}_{file_year}_{form_type}_merged.html"
        with open(str(merged_path), "w", encoding="utf-8") as f:
            f.write(combined_html)
        
        logger.info(
            f"6-K merged: {len(exhibit_files)} files → {merged_path} "
            f"({os.path.getsize(str(merged_path))} bytes)"
        )
        return merged_path

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

        # SEC要求特定User-Agent头，优先级: 构造参数 > 配置文件 > 环境变量 > 默认值
        if self._sec_user_agent:
            sec_ua = self._sec_user_agent
        else:
            from unified_downloader.core.config import get_default_config
            cfg = get_default_config()
            sec_ua = cfg.sec_user_agent or os.environ.get(
                "SEC_USER_AGENT", os.environ.get("USER") + "/contact@example.com Research Tool/1.0"
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

                # HTML→PDF 转换
                downloaded_path = Path(result["file_path"])

                # 6-K exhibits: download and merge exhibit content before PDF conversion
                # (6-K itself is only a cover page; real content is in EX-99 exhibits)
                if form_type == "6-K":
                    exhibits = filing.get("_exhibits", [])
                    if exhibits:
                        try:
                            downloaded_path = self._merge_6k_exhibits(
                                downloaded_path, exhibits, ticker, file_year,
                                form_type, headers
                            )
                        except Exception as e:
                            logger.warning(f"6-K exhibit merge failed, using cover only: {e}")

                converted_to_pdf = False
                if (
                    self._convert_to_pdf
                    and downloaded_path.suffix.lower() in (".html", ".htm")
                ):
                    try:
                        pdf_path = HTMLToPDFConverter.convert(
                            downloaded_path,
                            keep_original=self._keep_original_html,
                        )
                        result["file_path"] = str(pdf_path)
                        result["file_size"] = pdf_path.stat().st_size
                        downloaded_path = pdf_path
                        converted_to_pdf = True
                    except ConversionError as e:
                        logger.warning(f"PDF转换失败，保留原始HTML: {e}")

                # PDF 翻译
                translated = False
                translated_file_path = None
                if (
                    self._translate_enabled
                    and downloaded_path.suffix.lower() == ".pdf"
                ):
                    try:
                        translated_path = PDFTranslator.translate(
                            downloaded_path,
                            api_key=self._translate_api_key,
                            model=self._translate_model,
                            base_url=self._translate_base_url,
                            qps=self._translate_qps,
                            use_cache=self._use_translate_cache,
                        )
                        translated = True
                        translated_file_path = str(translated_path)
                    except TranslationError as e:
                        logger.warning(f"PDF翻译失败，保留原始文件: {e}")

                return DownloadResult(
                    success=True,
                    file_path=result["file_path"],
                    file_size=result["file_size"],
                    source=filing.get("source", "edgar"),
                    converted_to_pdf=converted_to_pdf,
                    translated=translated,
                    translated_file_path=translated_file_path,
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

    async def _async_download_form(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        form_type: str,
        year: Optional[int],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """异步下载指定类型SEC文档的通用方法"""
        ticker = code.upper()

        try:
            if self._init_edgar():
                filings = self._search_edgar(ticker, form_type, year, size=1)
            else:
                filings = self._search_sec_api(ticker, form_type, year, size=1)

            if not filings:
                return DownloadResult(
                    success=False,
                    error_code="NO_FILINGS_FOUND",
                    error_message=f"未找到 {ticker} {year or ''} 的{form_type}",
                )

            filing = filings[0]
            return await self._download_filing_async(
                http_client, filing, ticker, form_type, year, on_progress, checkpoint
            )

        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
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
        """异步下载年度报告：FPI用20-F，本土用10-K"""
        form_type = self.get_annual_form_type(code)
        return await self._async_download_form(http_client, code, form_type, year, checkpoint, on_progress)

    async def _async_download_10q(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        year: Optional[int],
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """异步下载季度报告：FPI用6-K，本土用10-Q"""
        form_type = self.get_quarterly_form_type(code)
        return await self._async_download_form(http_client, code, form_type, year, checkpoint, on_progress)

    async def _async_download_prospectus(
        self,
        http_client: AsyncHTTPClient,
        code: str,
        document_type: str,
        datasource: Optional[DataSource],
        checkpoint: Optional[Dict[str, Any]],
        on_progress: Optional[Callable],
    ) -> DownloadResult:
        """异步下载美股招股说明书（同步包装）"""
        return await asyncio.to_thread(
            self._download_prospectus,
            code, document_type, datasource, checkpoint, on_progress
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

            # HTML→PDF 转换
            downloaded_path = Path(result["file_path"])

            # 6-K exhibits: download and merge exhibit content before PDF conversion
            if form_type == "6-K":
                exhibits = filing.get("_exhibits", [])
                if exhibits:
                    try:
                        downloaded_path = self._merge_6k_exhibits(
                            downloaded_path, exhibits, ticker, file_year,
                            form_type, {}
                        )
                    except Exception as e:
                        logger.warning(f"6-K exhibit merge failed, using cover only: {e}")

            converted_to_pdf = False
            if (
                self._convert_to_pdf
                and downloaded_path.suffix.lower() in (".html", ".htm")
            ):
                try:
                    pdf_path = HTMLToPDFConverter.convert(
                        downloaded_path,
                        keep_original=self._keep_original_html,
                    )
                    result["file_path"] = str(pdf_path)
                    result["file_size"] = pdf_path.stat().st_size
                    downloaded_path = pdf_path
                    converted_to_pdf = True
                except ConversionError as e:
                    logger.warning(f"PDF转换失败，保留原始HTML: {e}")

            # PDF 翻译
            translated = False
            translated_file_path = None
            if (
                self._translate_enabled
                and downloaded_path.suffix.lower() == ".pdf"
            ):
                try:
                    translated_path = PDFTranslator.translate(
                        downloaded_path,
                        api_key=self._translate_api_key,
                        model=self._translate_model,
                        base_url=self._translate_base_url,
                        qps=self._translate_qps,
                        use_cache=self._use_translate_cache,
                    )
                    translated = True
                    translated_file_path = str(translated_path)
                except TranslationError as e:
                    logger.warning(f"PDF翻译失败，保留原始文件: {e}")

            return DownloadResult(
                success=True,
                file_path=result["file_path"],
                file_size=result["file_size"],
                source=filing.get("source", "edgar"),
                converted_to_pdf=converted_to_pdf,
                translated=translated,
                translated_file_path=translated_file_path,
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

    # CLI 文档类型 → SEC form type 映射
    _FORM_TYPE_MAP = {
        "10k": "10-K",
        "10q": "10-Q",
        "6k": "6-K",
        "8k": "8-K",
        "20f": "20-F",
        "s1": "S-1",
        "s1a": "S-1/A",
    }

    @classmethod
    def _normalize_form_type(cls, form_type: str) -> str:
        """将 CLI 文档类型(如 10k)标准化为 SEC form type(如 10-K)"""
        normalized = cls._FORM_TYPE_MAP.get(form_type.lower())
        if normalized:
            return normalized
        # 已经是标准格式(如 10-K)或未知类型，原样返回
        return form_type

    def search(
        self, code: str, year: Optional[int] = None, document_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """搜索可用文档"""
        ticker = code.upper()
        form_type = self._normalize_form_type(document_type or "10-K")

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
