"""PDF 翻译器 - 调用 BabelDOC CLI 进行文档翻译"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from unified_downloader.exceptions import TranslationError

logger = logging.getLogger(__name__)

# 匹配 BabelDOC stdout 中的输出文件路径
_OUTPUT_PATH_PATTERN = re.compile(r"(?:Output|output|saved|Saved)\s+(?:to\s+)?[:=]?\s*(.+\.pdf)", re.IGNORECASE)


def load_ark_api_key() -> str:
    """Load ARK coding-plan API key from supported environment aliases."""
    return os.environ.get("ARK_API_KEY", "") or os.environ.get("ARKCODE_API_KEY", "")


def _file_hash(path: Path) -> str:
    """计算文件的 MD5 哈希（用于翻译缓存校验）"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


class PDFTranslator:
    """将 PDF 文件翻译为指定语言，通过 BabelDOC CLI 实现"""

    @staticmethod
    def translate(
        pdf_path: Path,
        target_lang: str = "zh",
        api_key: Optional[str] = None,
        model: str = "minimax-m3",
        base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3",
        qps: int = 4,
        no_dual: bool = True,
        output_dir: Optional[Path] = None,
        use_cache: bool = True,
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
            use_cache: 是否使用翻译缓存，避免重复翻译

        Returns:
            翻译后的 PDF 文件路径

        Raises:
            TranslationError: 翻译失败时抛出
        """
        if not pdf_path.exists():
            raise TranslationError(f"PDF文件不存在: {pdf_path}")

        # API Key: 由调用方从已加载的 config 传入，或从环境变量 fallback。
        # 已切 coding plan，仅使用 ARK_API_KEY/ARKCODE_API_KEY，避免把旧 OpenAI key 用到 ARK endpoint。
        if not api_key:
            api_key = load_ark_api_key()

        if not api_key:
            raise TranslationError(
                "翻译API Key未配置，请在 config.yaml 设置 translate_api_key "
                "或设置环境变量 ARK_API_KEY/ARKCODE_API_KEY"
            )

        if output_dir is None:
            output_dir = pdf_path.parent

        output_dir.mkdir(parents=True, exist_ok=True)

        # 翻译缓存检查
        cache_key = {
            "source": str(pdf_path),
            "hash": _file_hash(pdf_path),
            "lang": target_lang,
            "model": model,
        }
        marker_path = output_dir / f"{pdf_path.stem}.{target_lang}.translated"

        if use_cache and marker_path.exists():
            try:
                cached = json.loads(marker_path.read_text(encoding="utf-8"))
                if (cached.get("hash") == cache_key["hash"]
                        and cached.get("lang") == cache_key["lang"]
                        and cached.get("model") == cache_key["model"]):
                    cached_path = Path(cached["output"])
                    if cached_path.exists():
                        logger.info(f"翻译缓存命中: {cached_path}")
                        return cached_path
            except (json.JSONDecodeError, KeyError):
                pass  # 缓存文件损坏，重新翻译

        # 构建 babeldoc 命令
        # 使用 wrapper 脚本剥离推理模型输出中的 </think> 思考标签
        wrapper_path = Path(__file__).parent / "_babeldoc_wrapper.py"
        use_wrapper = wrapper_path.exists()
        if use_wrapper:
            babeldoc_cmd = [sys.executable, str(wrapper_path)]
        else:
            babeldoc_cmd = ["babeldoc"]
            logger.warning(
                "babeldoc wrapper 不存在, API key 将以命令行参数传给子进程 "
                "(共享机器 ps 可见); 建议保留 _babeldoc_wrapper.py"
            )

        cmd = babeldoc_cmd + [
            "--files", str(pdf_path),
            "--openai",
            "--openai-model", model,
            "--openai-base-url", base_url,
        ]
        if not use_wrapper:
            # W40-#50: wrapper 存在时 key 经环境变量注入 (wrapper 的
            # __main__ 里补进 sys.argv, 进程已 exec, ps 不可见);
            # 仅 fallback 裸 babeldoc 时才进 argv
            cmd += ["--openai-api-key", api_key]
        cmd += [
            "--lang-in", "en",
            "--lang-out", target_lang,
            "--output", str(output_dir),
            "--qps", str(qps),
            "--enhance-compatibility",
            "--no-auto-extract-glossary",
        ]

        if no_dual:
            cmd.append("--no-dual")

        # 忽略 BabelDOC 内部缓存，使用自己的翻译缓存机制
        cmd.append("--ignore-cache")

        logger.info(f"开始翻译: {pdf_path.name} (en→{target_lang})")
        translate_start_time = time.time()

        run_env = None
        if use_wrapper:
            run_env = {
                **os.environ,
                "UNIFIED_DOWNLOADER_TRANSLATE_KEY": api_key,
            }

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=1800,  # 30分钟超时
                env=run_env,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise TranslationError(f"BabelDOC翻译失败 (exit {result.returncode}): {error_msg}")

            # 查找翻译输出文件 — 3 层策略
            translated_path = None

            # 策略1: 解析 BabelDOC stdout 提取输出路径
            for line in result.stdout.splitlines():
                m = _OUTPUT_PATH_PATTERN.search(line)
                if m:
                    parsed = Path(m.group(1).strip())
                    if parsed.exists():
                        translated_path = parsed
                        break

            # 策略2: 按文件名模式匹配
            if translated_path is None:
                stem = pdf_path.stem
                suffix = pdf_path.suffix
                candidates = [
                    output_dir / f"{stem}.{target_lang}.mono{suffix}",
                    output_dir / f"{stem}.{target_lang}{suffix}",
                ]
                for candidate in candidates:
                    if candidate.exists():
                        translated_path = candidate
                        break

            # 策略3: 按时间窗口 + 目标语言匹配（避免误匹配旧文件）
            if translated_path is None:
                pdf_files = sorted(
                    output_dir.glob("*.pdf"),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                for f in pdf_files:
                    if (f != pdf_path
                            and target_lang in f.name
                            and f.stat().st_mtime >= translate_start_time - 1):
                        translated_path = f
                        break

            if translated_path is None or not translated_path.exists():
                raise TranslationError(f"翻译输出文件未找到，输出目录: {output_dir}")

            # 写入翻译缓存标记
            marker_path.write_text(
                json.dumps({**cache_key, "output": str(translated_path)}, ensure_ascii=False),
                encoding="utf-8",
            )

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
