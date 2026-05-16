"""HTML 转 PDF 转换器"""

import logging
from pathlib import Path

from unified_downloader.exceptions import ConversionError

logger = logging.getLogger(__name__)


class HTMLToPDFConverter:
    """将 HTML 文件转换为 PDF 格式"""

    @staticmethod
    def convert(
        html_path: Path,
        pdf_path: Path | None = None,
        keep_original: bool = True,
    ) -> Path:
        """
        将 HTML 文件转换为 PDF

        Args:
            html_path: HTML 文件路径
            pdf_path: PDF 输出路径，None 则自动将扩展名替换为 .pdf
            keep_original: 是否保留原始 HTML 文件

        Returns:
            生成的 PDF 文件路径

        Raises:
            ConversionError: 转换失败时抛出
        """
        if pdf_path is None:
            pdf_path = html_path.with_suffix(".pdf")

        try:
            from weasyprint import HTML

            html = HTML(filename=str(html_path))
            html.write_pdf(str(pdf_path))

            if not pdf_path.exists():
                raise ConversionError(f"PDF文件未生成: {pdf_path}")

            # 转换成功后删除原始 HTML
            if not keep_original and html_path.exists():
                html_path.unlink()
                logger.info(f"已删除原始HTML: {html_path}")

            logger.info(f"HTML→PDF转换成功: {html_path} → {pdf_path}")
            return pdf_path

        except ConversionError:
            raise
        except ImportError as e:
            raise ConversionError(
                f"weasyprint未安装，请运行 pip install weasyprint: {e}"
            )
        except Exception as e:
            raise ConversionError(f"HTML→PDF转换失败: {e}")
