"""Translator ARK coding-plan configuration regression tests."""

from pathlib import Path

import pytest
import yaml

from unified_downloader.core.config import Config
from unified_downloader.exceptions import TranslationError
from unified_downloader.infra.translator import PDFTranslator, load_ark_api_key


def test_example_config_uses_ark_coding_defaults():
    data = yaml.safe_load(Path("config.yaml.example").read_text(encoding="utf-8"))
    cfg = Config.from_dict(data)

    assert cfg.translate_model == "minimax-m3"
    assert cfg.translate_base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"


def test_load_ark_api_key_prefers_ark_api_key(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("ARKCODE_API_KEY", "arkcode-key")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-key")

    assert load_ark_api_key() == "ark-key"


def test_load_ark_api_key_supports_arkcode_api_key(monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("ARKCODE_API_KEY", "arkcode-key")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-key")

    assert load_ark_api_key() == "arkcode-key"


def test_translate_does_not_use_legacy_openai_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("ARKCODE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-key")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    with pytest.raises(TranslationError, match="ARK_API_KEY/ARKCODE_API_KEY"):
        PDFTranslator.translate(pdf_path, api_key=None, use_cache=False)
