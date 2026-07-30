"""Unit tests for unified_downloader.utils.adr_map.

Covers:
  * load_adr_map returns the on-disk JSON.
  * _us_key_variants normalizes HESAY/HESAY.US/hesay correctly.
  * resolve_target dispatches:
      - use_hk_source  → hk_code + HK + is_adr_hk=True
      - use_sec_20f_only → base ticker + US + is_20f_only=True
      - default US      → unchanged + both flags False
      - non-US markets  → identity (no rewriting)
  * is_adr_skipped: dedup_skip_us list membership with/without .US suffix.

The cases track the current adr_map.json shape (4 use_hk_source + 6
use_sec_20f_only + 3 dedup_skip_us). If adr_map.json grows, add the
new tickers here so regressions surface immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from unified_downloader.utils.adr_map import (
    ADR_MAP_FILE,
    _us_key_variants,
    is_adr_skipped,
    load_adr_map,
    resolve_target,
)


# A representative adr_map shape — keep this in sync with the real file.
FAKE_ADR_MAP = {
    "use_hk_source": {
        "TCEHY.US": {"hk_code": "0700", "name": "腾讯控股"},
        "BEKE.US":  {"hk_code": "2423", "name": "贝壳-W"},
        "HTHT.US":  {"hk_code": "1179", "name": "华住集团-S"},
        "MNSO.US":  {"hk_code": "9896", "name": "名创优品"},
    },
    "use_sec_20f_only": {
        "HESAY.US": "爱马仕(ADR)",
        "NTDOY.US": "任天堂(ADR)",
        "NVO.US":   "诺和诺德(ADR)",
        "ASML.US":  "阿斯麦",
        "TSM.US":   "台积电",
        "RACE.US":  "法拉利",
    },
    "dedup_skip_us": ["TCEHY.US", "BEKE.US", "HTHT.US"],
}


# ── load_adr_map ──────────────────────────────────────────────────────


def test_load_adr_map_returns_real_file():
    """The real config/adr_map.json must parse without errors."""
    data = load_adr_map()
    assert isinstance(data, dict)
    # 三个 key 必须存在 (跟 plan 7-30 W35 issue 文档一致)
    assert "use_hk_source" in data
    assert "use_sec_20f_only" in data
    assert "dedup_skip_us" in data


def test_load_adr_map_missing_file_returns_empty():
    """A missing adr_map.json should not raise — callers rely on this."""
    with patch.object(Path, "exists", return_value=False):
        data = load_adr_map()
    assert data == {}


def test_adr_map_file_path():
    """ADR_MAP_FILE should point at config/adr_map.json under project root."""
    assert ADR_MAP_FILE.name == "adr_map.json"
    assert ADR_MAP_FILE.parent.name == "config"


# ── _us_key_variants ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [
        ("HESAY", ("HESAY", "HESAY", "HESAY.US")),
        ("HESAY.US", ("HESAY.US", "HESAY", "HESAY.US")),
        ("hesay", ("HESAY", "HESAY", "HESAY.US")),
        ("tsla", ("TSLA", "TSLA", "TSLA.US")),
    ],
)
def test_us_key_variants_normalizes(code, expected):
    assert _us_key_variants(code) == expected


# ── resolve_target: use_hk_source ─────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected_code,expected_mkt,expected_hk,expected_20f",
    [
        ("TCEHY",     "0700", "HK", True,  False),
        ("TCEHY.US",  "0700", "HK", True,  False),
        ("BEKE.US",   "2423", "HK", True,  False),
        ("HTHT",      "1179", "HK", True,  False),
        ("MNSO.US",   "9896", "HK", True,  False),
    ],
)
def test_resolve_target_use_hk_source(code, expected_code, expected_mkt, expected_hk, expected_20f):
    dl_code, dl_mkt, is_hk, is_20f = resolve_target(code, "US", FAKE_ADR_MAP)
    assert (dl_code, dl_mkt, is_hk, is_20f) == (expected_code, expected_mkt, expected_hk, expected_20f)


# ── resolve_target: use_sec_20f_only ──────────────────────────────────


@pytest.mark.parametrize(
    "code,expected_code,expected_mkt,expected_20f",
    [
        ("HESAY",     "HESAY", "US", True),
        ("HESAY.US",  "HESAY", "US", True),  # .US 也要正确剥离
        ("NTDOY.US",  "NTDOY", "US", True),
        ("NVO",       "NVO",   "US", True),
        ("ASML.US",   "ASML",  "US", True),
        ("TSM",       "TSM",   "US", True),
        ("RACE.US",   "RACE",  "US", True),
    ],
)
def test_resolve_target_use_sec_20f_only(code, expected_code, expected_mkt, expected_20f):
    _, dl_mkt, is_hk, is_20f = resolve_target(code, "US", FAKE_ADR_MAP)
    assert is_hk is False
    assert (dl_mkt, is_20f) == (expected_mkt, expected_20f)
    assert dl_mkt == "US"


# ── resolve_target: default US (13 SUCCESS records) ──────────────────


@pytest.mark.parametrize(
    "code",
    [
        "AAPL", "MSFT", "TSLA", "NVDA", "GOOGL", "META", "AMZN",
        "NFLX",  "AMD",  "INTC", "ORCL", "CRM",  "ADBE",
    ],
)
def test_resolve_target_default_us_passthrough(code):
    """13 US 本土 ticker (跟 W34 SUCCESS records 一致) 必须完全 passthrough.

    这是 W36 PR #48 关键不 regress 约束. m_stock 走 default 10-K/10-Q 路径,
    不被 adr_map 改写.
    """
    dl_code, dl_mkt, is_hk, is_20f = resolve_target(code, "US", FAKE_ADR_MAP)
    assert (dl_code, dl_mkt, is_hk, is_20f) == (code, "US", False, False)


# ── resolve_target: non-US markets ───────────────────────────────────


@pytest.mark.parametrize(
    "code,market,expected_code,expected_mkt",
    [
        ("600519", "CN", "600519", "CN"),
        ("000933", "CN", "000933", "CN"),
        ("00700",  "HK", "00700",  "HK"),
        ("09988",  "HK", "09988",  "HK"),
    ],
)
def test_resolve_target_non_us_identity(code, market, expected_code, expected_mkt):
    """非 US market 必须 identity 透传, 任何 adr_map 都不改写."""
    dl_code, dl_mkt, is_hk, is_20f = resolve_target(code, market, FAKE_ADR_MAP)
    assert (dl_code, dl_mkt, is_hk, is_20f) == (expected_code, expected_mkt, False, False)


# ── is_adr_skipped ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [
        ("TCEHY",    True),   # dedup_skip_us 命中
        ("TCEHY.US", True),
        ("BEKE.US",  True),
        ("HTHT",     True),
        ("AAPL",     False),  # 不在 dedup_skip_us
        ("HESAY",    False),  # 在 use_sec_20f_only 但不在 dedup
        ("TSLA",     False),
    ],
)
def test_is_adr_skipped(code, expected):
    assert is_adr_skipped(code, FAKE_ADR_MAP) is expected


def test_is_adr_skipped_empty_map():
    assert is_adr_skipped("TCEHY", {}) is False


# ── 集成: 跟真实 adr_map.json 数量级 ──────────────────────────────────


def test_real_adr_map_has_expected_categories():
    """真实 adr_map.json 必须三类都有, 且数量级跟 W35 issue 文档一致."""
    data = load_adr_map()
    assert len(data.get("use_hk_source", {})) >= 4
    assert len(data.get("use_sec_20f_only", {})) >= 6
    assert len(data.get("dedup_skip_us", [])) >= 3
