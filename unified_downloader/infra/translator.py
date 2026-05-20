"""PDF 翻译器 - 调用 BabelDOC CLI 进行文档翻译"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from unified_downloader.exceptions import TranslationError

logger = logging.getLogger(__name__)


class PDFTranslator:
    """将 PDF 文件翻译为指定语言，通过 BabelDOC CLI 实现"""

    @staticmethod
    def translate(
        pdf_path: Path,
        target_lang: str = "zh",
        api_key: Optional[str] = None,
        model: str = "MiniMax-M2.7",
        base_url: str = "https://api.minimaxi.com/v1",
        qps: int = 4,
        no_dual: bool = True,
        output_dir: Optional[Path] = None,
    ) -> Path:
        """
        翻译 PDF 文件 (英文 → 目标语言)

        Args:
            pdf_path: 输入 PDF 文件路径
            target_lang: 目标语言代码 (默认 "zh")
            api_key: 翻译 API Key
            model: 翻译模型名称
            base_url: 翻译 API Base URL
            qps: 每秒请求数限制
            no_dual: 是否仅输出目标语言版（不输出双语版）
            output_dir: 输出目录，None 则与输入同目录

        Returns:
            翻译后的 PDF 文件路径

        Raises:
            TranslationError: 翻译失败时抛出
        """
        if not pdf_path.exists():
            raise TranslationError(f"PDF文件不存在: {pdf_path}")

        # API Key: 由调用方从已加载的 config 传入，或从环境变量 fallback
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY", "")

        if not api_key:
            raise TranslationError(
                "翻译API Key未配置，请在 config.yaml 设置 translate_api_key "
                "或设置环境变量 OPENAI_API_KEY"
            )

        if output_dir is None:
            output_dir = pdf_path.parent

        output_dir.mkdir(parents=True, exist_ok=True)

        # 构建 babeldoc 命令
        # 使用 wrapper 脚本剥离推理模型输出中的 </think> 思考标签
        wrapper_path = Path(__file__).parent / "_babeldoc_wrapper.py"
        if wrapper_path.exists():
            babeldoc_cmd = [sys.executable, str(wrapper_path)]
        else:
            babeldoc_cmd = ["babeldoc"]

        cmd = babeldoc_cmd + [
            "--files", str(pdf_path),
            "--openai",
            "--openai-model", model,
            "--openai-base-url", base_url,
            "--openai-api-key", api_key,
            "--lang-in", "en",
            "--lang-out", target_lang,
            "--output", str(output_dir),
            "--qps", str(qps),
            "--enhance-compatibility",
            "--no-auto-extract-glossary",
        ]

        if no_dual:
            cmd.append("--no-dual")

        # 忽略翻译缓存，避免使用之前含思考标签的缓存结果
        cmd.append("--ignore-cache")

        logger.info(f"开始翻译: {pdf_path.name} (en→{target_lang})")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # 30分钟超时
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise TranslationError(f"BabelDOC翻译失败 (exit {result.returncode}): {error_msg}")

            # 查找翻译输出文件
            # BabelDOC 输出命名: {stem}.{target_lang}.mono.pdf (no-dual) 或 {stem}.{target_lang}.pdf
            stem = pdf_path.stem
            suffix = pdf_path.suffix

            # 可能的输出文件名
            candidates = [
                output_dir / f"{stem}.{target_lang}.mono{suffix}",
                output_dir / f"{stem}.{target_lang}{suffix}",
            ]

            translated_path = None
            for candidate in candidates:
                if candidate.exists():
                    translated_path = candidate
                    break

            # 如果以上都不匹配，查找输出目录中最近生成的 PDF
            if translated_path is None:
                pdf_files = sorted(
                    output_dir.glob("*.pdf"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                for f in pdf_files:
                    if f != pdf_path and target_lang in f.name:
                        translated_path = f
                        break

            if translated_path is None or not translated_path.exists():
                raise TranslationError(f"翻译输出文件未找到，输出目录: {output_dir}")

            logger.info(f"翻译完成: {translated_path}")
            return translated_path

        except TranslationError:
            raise
        except subprocess.TimeoutExpired:
            raise TranslationError("BabelDOC翻译超时（30分钟）")
        except FileNotFoundError:
            raise TranslationError(
                "babeldoc 未安装，请运行: uv tool install --python 3.12 BabelDOC"
            )
        except Exception as e:
            raise TranslationError(f"翻译过程出错: {e}")
