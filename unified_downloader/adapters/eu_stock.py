"""European stock adapter — skeleton (W36 PR #50a).

This file exists to unblock the Market.E enum, the dispatch path in
core/downloader.py, and the adr_map "use_eu_source" route added in
PR #48. The actual implementation is **not** in this PR.

## Why a skeleton?

W36 PR #50a is the smallest useful step toward letting HESAY and other
EU-listed names route through a real EU data source. Hermes (RMS.PA),
LVMH (MC.PA), ASML (ASML.AS), RACE (RACE.MI) and similar issuers don't
file with SEC EDGAR — they publish through AMF (France), Euronext
Live, and their own IR pages. Engineering a working EU adapter means:

1. Picking a data source strategy (company IR scrape vs. AMF DB vs.
   Euronext corporate-actions feed).
2. Reverse-engineering one ticker's IR page so the URL pattern is
   stable across the 2024-2026 filings we need.
3. Dealing with multi-language HTML (FR/NL/IT/DE) and PDF embeds.
4. Generalising to 5+ issuers so the adapter is worth running.

That is 1-2 days of careful work and belongs in a separate PR (W36
PR #50b). This skeleton is intentionally limited to:

* Inheriting BaseStockAdapter so the class can be imported.
* Raising ``NotImplementedError`` from every abstract method with a
  message that points at the next PR.

The dispatch wiring (core/downloader.py adds ``Market.E: EuStockAdapter``)
and the config default (core/config.py adds an ``amf`` data source for
EU) are also part of this PR so that ``-m e`` works in the CLI without
crashing — it just fails cleanly with a "PR #50b pending" message.

## What NOT to do here

* Don't add web scraping code that hasn't been verified against a real
  Hermes or LVMH page; the URL patterns change.
* Don't add a CI-only smoke test that hits live EURONEXT pages; the
  test environment doesn't have stable network.
* Don't extend the DocumentType enum — ANNUAL_REPORT / INTERIM_REPORT /
  PROSPECTUS already cover the EU use cases (HESAY 2026FY is annual).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from unified_downloader.adapters.base import BaseStockAdapter
from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult, DataSource
from unified_downloader.infra.http_client import HTTPClient, AsyncHTTPClient

logger = logging.getLogger(__name__)


class EuStockAdapter(BaseStockAdapter):
    """European stock adapter (W36 PR #50a skeleton).

    Dispatch and config are wired up in this PR so ``-m e`` works in
    the CLI; actual download logic is the next PR (W36 PR #50b).

    W36 context:
    * Hermes RMS.PA, LVMH MC.PA, ASML ASML.AS, RACE RACE.MI, etc.
    * Not in SEC EDGAR (these are primary EU listings, not US OTC ADRs).
    * Publish through AMF (France), Euronext Live, and company IR pages.
    """

    market: Market = Market.E  # 来自 Market 枚举, 已新加

    def __init__(
        self,
        http_client: HTTPClient,
        datasources: List[Dict[str, Any]],
    ):
        super().__init__(http_client, datasources)
        logger.info(
            "EuStockAdapter skeleton initialised (W36 PR #50a). "
            "Download is not yet implemented — see PR #50b."
        )

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
        """EU download — W36 PR #50b pending.

        Calling this is a programming error until the real EU source
        is wired up. We raise ``NotImplementedError`` so that any
        morning-brief trigger_download that reaches the EU path fails
        loudly instead of silently producing an empty / wrong file.
        """
        raise NotImplementedError(
            f"EU download for {code} {year} {document_type} is not "
            "implemented in W36 PR #50a. See PR #50b (planned W37) "
            "for the AMF / Euronext Paris integration."
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
        """Async EU download — W36 PR #50b pending."""
        raise NotImplementedError(
            f"EU async download for {code} {year} {document_type} is "
            "not implemented in W36 PR #50a. See PR #50b."
        )

    def search(
        self,
        code: str,
        year: Optional[int] = None,
        document_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """EU document search — W36 PR #50b pending."""
        raise NotImplementedError(
            f"EU search for {code} is not implemented in W36 PR #50a. "
            "See PR #50b."
        )

    def get_available_years(
        self,
        code: str,
        document_type: Optional[str] = None,
    ) -> List[int]:
        """EU available years — W36 PR #50b pending."""
        raise NotImplementedError(
            f"EU get_available_years for {code} is not implemented in "
            "W36 PR #50a. See PR #50b."
        )

    # ─────────────────────────────────────────────────────────────────
    # Below: small helpers that PR #50b will fill in. They live here
    # so the dispatch code in core/downloader.py has somewhere to call
    # without import-cycles.
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _placeholder_save_path(
        code: str, year: Optional[int], document_type: str, ext: str = ".html"
    ) -> Path:
        """Future: where PR #50b will save the downloaded EU document."""
        suffix = f"_{year}" if year else ""
        return Path("downloads") / "e" / code[:3] / f"{code}{suffix}_{document_type}{ext}"
