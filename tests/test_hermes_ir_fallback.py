"""W37-#50b: HESAY 爱马仕(ADR) custom_ir_source fallback 回归测试.

Hermès International (RMS.PA Euronext Paris) 在 SEC EDGAR 100% Company not found
(99% family-owned, 无动机美股二次上市). 走 finance.hermes.com sitemap-pdf.xml
真源 (跟 7-19 港股 source 同样公开 PDF 直链模式).

跟 W37-#50c (NTDOY 任天堂) 同样 verified_pdfs 模式, 但 URL 模式不一样:
  - NTDOY: {ir_base}/{yyyy}/{yyyymmdd}{lang}.pdf  (URL 可拼)
  - HESAY: {ir_base}/{yyyy-mm}/{hhh}/{filename}?VersionId=xxx  (URL 不可拼, 需 dict)
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# adr_map.json 加载 (跟 W36 PR #43 同样 mode)
ADR_MAP_PATH = Path(__file__).parent.parent / "config" / "adr_map.json"


def load_adr_map():
    with open(ADR_MAP_PATH) as f:
        return json.load(f)


def test_hesay_in_custom_ir_source():
    """HESAY.US 必须在 custom_ir_source 段, 提供 sitemap_pdfs 跟 verified_pdfs."""
    adr_map = load_adr_map()
    cis = adr_map["custom_ir_source"]
    assert "HESAY.US" in cis, "HESAY.US 必须在 adr_map.custom_ir_source (W37-#50b)"
    hesay = cis["HESAY.US"]
    assert "sitemap_pdfs" in hesay, "HESAY 需 sitemap_pdfs 字段找真链"
    assert "verified_pdfs" in hesay, "HESAY 需 verified_pdfs 字段存真链"
    assert len(hesay["verified_pdfs"]) >= 2, "HESAY 至少 2 个 verified PDF (URD 2024+2025)"


def test_hesay_verified_pdfs_use_dict_format():
    """HESAY verified_pdfs 必须是 dict 格式 (含 url field), 不是 NTDOY string 模式."""
    adr_map = load_adr_map()
    hesay = adr_map["custom_ir_source"]["HESAY.US"]
    for date, info in hesay["verified_pdfs"].items():
        assert isinstance(info, dict), f"{date} 必须是 dict, not {type(info).__name__}"
        assert "url" in info, f"{date} dict 必须有 url field"
        assert info["url"].startswith("https://assets-finance.hermes.com"), \
            f"{date} URL 必须在 assets-finance.hermes.com CDN (不是 finance.hermes.com 渲染页)"


def test_ntsoy_still_string_format():
    """NTDOY 仍走 string 模式 (跟 W37-#50c PR #44 同样), 不被 HESAY 改影响."""
    adr_map = load_adr_map()
    ntsoy = adr_map["custom_ir_source"]["NTDOY.US"]
    for date, info in ntsoy["verified_pdfs"].items():
        assert isinstance(info, str), f"NTDOY {date} 必须仍 string 模式 (W37-#50c PR #44)"
        assert ".pdf" in info, f"NTDOY {date} 描述必须含 .pdf 文件名"


def test_hesay_url_real_hermes_sitemap():
    """HESAY 真 URL 来自 finance.hermes.com sitemap-pdf.xml 真抓验证."""
    adr_map = load_adr_map()
    hesay = adr_map["custom_ir_source"]["HESAY.US"]
    # sitemap_pdfs URL 必须跟真源一致
    assert hesay["sitemap_pdfs"] == "https://finance.hermes.com/sitemap-pdf.xml", \
        "sitemap_pdfs URL 必须是 finance.hermes.com/sitemap-pdf.xml (W37-#50b 8-2 真验证)"
    # verified URLs 必须包含 assets-finance CDN 路径
    for date, info in hesay["verified_pdfs"].items():
        assert "/s3fs-public/node/pdf_file/" in info["url"], \
            f"{date} URL 必须含 s3fs-public CDN path (Hermès 公开 PDF 真源)"


def test_use_sec_20f_only_still_marks_hesay():
    """use_sec_20f_only 仍标 HESAY (跟 W36 PR #40 同样), custom_ir_source 是 fallback."""
    adr_map = load_adr_map()
    assert "HESAY.US" in adr_map["use_sec_20f_only"], \
        "HESAY.US 必须在 use_sec_20f_only (跟 W36 PR #40 同样), custom_ir_source 是 W37-#50b 新 fallback"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
