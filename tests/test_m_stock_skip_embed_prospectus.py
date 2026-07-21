"""
Unit tests for MStockAdapter prospectus skip-image-embed behavior.

Regression: PR #38 issue — 招股书 S-1 招股书 embed base64 后 8.5M → 51M，
触发 IMA 后端不索引 body content。
修复: 招股书 (S-1/F-1/424B4 等) 跳过 _embed_images_as_base64。

这些测试只测"招股书跳过 embed"逻辑，不下载真实文件。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import MStockAdapter  # noqa: E402


class TestProspectusFormsConstant:
    """招股书 form 常量必须包含 S-1/F-1/424B4 全家族"""

    def test_prospectus_forms_includes_s1(self):
        assert "S-1" in MStockAdapter._PROSPECTUS_FORMS
        assert "S-1/A" in MStockAdapter._PROSPECTUS_FORMS

    def test_prospectus_forms_includes_f1(self):
        assert "F-1" in MStockAdapter._PROSPECTUS_FORMS
        assert "F-1/A" in MStockAdapter._PROSPECTUS_FORMS

    def test_prospectus_forms_includes_424b(self):
        # 424B 系列是已定价的 prospectus
        assert "424B4" in MStockAdapter._PROSPECTUS_FORMS
        assert "424B3" in MStockAdapter._PROSPECTUS_FORMS
        assert "424B5" in MStockAdapter._PROSPECTUS_FORMS

    def test_prospectus_forms_excludes_annual_reports(self):
        # 10-K/10-Q/20-F/8-K/6-K 不应被当招股书（这些仍走 embed）
        for f in ("10-K", "10-Q", "20-F", "8-K", "6-K"):
            assert f not in MStockAdapter._PROSPECTUS_FORMS, (
                f"{f} 不应被视为招股书 (应是季报/年报/重大事件)"
            )

    def test_prospectus_forms_is_frozenset(self):
        # frozenset 保证不可变
        assert isinstance(MStockAdapter._PROSPECTUS_FORMS, frozenset)


class TestProspectusSkipsEmbed:
    """_download_filing 流程中，招股书 form 不应调 _embed_images_as_base64"""

    @pytest.fixture
    def adapter(self):
        """构造一个 MStockAdapter mock 桩 (只测逻辑，不下载文件)"""
        http_client = MagicMock()
        datasources: list = []
        return MStockAdapter(http_client, datasources)

    @pytest.mark.parametrize("form_type", ["S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B3", "424B5"])
    def test_prospectus_form_skips_embed(self, adapter, form_type, tmp_path):
        """所有招股书 form 都不调 _embed_images_as_base64"""
        # 准备: 一个 HTML 文件 + filing dict + form_type
        html_path = tmp_path / f"test_{form_type.replace('/', '_')}.html"
        html_path.write_text("<html><body>Test prospectus</body></html>")

        filing = {
            "linkToTxt": f"https://www.sec.gov/test/{form_type}.htm",
            "filedAt": "2026-07-21",
        }

        with patch.object(adapter, "_http_client") as mock_http, \
             patch.object(adapter, "_embed_images_as_base64") as mock_embed, \
             patch.object(adapter, "_rate_limiter"):

            mock_http.download_file.return_value = {
                "file_path": str(html_path),
                "file_size": html_path.stat().st_size,
            }

            result = adapter._download_filing(
                filing=filing,
                ticker="TEST",
                form_type=form_type,
                year=2026,
                on_progress=None,
                checkpoint=None,
            )

            # 关键断言: 招股书不该调 embed
            assert mock_embed.call_count == 0, (
                f"招股书 {form_type} 不应触发 _embed_images_as_base64 (实际调了 {mock_embed.call_count} 次)"
            )

    @pytest.mark.parametrize("form_type", ["10-K", "10-Q", "20-F", "6-K", "8-K"])
    def test_non_prospectus_form_does_embed(self, adapter, form_type, tmp_path):
        """非招股书 form (10-K/10-Q/20-F/6-K/8-K) 仍走 embed (回归保护)"""
        html_path = tmp_path / f"test_{form_type.replace('-', '_')}.html"
        html_path.write_text("<html><body>Test annual</body></html>")

        filing = {
            "linkToTxt": f"https://www.sec.gov/test/{form_type}.htm",
            "filedAt": "2026-07-21",
        }

        with patch.object(adapter, "_http_client") as mock_http, \
             patch.object(adapter, "_embed_images_as_base64") as mock_embed, \
             patch.object(adapter, "_rate_limiter"):

            mock_http.download_file.return_value = {
                "file_path": str(html_path),
                "file_size": html_path.stat().st_size,
            }

            # 6-K 还要 mock _merge_6k_exhibits (避免走真实下载)
            with patch.object(adapter, "_merge_6k_exhibits") as mock_merge:
                mock_merge.return_value = html_path
                result = adapter._download_filing(
                    filing=filing,
                    ticker="TEST",
                    form_type=form_type,
                    year=2026,
                    on_progress=None,
                    checkpoint=None,
                )

            # 关键断言: 非招股书仍调 embed
            assert mock_embed.call_count == 1, (
                f"非招股书 {form_type} 应触发 _embed_images_as_base64 1 次 (实际 {mock_embed.call_count} 次)"
            )
