"""BabelDOC wrapper - 剥离推理模型输出中的思考标签

MiniMax M2.7 等推理模型会在翻译输出中包含 <think>...</think> 思考过程，
BabelDOC 默认不清理这些内容，导致翻译 PDF 中混入模型推理文本。

本脚本在 BabelDOC 启动前 monkey-patch 其 OpenAI 翻译器，
自动剥离思考标签内容，只保留实际翻译结果。
"""

import re
import sys

# 匹配 <think>...</think> 标签
_THINK_TAG_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_tags(text: str) -> str:
    """剥离 <think>...</think> 标签及其内容，保留实际翻译结果"""
    if not text:
        return text or ""
    cleaned = _THINK_TAG_PATTERN.sub("", text)
    result = cleaned.strip()
    # 如果清理后为空，返回原始文本（降级策略）
    return result if result else text


def patch_babeldoc_translator():
    """Monkey-patch BabelDOC 的 OpenAI 翻译器，剥离思考标签"""
    try:
        from babeldoc.translator import translator

        original_do_translate = translator.OpenAITranslator.do_translate
        original_do_llm_translate = translator.OpenAITranslator.do_llm_translate

        def patched_do_translate(self, text, rate_limit_params=None):
            result = original_do_translate(self, text, rate_limit_params)
            return strip_think_tags(result)

        def patched_do_llm_translate(self, text, rate_limit_params=None):
            result = original_do_llm_translate(self, text, rate_limit_params)
            return strip_think_tags(result)

        translator.OpenAITranslator.do_translate = patched_do_translate
        translator.OpenAITranslator.do_llm_translate = patched_do_llm_translate

    except ImportError:
        pass  # babeldoc 未安装，后续会报错


if __name__ == "__main__":
    patch_babeldoc_translator()
    # W40-#50: API key 经环境变量传入 (父进程 argv 不含 key, ps 不可见);
    # 这里补进 sys.argv 供 babeldoc 的 argparse 使用 — 进程已 exec,
    # 运行期改 sys.argv 不会反映到内核 cmdline, ps 仍然不可见
    import os
    _key = os.environ.get("UNIFIED_DOWNLOADER_TRANSLATE_KEY", "")
    if _key and "--openai-api-key" not in sys.argv:
        sys.argv.extend(["--openai-api-key", _key])
    from babeldoc.main import main
    import asyncio

    asyncio.run(main())
