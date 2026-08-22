"""
Unit tests for MStockAdapter._download_pdf_direct 的 %PDF- magic 校验.

Regression (W40-#50): custom_ir_source 兜底之前唯一真实性校验是
file_size < 1000 — 大于 1KB 的品牌化 404/redirect HTML 页会被原样存成
.pdf 且 success=True 直接入库 (错误数据)。修复后要求前 1024 字节内
出现 %PDF- magic (PDF spec 允许 header 前有少量垃圾字节)。

这些测试只测校验逻辑, 不下载真实文件。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 强制导入稳定版本
sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from adapters.m_stock import MStockAdapter  # noqa: E402


@pytest.fixture
def adapter():
    return MStockAdapter(MagicMock(), [])


def _run_download(adapter, tmp_path, content: bytes):
    """让 _download_pdf_direct 把 '下载结果' 写到 tmp_path/out.pdf 再校验"""
    target = tmp_path / "out.pdf"

    def fake_download(url, file_path):
        Path(file_path).write_bytes(content)
        return {"file_path": file_path, "file_size": len(content)}

    with patch.object(adapter, "_build_file_path", return_value=target), \
         patch.object(adapter, "_http_client") as http:
        http.download_file.side_effect = fake_download
        return adapter._download_pdf_direct(
            url="https://ir.example.com/2026/report.pdf",
            ticker="TEST",
            year=2026,
            source_label="custom_ir_source",
        )


class TestPdfMagicValidation:

    def test_brand_404_html_page_rejected(self, adapter, tmp_path):
        """>1KB 的 HTML 错误页不能被当 PDF success 入库 (回归核心)"""
        html = b"<html><body>Company IR - Page Not Found</body>" + b"<!-- padding -->" * 200

        result = _run_download(adapter, tmp_path, html)

        assert result.success is False
        assert result.error_code == "PDF_INVALID_CONTENT"
        # 假 PDF 文件必须删除, 不留在 downloads/ 里
        assert not (tmp_path / "out.pdf").exists()

    def test_small_page_still_rejected_by_size(self, adapter, tmp_path):
        """<1KB 的 404 页维持原有 PDF_TOO_SMALL 拒绝"""
        result = _run_download(adapter, tmp_path, b"%PDF-1.4 short")

        assert result.success is False
        assert result.error_code == "PDF_TOO_SMALL"

    def test_real_pdf_accepted(self, adapter, tmp_path):
        """正常 PDF (magic 在文件头) 通过"""
        pdf = b"%PDF-1.7\n%" + b"\xff\xff\xff\xff" * 512  # >1KB

        result = _run_download(adapter, tmp_path, pdf)

        assert result.success is True
        assert result.error_code == "" or result.error_code is None
        assert (tmp_path / "out.pdf").exists()

    def test_pdf_with_junk_prefix_accepted(self, adapter, tmp_path):
        """spec 允许 %PDF- header 出现在前 1024 字节内 (前缀有垃圾字节仍算合法)"""
        pdf = b"junk-prefix" * 8 + b"%PDF-1.4\n" + b"%" * 2048

        result = _run_download(adapter, tmp_path, pdf)

        assert result.success is True

    def test_html_with_pdf_word_but_no_magic_rejected(self, adapter, tmp_path):
        """正文里提到 'PDF' 字样但没有 magic header 的 HTML 仍拒绝"""
        html = (
            b"<html><body>Download our annual PDF report here</body>"
            + b"filler " * 512
        )

        result = _run_download(adapter, tmp_path, html)

        assert result.success is False
        assert result.error_code == "PDF_INVALID_CONTENT"
        assert not (tmp_path / "out.pdf").exists()
