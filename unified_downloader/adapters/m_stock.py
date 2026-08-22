"""美股适配器 - 使用 edgartools (主) + sec-api (兜底)"""

import asyncio
import calendar
import datetime
import logging
import os
import re
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
    UnsupportedOperationError,
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
        translate_model: str = "minimax-m3",
        translate_base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3",
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

        if doc_type_lower in self._ANNUAL_DOC_TYPES:
            return self._download_10k(code, year, datasource, checkpoint, on_progress, **kwargs)
        elif doc_type_lower in self._QUARTERLY_DOC_TYPES:
            # W40-#50: quarterly/interim_report/q1_report/q3_report 之前落 else
            # 分支被当 10-K 年报下载（要季报给年报, 文件名标 20F, 无告警）
            return self._download_10q(code, year, datasource, checkpoint, on_progress, **kwargs)
        elif doc_type_lower in self._PROSPECTUS_DOC_TYPES:
            return self._download_prospectus(
                code, document_type, datasource, checkpoint, on_progress
            )
        elif doc_type_lower == "6k":
            return self._download_6k(
                code, year, datasource, checkpoint, on_progress,
                report_period=kwargs.get("report_period"),
            )
        elif doc_type_lower == "8k":
            # W40-#50: 8-K 之前跟 6k 一起进 _download_6k（内部硬编码 "6-K"），
            # sync 下载 6-K 而 async 下载 8-K, 同一调用两个入口行为相反
            return self._download_form(code, "8-K", year, checkpoint, on_progress)
        elif doc_type_lower in self._TWENTY_F_DOC_TYPES:
            return self._download_form(code, "20-F", year, checkpoint, on_progress)
        else:
            # W40-#50: 未知类型不再静默按 10-K 下载（错误数据入库），显式报错
            raise UnsupportedOperationError(
                f"美股市场不支持的文档类型: {document_type}"
                f"（支持: 10k/annual_report, 10q/quarterly/q1_report/q3_report/"
                f"interim_report, 6k, 8k, 20f, s1/f1/424b4/prospectus）"
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
        """异步下载美股文档"""
        doc_type_lower = document_type.lower()

        if doc_type_lower in self._ANNUAL_DOC_TYPES:
            return await self._async_download_10k(
                http_client, code, year, datasource, checkpoint, on_progress, **kwargs
            )
        elif doc_type_lower in self._QUARTERLY_DOC_TYPES:
            # W40-#50: 与 sync 分发对齐（之前 quarterly 家族落 else 被当 10-K）
            return await self._async_download_10q(
                http_client, code, year, datasource, checkpoint, on_progress, **kwargs
            )
        elif doc_type_lower in self._PROSPECTUS_DOC_TYPES:
            return await self._async_download_prospectus(
                http_client, code, document_type, datasource, checkpoint, on_progress
            )
        elif doc_type_lower in ("6k", "8k"):
            return await self._async_download_form(
                http_client, code, "6-K" if doc_type_lower == "6k" else "8-K", year, checkpoint, on_progress
            )
        elif doc_type_lower in self._TWENTY_F_DOC_TYPES:
            return await self._async_download_form(
                http_client, code, "20-F", year, checkpoint, on_progress
            )
        else:
            # W40-#50: 未知类型不再静默按 10-K 下载，显式报错（与 sync 一致）
            raise UnsupportedOperationError(
                f"美股市场不支持的文档类型: {document_type}"
                f"（支持: 10k/annual_report, 10q/quarterly/q1_report/q3_report/"
                f"interim_report, 6k, 8k, 20f, s1/f1/424b4/prospectus）"
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
                    # 单文档 6-K (NVO 等): 正文就是 primary doc, 无独立 EX-99 exhibit.
                    # 记录 size 供回退时选“最大文件”（财报正文通常远大于普通公告）.
                    # 2026-08-13 NVO 修复: filing.size 在 edgar 4.x/5.x 均可拿.
                    "size": getattr(filing, "size", None)
                    or (len(filing.text or "") if hasattr(filing, "text") else None)
                    or 0,
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

        加 timing log (2026-07-16, plan: earnings-download-failure-handling)：
        - 记录 edgar init / search / sec_api 各阶段耗时
        - 解决“downloader 120s 讴 “unknown error” 问题：能定位是哪一步慢
        """
        # 应用速率限制
        self._rate_limiter.wait("edgar_search")
        self._rate_limiter.wait("sec_api_search")

        last_error = None
        _t_search = time.monotonic()

        # 优先使用edgartools (免费)，带重试
        for attempt in range(self.MAX_RETRIES):
            try:
                _t_init = time.monotonic()
                init_ok = self._init_edgar()
                _dt_init = time.monotonic() - _t_init
                if init_ok:
                    _t_edgar = time.monotonic()
                    results = self._search_edgar(ticker, form_type, year, size)
                    _dt_edgar = time.monotonic() - _t_edgar
                    logger.info(
                        f"[m_stock timing] search edgar ok: ticker={ticker} form={form_type} "
                        f"year={year} init={_dt_init:.2f}s search={_dt_edgar:.2f}s "
                        f"results={len(results)} total={time.monotonic()-_t_search:.2f}s"
                    )
                    return results
            except Exception as e:
                last_error = e
                logger.warning(
                    f"edgartools搜索失败 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e} "
                    f"[m_stock timing] elapsed={time.monotonic()-_t_search:.2f}s"
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
        report_period: Optional[str] = None,
    ) -> DownloadResult:
        """下载指定类型SEC文档的通用方法

        W37-#50c: 若 SEC EDGAR (_search_filings) 仍 Company not found
        (NTDOY 任天堂跟 HESAY 爱马仕同样走 adr_map.use_sec_20f_only 但
        edgartools/sec-api 都不认的 OTC ADR), 在 2 个出口 (NetworkError
        raise + NO_FILINGS_FOUND 空返) 都试 adr_map.custom_ir_source
        公开 PDF (W37-#50c NTDOY 240507e.pdf 977 KB 真任天堂 FY24 Q4
        Consolidated Results). 跟 W36 PR #40 同样 1 file fix 模式
        (不修 edgartools/sec-api, 加 fallback).
        """
        ticker = code.upper()

        # W37-#50c: helper 集中处理 SEC fail 后走 custom_ir_source
        def _try_ir_fallback() -> "DownloadResult":
            ir = self._try_custom_ir_source(ticker, year, form_type, checkpoint)
            if ir is not None:
                return ir
            # 没 custom_ir_source mapping, 返原始 error
            return DownloadResult(
                success=False,
                error_code="NO_FILINGS_FOUND",
                error_message=f"未找到 {ticker} {year or ''} 的{form_type}文件",
            )

        try:
            # W39 8-9 RACE 修复: 6-K 是"封面+exhibit"结构, 同一公司一年有几十条 6-K
            # (普通 PR / 董事会变动 / 财报). 若只取 size=1 (最新一条), 可能取到普通 PR
            # (如 RACE 7-31 fnvbb310726prex.htm 3KB), 而非 Q2 财报 (7-30 interim report
            # 209KB 含 revenue/EBITDA). 所以对 6-K 多取几条, 选 exhibit 含财报关键词的.
            size = 10 if form_type == "6-K" else 1
            filings = self._search_filings(ticker, form_type, year, size=size)

            if not filings:
                return _try_ir_fallback()

            filing = filings[0]
            if form_type == "6-K" and len(filings) > 1:
                picked = self._pick_earnings_6k(filings, report_period=report_period)
                if picked is not None:
                    filing = picked
                else:
                    # 2026-08-13 NVO 修复: _pick_earnings_6k 对“单文档 6-K”
                    # (NVO 等, 正文=primary doc, 无独立 EX-99 exhibit) 无 exhibit
                    # 可打分 → 返回 None → 旧逻辑回退 filings[0] (最新一条),
                    # 会抓到普通公告 (NVO 8-10 股票回购 24KB) 而非 8-4 完整财报
                    # (caq22026.htm 1.73MB). 改为选 size 最大的 filing (财报正文
                    # 通常远大于普通公告/回购公告), 避免 SUSPICIOUS_TOO_SMALL.
                    best = max(
                        filings, key=lambda f: int(f.get("size") or 0)
                    )
                    if int(best.get("size") or 0) > int(filing.get("size") or 0):
                        logger.info(
                            f"[m_stock] 6-K 无财报 exhibit, 选 size 最大 filing: "
                            f"{best.get('accessionNo')} size={best.get('size')} "
                            f"(替代 filings[0] {filing.get('accessionNo')} "
                            f"size={filing.get('size')})"
                        )
                        filing = best

            return self._download_filing(
                filing, ticker, form_type, year, on_progress, checkpoint
            )

        except NetworkError as e:
            # W37-#50c: SEC EDGAR 跟 sec-api 都不可用 (Company not found
            # / network / API key), 试 custom_ir_source 公开 IR PDF
            logger.info(
                f"{ticker} {form_type} SEC 不可用 ({e.error_code}: {e}), "
                f"尝试 custom_ir_source fallback (W37-#50c)"
            )
            ir = self._try_custom_ir_source(ticker, year, form_type, checkpoint)
            if ir is not None:
                return ir
            return DownloadResult(
                success=False, error_code=e.error_code, error_message=str(e)
            )
        except Exception as e:
            return DownloadResult(
                success=False, error_code="DOWNLOAD_ERROR", error_message=str(e)
            )

    _EARNINGS_KEYWORDS = (
        "revenue", "net income", "gross profit", "earnings per share",
        "diluted", "EBITDA", "operating income", "financial results",
        "second quarter", "third quarter", "fourth quarter", "first quarter",
    )
    # 文件名特征词: 财报型 exhibit 文件名常含这些 (RACE: fnvq22026results /
    # ferrarinvinterimreport-063; 普通 PR 是 fnvbb...prex)
    _FILE_EARNINGS_KEYWORDS = (
        "results", "interim", "quarter", "annual", "report", "earnings",
        "financial", "fy", "semiannual", "half-year", "halfyear", "semi",
    )

    def _pick_earnings_6k(
        self,
        filings: List[Dict[str, Any]],
        report_period: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """从多条 6-K 中选财报特征最强的.

        W39 8-9 RACE 修复: 6-K 是"封面+exhibit"结构, 同一年几十条 6-K
        (普通 PR / 董事会变动 / 财报). 仅凭日期无法区分. 每条 6-K 的 exhibit
        (EX-99) 才是真内容. 选 exhibit 文件名+描述含财报关键词最多的那条;
        若没有, 选 exhibit 最多的那条; 仍没有回退 None (调用方用第一条).

        XNET 8-14 修复: 部分公司 (如迅雷 XNET) 的 6-K exhibit 描述全是通用
        "EXHIBIT 99.1", document 全是 tmXXX_ex99-1.htm, 不含任何财报关键词
        → 所有 filing 打分都是 0 → 回退按 size 选最大, 但 size 最大不一定是
        目标季度 (XNET Q2 8-13 发布 size=206088, Q4+FY2025 3-12 发布
        size=244676 更大 → 误选 Q4).

        修复策略 (两层):
        1. 若传了 report_period (如 "2026Q2") 且能解析出目标季度 → 用
           filedAt 日期窗口优先匹配: 目标季度结束日 + [EARLY_DAYS, LATE_DAYS]
           天窗口内的 filing 优先. 窗口内选 exhibit 打分最高的; 若窗口内
           全无 exhibit 可打分, 选窗口内 size 最大的. 这保证 XNET 选到
           8-13 的 Q2 (窗口 ~7-30~9-18) 而不是 3-12 的 Q4.
        2. 无 report_period 或窗口无命中 → 回退旧逻辑 (打分 → None).
        """
        # 目标季度窗口 (季度结束后第 N~M 天发布财报)
        QUARTER_EARLY_DAYS = 25   # 财报最早可能在季度结束后 ~25 天发布
        QUARTER_LATE_DAYS = 85    # 最晚在 ~85 天内 (跨季末但未到下一季末)

        # 解析 report_period → 目标季度结束日
        target_end: Optional[datetime.date] = None
        if report_period:
            try:
                period = report_period.upper()  # e.g. 2026Q2 / 2026FY / 2026H1
                m = re.match(r"(\d{4})(?:[QH]([1-4]))?", period)
                if m:
                    y = int(m.group(1))
                    q = m.group(2)
                    if q:
                        month_end = {"1": 3, "2": 6, "3": 9, "4": 12}[q]
                        # off-by-1 修复 (PR #48 review 8-14): 3月=31天, 12月=31天,
                        # 不能硬编码 day=30 (Q1 会得 Mar30 而非 Mar31, Q4 得 Dec30 而非 Dec31)
                        last_day = calendar.monthrange(y, month_end)[1]
                        target_end = datetime.date(y, month_end, last_day)
                    else:
                        # FY 无明确季度 → 用 12-31 (年度报告)
                        target_end = datetime.date(y, 12, 31)
            except Exception:
                target_end = None

        def _filed_at_date(f: Dict[str, Any]) -> Optional[datetime.date]:
            raw = f.get("filedAt") or f.get("filing_date") or ""
            if isinstance(raw, datetime.date):
                return raw
            try:
                return datetime.date.fromisoformat(str(raw)[:10])
            except (ValueError, TypeError):
                return None

        def _exhibit_score(f: Dict[str, Any]) -> int:
            """计算 filing 的 exhibit 财报特征分 (旧逻辑)."""
            score = 0
            for ex in f.get("_exhibits", []):
                desc = str(ex.get("description", "")) or ""
                doc = str(ex.get("document", "")) or ""
                blob = (desc + " " + doc).lower()
                score += sum(1 for kw in self._EARNINGS_KEYWORDS if kw in blob)
                score += sum(1 for kw in self._FILE_EARNINGS_KEYWORDS if kw in blob)
                ex_size = ex.get("size_bytes", 0) or 0
                if ex_size:
                    score += 1 if ex_size > 100_000 else 0
            return score

        # ── 策略 1: 目标季度日期窗口优先 ──
        if target_end is not None:
            window_lo = target_end + datetime.timedelta(days=QUARTER_EARLY_DAYS)
            window_hi = target_end + datetime.timedelta(days=QUARTER_LATE_DAYS)
            in_window = []
            for f in filings:
                d = _filed_at_date(f)
                if d is None:
                    continue
                if window_lo <= d <= window_hi:
                    in_window.append(f)
            if in_window:
                logger.info(
                    f"[m_stock] 6-K 目标季度窗口 {window_lo}~{window_hi} "
                    f"命中 {len(in_window)}/{len(filings)} 条: "
                    + ", ".join(f.get('accessionNo','?') for f in in_window)
                )
                # 窗口内优先选 exhibit 打分最高; 全 0 分则选 size 最大
                best_in = None
                best_score_in = -1
                for f in in_window:
                    s = _exhibit_score(f)
                    if s > best_score_in:
                        best_score_in = s
                        best_in = f
                if best_score_in > 0:
                    return best_in
                # 全无 exhibit 可打分 → 选窗口内 size 最大
                best_in = max(
                    in_window, key=lambda f: int(f.get("size") or 0)
                )
                logger.info(
                    f"[m_stock] 6-K 窗口内无 exhibit 财报分, 选 size 最大: "
                    f"{best_in.get('accessionNo')} size={best_in.get('size')}"
                )
                return best_in

        # ── 策略 2: 旧逻辑 (exhibit 打分, 无窗口) ──
        best = None
        best_score = -1
        for filing in filings:
            exhibits = filing.get("_exhibits", [])
            if not exhibits:
                continue
            score = _exhibit_score(filing)
            if score > best_score:
                best_score = score
                best = filing
        return best if best_score > 0 else None

    def _try_custom_ir_source(
        self,
        ticker: str,
        year: Optional[int],
        form_type: str,
        checkpoint: Optional[Dict[str, Any]],
    ) -> Optional["DownloadResult"]:
        """若 ticker 在 adr_map.custom_ir_source, 走 IR 公开 PDF.

        W37-#50c: NTDOY 任天堂 240507e.pdf 977 KB 100% 验过真财报.
        HESAY 不在此 (走 AMF BALO 计划, 属 #50b 范围, 不在 #50c 修).

        Returns:
            DownloadResult on success, None when no custom_ir_source
            mapping exists (so the caller can fall through to the
            original NO_FILINGS_FOUND return path).
        """
        try:
            from unified_downloader.utils.adr_map import load_adr_map, get_custom_ir_source
            adr_map = load_adr_map()
            ir_info = get_custom_ir_source(ticker, adr_map)
        except Exception as e:
            logger.debug(f"adr_map custom_ir_source lookup failed for {ticker}: {e}")
            return None

        if not ir_info:
            return None

        ir_base = ir_info.get("ir_base_url", "").rstrip("/")
        pdf_lang = ir_info.get("lang", "e")
        verified = ir_info.get("verified_pdfs", {})
        if not ir_base or not verified:
            return None

        # W37-#50c: 优先 verified_pdfs 匹配的 (YYMMDD -> URL), 跟
        # 7-19 港股 source 同样“公开 PDF 直链 + 命名规则推断” 模式.
        # 按 year 过滤: year=None 走最新一个; year=2024 只试 2024 公布日.
        # W37-#50b: verified_pdfs value 支持两种格式:
        #   - str: 跟 NTDOY 同样 URL 拼凑 (ir_base/{yyyy}/{yyyymmdd}{lang}.pdf)
        #   - dict: {"url": "...", "size": "...", "note": "..."} 直接拿 url
        #           用于 HESAY 这种 URL pattern 不规则 (VersionId + assets CDN) 的源.
        candidates = []
        sorted_verified = sorted(verified.items(), reverse=True)  # 倒序, 最新优先
        for publish_date, info in sorted_verified:
            yyyy = publish_date[:4]
            if year is None or yyyy == str(year) or yyyy == str(year + 1):
                if isinstance(info, dict):
                    url = info.get("url", "")
                    note = info.get("note", "")
                    if url:
                        candidates.append((url, note))
                else:
                    yyyymmdd = publish_date.replace("-", "")[2:]  # 2024-05-07 -> 240507
                    candidates.append((f"{ir_base}/{yyyy}/{yyyymmdd}{pdf_lang}.pdf", info))

        # 最后一手 fallback: 跟年度不匹配时, 试通用 YYMMDD (240507 Q4 / 241105 Q2 / 250204 Q3)
        if year and not candidates:
            yy = year % 100
            yy_next = (year + 1) % 100
            yy_prev = (year - 1) % 100
            for yyyy, yymmdd_list in [
                (str(year), [f"{yy:02d}0507", f"{yy:02d}1105", f"{yy:02d}0204"]),
                (str(year + 1), [f"{yy_next:02d}0507", f"{yy_next:02d}1105", f"{yy_next:02d}0204"]),
            ]:
                for yymmdd in yymmdd_list:
                    candidates.append((f"{ir_base}/{yyyy}/{yymmdd}{pdf_lang}.pdf", "inferred"))

        for url, info_str in candidates:
            try:
                logger.info(f"{ticker} trying custom_ir_source PDF: {url} ({info_str})")
                result = self._download_pdf_direct(
                    url=url, ticker=ticker, year=year, source_label="custom_ir_source"
                )
                if result.success:
                    logger.info(f"{ticker} custom_ir_source hit: {url} ({result.file_size} bytes)")
                    return result
            except Exception as e:
                logger.debug(f"{ticker} custom_ir_source URL {url} failed: {e}")
                continue

        logger.warning(f"{ticker} custom_ir_source exhausted {len(candidates)} candidates, none hit")
        return None

    def _download_pdf_direct(
        self,
        url: str,
        ticker: str,
        year: Optional[int],
        source_label: str,
    ) -> "DownloadResult":
        """下载公开 IR PDF URL 到 downloads/ 目录, 返回 DownloadResult.

        跟 _download_filing 同样 HTTP 路径, 但不依赖 edgartools/filing dict.
        W37-#50c: 任天堂 240507e.pdf 977 KB 验过真财报.
        """
        from unified_downloader.models.entities import DownloadResult, DataSource
        from datetime import date

        try:
            ext = ".pdf"
            file_path = self._build_file_path(
                ticker, year or date.today().year, source_label, ext
            )
            file_path.parent.mkdir(parents=True, exist_ok=True)

            # W37-#50c: 跟 m_stock 现有 download_file 同样用法 (line 810
            # self._http_client.download_file(url, file_path, headers))
            download_result = self._http_client.download_file(
                url=url, file_path=str(file_path)
            )
            file_size = (
                download_result.get("file_size", 0)
                if isinstance(download_result, dict)
                else 0
            )

            if file_size < 1000:
                # <1KB 可能是 404 页面/redirect, 不算真抓
                return DownloadResult(
                    success=False,
                    error_code="PDF_TOO_SMALL",
                    error_message=f"{url} returned {file_size} bytes (< 1KB, likely 404 page)",
                )

            # W40-#50: >1KB 的品牌化 404/HTML 错误页之前被当 PDF success 入库。
            # 校验 %PDF- magic header（PDF spec 允许 header 出现在前 1024 字节内，
            # 兼容前面有少量垃圾字节的合法 PDF）。
            try:
                with open(file_path, "rb") as f:
                    head = f.read(1024)
            except OSError as read_err:
                return DownloadResult(
                    success=False,
                    error_code="CUSTOM_IR_DOWNLOAD_ERROR",
                    error_message=f"{url} read-back failed: {read_err}",
                )
            if b"%PDF-" not in head:
                file_path.unlink(missing_ok=True)
                return DownloadResult(
                    success=False,
                    error_code="PDF_INVALID_CONTENT",
                    error_message=(
                        f"{url} returned non-PDF content "
                        f"(no %PDF- header in first 1024 bytes, likely HTML error page)"
                    ),
                )

            return DownloadResult(
                success=True,
                file_path=str(file_path),
                file_size=file_size,
                source="custom_ir_source",  # 跟 m_stock 现有 source="edgar" 同样用 string
                metadata={"source": source_label, "url": url, "ticker": ticker, "year": year},
            )
        except Exception as e:
            return DownloadResult(
                success=False,
                error_code="CUSTOM_IR_DOWNLOAD_ERROR",
                error_message=f"{url} download failed: {e}",
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

        W36: edgartools Company() 对 OTC ADR (Hermes / LVMH 等) 抛
        "Company not found" 之前根本到不了 is_foreign 判断, 所以单靠
        edgartools FPI check 永远返回 10-K, 然后下载 10-K 又失败.
        我们不修 edgartools, 而是看 adr_map.use_sec_20f_only: 命中 → 20-F
        (即便 edgartools 找不到 ticker, 也至少让 m_stock 走对的 form,
        错误信息变为 "20-F not found" 而不是 "10-K not found", 暴露真因).

        use_hk_source (TCEHY/BEKE/HTHT/MNSO) 不在这里处理 — 这些 ticker
        根本不该走 m_stock, 应在上层 dispatch 改走 h_stock. 这里是
        SEC form type 决策, 不是 market 决策.

        作为 USFormResolver 协议成员被 UnifiedDownloader 跨模块调用。
        """
        # W36: adr_map.use_sec_20f_only 优先, 因为 edgartools 对
        # 不在 SEC EDGAR 的 OTC ADR 抛 "Company not found" 之前到不了
        # is_foreign 判断. 即便 adr_map 命中, 后面 _download_form 仍
        # 会失败, 但失败信息从 "primary=10k not found" 变成
        # "primary=20f not found", 正确暴露真因 (不是 SEC 源).
        from unified_downloader.utils.adr_map import load_adr_map, resolve_target
        adr_map = load_adr_map()
        _, _, _, is_20f_only = resolve_target(code, "US", adr_map)
        if is_20f_only:
            logger.info(f"{code} is in adr_map.use_sec_20f_only, using 20-F for annual report")
            return "20-F"

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

        W36: 同 annual, adr_map.use_sec_20f_only 优先 → 6-K.
        use_hk_source 不在这里处理 (跟 get_annual_form_type 同样理由:
        market dispatch 是上层的事, 这里是 SEC form type 决策).

        作为 USFormResolver 协议成员被 UnifiedDownloader 跨模块调用。
        """
        # W36: 跟 annual 同步, adr_map.use_sec_20f_only 优先.
        from unified_downloader.utils.adr_map import load_adr_map, resolve_target
        adr_map = load_adr_map()
        _, _, _, is_20f_only = resolve_target(code, "US", adr_map)
        if is_20f_only:
            logger.info(f"{code} is in adr_map.use_sec_20f_only, using 6-K for quarterly report")
            return "6-K"

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
        report_period: Optional[str] = None,
    ) -> DownloadResult:
        """下载6-K报告（含展品合并）

        report_period: 目标报告期, 如 "2026Q2". 传参时 _download_form 会用
        目标季度日期窗口优先选对应财报 6-K (XNET 修复, 8-14), 避免 exhibit
        描述都是通用 "EXHIBIT 99.1" 时按 size 选到其他季度.
        """
        return self._download_form(
            code, "6-K", year, checkpoint, on_progress,
            report_period=report_period,
        )

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

        加 timing log (2026-07-16, plan: earnings-download-failure-handling)：
        - 记录每个 exhibit 下载耗时
        - 超时可以定位是哪条 6-K 的哪个 exhibit 卡
        """
        _t_merge_start = time.monotonic()
        exhibit_files = [main_file]  # Start with the cover

        for i, ex in enumerate(exhibits):
            ex_url = ex.get("url", "")
            ex_desc = ex.get("description", f"exhibit_{i}")
            if not ex_url:
                continue

            # Build exhibit file path
            ex_path = main_file.parent / f"{ticker}_{file_year}_{form_type}_ex{i}.html"

            _t_ex = time.monotonic()
            try:
                self._rate_limiter.wait("edgar_download")
                logger.info(f"Downloading 6-K exhibit {i}: {ex_desc[:80]}")
                
                ex_result = self._http_client.download_file(
                    url=ex_url, file_path=str(ex_path), headers=headers
                )
                _dt_ex = time.monotonic() - _t_ex
                if os.path.exists(str(ex_path)) and os.path.getsize(str(ex_path)) > 100:
                    exhibit_files.append(ex_path)
                    logger.info(
                        f"  → {ex_path} ({os.path.getsize(str(ex_path))} bytes) "
                        f"[m_stock timing] exhibit[{i}] {ex_desc[:30]}={_dt_ex:.2f}s"
                    )
                else:
                    logger.debug(
                        f"  → Exhibit {i} too small or missing, skipped "
                        f"[m_stock timing] exhibit[{i}]={_dt_ex:.2f}s"
                    )
            except Exception as e:
                _dt_ex = time.monotonic() - _t_ex
                logger.warning(
                    f"  → Exhibit {i} download failed: {e} "
                    f"[m_stock timing] exhibit[{i}]={_dt_ex:.2f}s"
                )

        _dt_total = time.monotonic() - _t_merge_start
        logger.info(
            f"[m_stock timing] _merge_6k_exhibits done: "
            f"{len(exhibit_files)-1}/{len(exhibits)} exhibits ok total={_dt_total:.2f}s"
        )
        
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

    def _embed_images_as_base64(self, html_path: Path, base_url: str, headers: Dict[str, str]):
        """解析HTML中的相对路径图片，下载并嵌入为base64 data URI，保证单文件HTML自带所有图片。

        加并发超时隔离 (2026-07-16, plan: earnings-download-failure-handling)：
        - 原始实现对每张图逐个下载，ASML 6-K 主页有 30+ 张 logo/装饰图
          每张都 30s timeout，总体 15+ 分钟，下载阶段必 hang
        - 修复：ThreadPoolExecutor 并发下载 + 整体 timeout 20s
        - 失败的图片原样保留相对路径（不会用 base64 替代），下次再试
        """
        import re
        import base64
        import mimetypes
        from urllib.parse import urljoin
        from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
        import requests

        _t_start = time.monotonic()
        _EMBED_TIMEOUT_S = 20  # 整体 embed 不能超过 20s

        content = html_path.read_text(encoding="utf-8", errors="replace")
        img_pattern = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

        base_dir = base_url.rsplit("/", 1)[0] + "/"

        # 收集所有需要下载的图片
        tasks = []
        for match in img_pattern.finditer(content):
            img_src = match.group(1)
            if img_src.startswith(("data:", "http://", "https://", "#", "javascript:")):
                continue
            img_url = urljoin(base_dir, img_src)
            tasks.append((match, img_src, img_url))

        if not tasks:
            return

        # 并发下载，每张图 8s timeout
        def fetch_one(args):
            match, img_src, img_url = args
            try:
                resp = requests.get(img_url, headers=headers, timeout=8)
                if resp.status_code != 200:
                    return (match, img_src, None, None, f"HTTP {resp.status_code}")
                content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
                if not content_type:
                    content_type, _ = mimetypes.guess_type(img_src)
                    content_type = content_type or "image/jpeg"
                img_b64 = base64.b64encode(resp.content).decode("ascii")
                return (match, img_src, content_type, img_b64, None)
            except Exception as e:
                return (match, img_src, None, None, str(e))

        results = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_task = {executor.submit(fetch_one, t): t for t in tasks}
            try:
                for fut in as_completed(future_to_task, timeout=_EMBED_TIMEOUT_S):
                    try:
                        result = fut.result(timeout=1)
                    except FuturesTimeout:
                        continue
                    except Exception:
                        continue
                    if result:
                        match, img_src, content_type, img_b64, err = result
                        if img_b64 is not None:
                            results[img_src] = (match, content_type, img_b64)
            except FuturesTimeout:
                logger.warning(
                    f"[m_stock timing] _embed_images_as_base64 overall timeout {_EMBED_TIMEOUT_S}s, "
                    f"embedded {len(results)}/{len(tasks)}"
                )

        # 替换 src
        modified = False
        for img_src, (match, content_type, img_b64) in results.items():
            data_uri = f"data:{content_type};base64,{img_b64}"
            old_tag = match.group(0)
            new_tag = old_tag.replace(f'src="{img_src}"', f'src="{data_uri}"').replace(f"src='{img_src}'", f"src='{data_uri}'")
            content = content.replace(old_tag, new_tag)
            modified = True

        if modified:
            html_path.write_text(content, encoding="utf-8")
        logger.info(
            f"[m_stock timing] _embed_images_as_base64 done: "
            f"{len(results)}/{len(tasks)} images embedded total={time.monotonic()-_t_start:.2f}s"
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

        # SEC要求特定User-Agent头，优先级: 构造参数 > 配置文件 > 环境变量 > 默认值
        if self._sec_user_agent:
            sec_ua = self._sec_user_agent
        else:
            from unified_downloader.core.config import get_default_config
            cfg = get_default_config()
            # W36: 以前用 `os.environ.get("USER") + "..."`, 如果 USER env 在
            # 子进程里被 strip (e.g. OpenClaw cron wrapper, systemd unit),
            # os.environ.get("USER") 返回 None → None + str → TypeError.
            # META 26Q2 7-29 早上报这个错, download_status=FAILED.
            # 修: `or "Unknown"` 兜底, 跟 m_stock 其他 getattr 模式一致.
            user = os.environ.get("USER") or "Unknown"
            sec_ua = cfg.sec_user_agent or os.environ.get(
                "SEC_USER_AGENT", f"{user}/contact@example.com Research Tool/1.0"
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
                            merged = self._merge_6k_exhibits(
                                downloaded_path, exhibits, ticker, file_year,
                                form_type, headers
                            )
                            # W39 8-9 RACE 修复: merge 返回的才是含财报正文的文件,
                            # 必须同步 result["file_path"] (否则 return 仍指向封面 15KB)
                            if merged and merged != downloaded_path:
                                downloaded_path = merged
                                result["file_path"] = str(merged)
                                result["file_size"] = merged.stat().st_size
                        except Exception as e:
                            logger.warning(f"6-K exhibit merge failed, using cover only: {e}")

                # 嵌入所有相对路径图片为base64 data URI，保证单文件HTML自带图片，上传IMA不会图挂
                # 2026-07-21 fix: 招股书 (S-1/F-1/424B4 等) 跳过 embed —— 招股书图片
                # 嵌 base64 后 8.5M → 51M，触发 IMA 后端不索引 body content。
                # 招股书图片装饰性多、重要性低，跳过 embed 反而可用。
                # 6-K/10-K/10-Q 仍保留 embed (图片一般是 logo/财务图，实用价值高)
                if downloaded_path.suffix.lower() in (".html", ".htm"):
                    if form_type in self._PROSPECTUS_FORMS:
                        logger.info(
                            f"[m_stock] 招股书 {form_type} 跳过 embed base64: {downloaded_path.name} "
                            f"原因=避免文件膨胀触发 IMA 不索引"
                        )
                    else:
                        try:
                            self._embed_images_as_base64(downloaded_path, link, headers)
                            logger.debug(f"Embedded relative images into {downloaded_path.name}")
                        except Exception as e:
                            logger.warning(f"图片嵌入处理失败，保留原始HTML: {e}")

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

    # 招股书 form 类型集合（不嵌 base64 图片，避免 IMA 不索引大文件）
    # 招股书 (S-1/F-1/424B4) 通常含 30+ 张 logo/财务图表装饰图，
    # 嵌 base64 后文件膨胀 6x+ (8.5M → 51M)，
    # 触发 IMA 后端不索引 body content（仅 record 元数据）
    # 招股书图片重要性低，跳过 embed 直接传主 HTML 即可
    _PROSPECTUS_FORMS = frozenset({"S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B3", "424B5"})

    # W40-#50: download()/async_download() 共用的 doc_type 分发表（防 sync/async drift）。
    # 回归背景: quarterly/interim_report/q1_report/q3_report 是 CLI -t 允许的类型,
    # 之前在 sync 分发里落到 else 分支被当 10-K 年报下载（要季报给年报, 无告警）;
    # annual_report 是 CLI/MCP 的默认类型, 必须显式映射而不是靠 else 兜底。
    _ANNUAL_DOC_TYPES = frozenset({"10k", "ten_k", "annual_report"})
    _QUARTERLY_DOC_TYPES = frozenset(
        {"10q", "ten_q", "quarterly", "q1_report", "q3_report", "interim_report"}
    )
    _PROSPECTUS_DOC_TYPES = frozenset({"s1", "s1a", "f1", "424b4", "prospectus"})
    _TWENTY_F_DOC_TYPES = frozenset({"20f", "20-f", "twenty_f"})

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
