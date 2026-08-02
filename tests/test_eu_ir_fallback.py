"""W37-#50d: EU 4 ticker custom_ir_source fallback 回归测试.

W36 7-30 标的 "ASML + 4 EU ticker (LVMH/OR/SAN/AIR) 跨语种" 在 8-2 真验证:
  - ASML: 25.5 MB SEC 20-F (走 SEC 兜底已成功, 不需 custom_ir_source)
  - SNY (Sanofi): 18.7 MB SEC 20-F (走 SEC edgar 兜底)
  - LVMUY/LVMHF: LVMH SEC Company not found, 走 Prismic CMS 真源
  - LRLCY/LRLCF: L'Oréal SEC Company not found, 走 loreal-finance.com Drupal CMS
  - EADSY (Airbus): SEC 0 hits + WAF 强 (Incapsula 拦截), 跳过 (跟 7-22 教训 WAF 同样)

跟 W37-#50b (HESAY) + W37-#50c (NTDOY) 同样 verified_pdfs dict 模式 (URL 不可拼,
CDN 直链).
"""
import json
import sys
from pathlib import Path
import pytest

# adr_map.json 加载 (跟 W36 PR #43 同样 mode)
ADR_MAP_PATH = Path(__file__).parent.parent / "config" / "adr_map.json"


def load_adr_map():
    with open(ADR_MAP_PATH) as f:
        return json.load(f)


# === LVMH (Prismic CMS) ===

def test_lvmuy_in_custom_ir_source():
    """LVMUY.US 必须在 custom_ir_source 段, 走 Prismic CMS 真源."""
    adr_map = load_adr_map()
    cis = adr_map["custom_ir_source"]
    assert "LVMUY.US" in cis, "LVMUY.US 必须在 adr_map.custom_ir_source (W37-#50d)"
    lvmuy = cis["LVMUY.US"]
    assert lvmuy.get("prismic_cms") is True, "LVMH 走 Prismic CMS"
    assert "lvmh-com.cdn.prismic.io" in lvmuy["ir_base_url"], "LVMH Prismic CDN URL"
    assert len(lvmuy["verified_pdfs"]) >= 1, "LVMH 至少 1 个 verified PDF"


def test_lvmuy_verified_pdfs_use_prismic_url():
    """LVMH verified_pdfs 必须是 dict 格式, URL 走 Prismic CDN."""
    adr_map = load_adr_map()
    lvmuy = adr_map["custom_ir_source"]["LVMUY.US"]
    for date, info in lvmuy["verified_pdfs"].items():
        assert isinstance(info, dict), f"LVMH {date} 必须是 dict"
        assert "url" in info
        assert "lvmh-com.cdn.prismic.io" in info["url"], \
            f"LVMH {date} URL 必须在 Prismic CDN"


def test_lvmhf_alias_to_lvmuy():
    """LVMHF.US 跟 LVMUY.US 同样 (因为 SEC EDGAR 2 ticker 都 Company not found)."""
    adr_map = load_adr_map()
    assert "LVMHF.US" in adr_map["custom_ir_source"], "LVMHF 必须也在 (SEC 同样 Company not found)"
    lvmhf = adr_map["custom_ir_source"]["LVMHF.US"]
    assert lvmhf.get("prismic_cms") is True


# === L'Oréal (Drupal CMS) ===

def test_lrlcy_in_custom_ir_source():
    """LRLCY.US 必须在 custom_ir_source 段, 走 Drupal CMS 真源."""
    adr_map = load_adr_map()
    cis = adr_map["custom_ir_source"]
    assert "LRLCY.US" in cis, "LRLCY.US 必须在 adr_map.custom_ir_source (W37-#50d)"
    lrlcy = cis["LRLCY.US"]
    assert lrlcy.get("drupal_cms") is True, "L'Oréal 走 Drupal CMS"
    assert lrlcy.get("file_path_pattern") == "system/files/{YYYY-MM}/{filename}.pdf", \
        "L'Oréal Drupal system/files 真链"
    assert "loreal-finance.com" in lrlcy["ir_base_url"]


