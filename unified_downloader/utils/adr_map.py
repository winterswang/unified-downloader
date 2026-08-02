"""ADR map loading and lookup helpers.

The single source of truth for ticker-level data source overrides lives in
``config/adr_map.json``. It declares three routing categories used across
the project:

* ``use_hk_source`` — ADR that has a primary HK listing (TCEHY/BEKE/HTHT/
  MNSO). When a US ticker is in this set, m_stock should refuse to call
  SEC EDGAR and instead route the request to ``h_stock`` using the
  ``hk_code`` field.
* ``use_sec_20f_only`` — Foreign Private Issuer (FPI) ADRs that are NOT
  present in SEC EDGAR (HESAY/NTDOY/NVO/ASML/TSM/RACE). The right SEC
  form is 20-F (annual) / 6-K (quarterly); m_stock's default
  ``primary=10k/10q`` lookup will always fail for these tickers because
  ``edgar.Company(code)`` raises "Company not found" before the FPI
  check runs.
* ``dedup_skip_us`` — ADR whose HK source is already covered by another
  earnings entry (TCEHY/BEKE/HTHT). The bulk_download path silently
  drops these from the US run to avoid double work.

``scripts/bulk_download.py`` previously inlined ``load_adr_map`` /
``_us_key_variants`` / ``resolve_target``. The same logic is now exposed
here so m_stock (single-stock path) and the bulk script can share one
implementation.

Lookup keys: ``HESAY``, ``HESAY.US`` and ``hesay`` should all resolve to
the same row. ``_us_key_variants`` generates the three normalizations.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Project root: unified_downloader/utils/adr_map.py -> project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADR_MAP_FILE = _PROJECT_ROOT / "config" / "adr_map.json"


def load_adr_map(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load adr_map.json from disk.

    Returns an empty mapping (instead of raising) if the file is missing
    so callers don't have to guard against first-run / fresh-checkout
    scenarios.
    """
    target = path or ADR_MAP_FILE
    if not target.exists():
        logger.warning("adr_map.json not found at %s, returning empty map", target)
        return {}
    return json.loads(target.read_text(encoding="utf-8"))


def _us_key_variants(code: str) -> Tuple[str, str, str]:
    """Return the (code, base, base.US) lookup variants for a US ticker.

    Adapters historically accept tickers with or without the ``.US``
    suffix and sometimes normalize to uppercase. Generating all three
    keeps lookups robust regardless of caller convention.
    """
    cu = code.upper().strip()
    base = cu[:-3] if cu.endswith(".US") else cu
    return cu, base, f"{base}.US"


def _lookup_us_map(mapping: Dict[str, Any], code: str) -> Tuple[Optional[str], Any]:
    """Return ``(matched_key, value)`` for the first key variant present."""
    for key in _us_key_variants(code):
        if key in mapping:
            return key, mapping[key]
    return None, None


def resolve_target(
    code: str, market: str, adr_map: Dict[str, Any]
) -> Tuple[str, str, bool, bool]:
    """Decide where a US ticker should actually be downloaded from.

    Returns ``(download_code, download_market, is_adr_hk, is_20f_only)``:

    * ``is_adr_hk=True`` — caller should route the request to the HK
      adapter using ``download_code`` (5-digit numeric).
    * ``is_20f_only=True`` — caller should use SEC form 20-F (annual) /
      6-K (quarterly) instead of the default 10-K / 10-Q.
    * Both flags False — default US path with 10-K / 10-Q.

    For non-US markets the function is the identity: it returns the
    original code/market and both flags False. This keeps the function
    safe to call from generic dispatch sites without a market guard.
    """
    mkt = market.upper()
    if mkt != "US":
        return code, mkt, False, False

    key, info = _lookup_us_map(adr_map.get("use_hk_source", {}), code)
    if info:
        # ``info`` is a dict like {"hk_code": "0700", "name": "腾讯控股", ...}
        hk_code = info.get("hk_code", code)
        return hk_code, "HK", True, False

    key, info = _lookup_us_map(adr_map.get("use_sec_20f_only", {}), code)
    if info:
        base = code.upper()
        base = base[:-3] if base.endswith(".US") else base
        return base, "US", False, True

    return code, "US", False, False


def is_adr_skipped(code: str, adr_map: Dict[str, Any]) -> bool:
    """True if the US ticker is listed in ``dedup_skip_us``.

    Bulk download uses this to drop ADRs that are already covered by a
    separate HK earnings entry (e.g. TCEHY is in HK as 0700.HK, so the
    US run skips it). m_stock does not currently call this — the per-
    ticker download is a manual operator action where skipping without
    an explicit request would be surprising.
    """
    # dedup_skip_us is a list (not a dict) in adr_map.json, so we can't
    # reuse _lookup_us_map which does dict[key] lookups. Inline the
    # membership test with the same _us_key_variants normalization.
    for key in _us_key_variants(code):
        if key in adr_map.get("dedup_skip_us", []):
            return True
    return False


def get_custom_ir_source(code: str, adr_map: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the ``custom_ir_source`` entry for a US ticker, or None.

    W37-#50c (8-2) — for tickers that SEC EDGAR cannot find
    (e.g. NTDOY 任天堂 — Nintendo Co., Ltd. is only listed on TSE,
    not on US exchanges as a foreign private issuer with full 20-F
    coverage), the IR site publishes a public English PDF with a
    predictable ``{YYMMDD}{lang}.pdf`` naming scheme. The m_stock
    fallback path uses this entry to construct the real PDF URL.

    Lookup keys: NTDOY / NTDOY.US / ntsoy all resolve to the same row,
    matching the ``_us_key_variants`` convention used elsewhere in this
    module. Returns None when the ticker has no custom IR mapping so
    callers can fall through to existing SEC/sec-api logic.
    """
    key, info = _lookup_us_map(adr_map.get("custom_ir_source", {}), code)
    if info:
        return info
    return None
