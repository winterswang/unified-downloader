"""W36 PR #50a: EU adapter skeleton regression tests.

Covers the smallest useful surface of the new EU adapter:

* Market.E enum exists and parses from the string "e".
* EuStockAdapter inherits BaseStockAdapter and exposes ``market = Market.E``.
* EuStockAdapter.download / search / get_available_years raise
  ``NotImplementedError`` with a message that points at PR #50b
  (so any production caller that reaches the EU path fails loudly).
* UnifiedDownloader wires ``Market.E`` -> ``EuStockAdapter`` so
  ``-m e`` doesn't AttributeError in the CLI dispatch.

These tests do NOT exercise real EU data sources — that's PR #50b.
"""

from __future__ import annotations

import pytest

from unified_downloader.adapters import EuStockAdapter, BaseStockAdapter
from unified_downloader.adapters.eu_stock import EuStockAdapter as DirectImport
from unified_downloader.models.enums import Market


# ── Market.E enum ─────────────────────────────────────────────────────


def test_market_e_exists():
    assert Market.E.value == "e"


def test_market_e_parses_from_string():
    """CLI flag `-m e` must resolve to Market.E."""
    assert Market("e") is Market.E


# ── EuStockAdapter identity ───────────────────────────────────────────


def test_eu_adapter_inherits_base():
    assert issubclass(EuStockAdapter, BaseStockAdapter)


def test_eu_adapter_market_is_e():
    assert EuStockAdapter.market is Market.E


def test_eu_adapter_direct_import_matches_package_import():
    """The two import paths must resolve to the same class."""
    assert EuStockAdapter is DirectImport


# ── NotImplementedError surface ──────────────────────────────────────


def _make_adapter() -> EuStockAdapter:
    from unified_downloader.infra.http_client import HTTPClient
    return EuStockAdapter(http_client=HTTPClient(), datasources=[])


def test_eu_download_raises_not_implemented():
    a = _make_adapter()
    with pytest.raises(NotImplementedError) as exc:
        a.download(code="RMS.PA", year=2024, document_type="annual_report")
    assert "PR #50b" in str(exc.value)


def test_eu_search_raises_not_implemented():
    a = _make_adapter()
    with pytest.raises(NotImplementedError) as exc:
        a.search(code="RMS.PA", year=2024)
    assert "PR #50b" in str(exc.value)


def test_eu_get_available_years_raises_not_implemented():
    a = _make_adapter()
    with pytest.raises(NotImplementedError) as exc:
        a.get_available_years(code="RMS.PA")
    assert "PR #50b" in str(exc.value)


# ── Dispatch integration ─────────────────────────────────────────────


def test_unified_downloader_wires_market_e():
    """The downloader must register EuStockAdapter under Market.E so
    CLI ``-m e`` doesn't KeyError before reaching the NotImplementedError.

    This is a lightweight import + adapters-map check; we don't fire up
    a full UnifiedDownloader (the cache + circuit-breaker setup is heavy).
    """
    from unified_downloader.core.downloader import UnifiedDownloader
    # Inspect the source rather than constructing — constructing pulls in
    # the cache manager and config which need a real config file.
    src = open(UnifiedDownloader.__module__.replace(".", "/") + ".py").read()
    assert "Market.E:" in src, "UnifiedDownloader must dispatch Market.E"
    assert "EuStockAdapter(" in src, "UnifiedDownloader must instantiate EuStockAdapter"