def test_lrlcy_verified_pdfs_use_drupal_url():
    """L'Oréal verified_pdfs 必须是 dict 格式, URL 走 loreal-finance.com system/files."""
    adr_map = load_adr_map()
    lrlcy = adr_map["custom_ir_source"]["LRLCY.US"]
    for date, info in lrlcy["verified_pdfs"].items():
        assert isinstance(info, dict)
        assert "url" in info
        assert "loreal-finance.com/system/files" in info["url"], \
            f"L'Oréal {date} URL 必须在 system/files 路径"


def test_lrlcf_alias_to_lrlcy():
    """LRLCF.US 跟 LRLCY.US 同样 (因为 SEC EDGAR 2 ticker 都 Company not found)."""
    adr_map = load_adr_map()
    assert "LRLCF.US" in adr_map["custom_ir_source"]


# === 跟 W37-#50b HESAY 同样兼容性 ===

def test_hesay_still_dict_format():
    """HESAY 仍走 dict 模式 (跟 W37-#50b PR #45 同样), 不被 #50d 改影响."""
    adr_map = load_adr_map()
    hesay = adr_map["custom_ir_source"]["HESAY.US"]
    for date, info in hesay["verified_pdfs"].items():
        assert isinstance(info, dict)
        assert "assets-finance.hermes.com" in info["url"]


def test_ntsoy_still_string_format():
    """NTDOY 仍走 string 模式 (跟 W37-#50c PR #44 同样)."""
    adr_map = load_adr_map()
    ntsoy = adr_map["custom_ir_source"]["NTDOY.US"]
    for date, info in ntsoy["verified_pdfs"].items():
        assert isinstance(info, str)
        assert ".pdf" in info


# === 跟 W36 PR #40 同样 EU ticker 兼容性 ===

def test_use_sec_20f_only_still_marks_eu_tickers():
    """use_sec_20f_only 仍标 EU ticker, custom_ir_source 是 #50d 新 fallback."""
    adr_map = load_adr_map()
    sec20f = adr_map["use_sec_20f_only"]
    # 6 EU ticker 仍在 use_sec_20f_only
    for t in ["HESAY.US", "NTDOY.US", "ASML.US", "TSM.US", "RACE.US"]:
        assert t in sec20f, f"{t} 必须在 use_sec_20f_only (跟 W36 PR #40 同样)"


# === ASML 不需改 (走 SEC 兜底已成功) ===

def test_asml_not_in_custom_ir_source():
    """ASML 不在 custom_ir_source (因为 SEC 20-F 兜底已成功)."""
    adr_map = load_adr_map()
    cis = adr_map["custom_ir_source"]
    assert "ASML.US" not in cis, "ASML 不需 custom_ir_source (SEC 20-F 25.5 MB 走 SEC 兜底)"


# === Sanofi/SNY 走 SEC 兜底 (跟 ASML 同样) ===

def test_sny_not_in_custom_ir_source():
    """SNY (Sanofi) 不在 custom_ir_source (因为 SEC 20-F 兜底已成功)."""
    adr_map = load_adr_map()
    cis = adr_map["custom_ir_source"]
    assert "SNY.US" not in cis, "SNY 不需 custom_ir_source (SEC 20-F 18.7 MB 走 SEC edgar)"


# === Airbus EADSY 跳过 (WAF 强) ===

def test_eadsy_not_in_custom_ir_source():
    """EADSY (Airbus) 不在 custom_ir_source (WAF 强, 跟 7-22 教训同样)."""
    adr_map = load_adr_map()
    cis = adr_map["custom_ir_source"]
    assert "EADSY.US" not in cis, "EADSY 跳过 (WAF 强, 跟 7-22 教训 WAF 同样)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
