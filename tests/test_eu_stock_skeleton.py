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


def test_detect_market_recognises_eu_tickers():
    """W36 PR #50a follow-up: _detect_market must identify EU ticker format.

    EU tickers are 1-5 uppercase letters + '.' + 2-3 uppercase letters
    (e.g. RMS.PA, MC.PA, ASML.AS, RACE.MI, SAP.DE). Without this rule,
    -m e demo (which passes RMS.PA through _detect_market) raises
    MarketUnrecognizedError before the EU adapter is even invoked.
    """
    import re

    # Read the pattern from the source so the test stays in sync if the
    # regex is tightened later.
    from unified_downloader.core.downloader import UnifiedDownloader
    src = open(UnifiedDownloader.__module__.replace(".", "/") + ".py").read()
    # Extract the EU regex line.
    import re as _re
    m = _re.search(r"r\"(\^\[A-Z\]\{1,5\}\\\.\[A-Z\]\{2,3\}\$)\"", src)
    assert m, "EU ticker regex must be present in UnifiedDownloader._detect_market"
    pattern = m.group(1)
    for code in ["RMS.PA", "MC.PA", "ASML.AS", "RACE.MI", "OR.PA", "SAP.DE", "ENI.MI"]:
        assert _re.match(pattern, code), f"{code} should match the EU pattern"
    # Sanity: a malformed EU ticker should NOT match.
    assert not _re.match(pattern, "RMS.P"), "RMS.P is too short"
    assert not _re.match(pattern, "RMS.PAAX"), "RMS.PAAX is too long"
    # And existing CN / HK / US patterns must not collide.
    assert not _re.match(pattern, "600519"), "CN codes must not match EU"
    assert not _re.match(pattern, "AAPL"), "US tickers must not match EU"
    assert not _re.match(pattern, "00700"), "HK codes must not match EU"
