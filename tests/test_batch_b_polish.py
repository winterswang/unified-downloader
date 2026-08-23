"""
Regression tests for 批次 B 快修 (W40-#50).

B1: h_stock 年报/中报多檔案防护 (之前只有招股书路径检查)
B2: translator API key 经 wrapper 环境变量传递 (不进子进程 argv)
B3: bulk_common 统一语义 (compute_summary 严格版 / upload_ima 完整判定)
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "_bt"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    if hasattr(module, "log"):
        module.log = logging.getLogger(f"test-{name}")
    return module


# ═══ B1: h_stock 多檔案防护 ═══


class TestHStockMultiFileGuard:
    def _adapter(self):
        from unified_downloader.adapters.h_stock import HStockAdapter

        return HStockAdapter(MagicMock(), [])

    def test_annual_report_multi_file_rejected(self):
        """年报/中报路径的多檔案条目返回 MULTI_FILE 而非下载索引页"""
        adapter = self._adapter()

        class _FakeApi:
            def search_documents(self, **kwargs):
                return [{
                    "file_link": "https://www1.hkexnews.hk/listedco/...",
                    "file_info": "多檔案",
                    "date_time": "31/12/2025 16:00",
                }]

        with patch.object(adapter, "_get_stock_id", return_value="123"), \
             patch.object(adapter, "_get_api", return_value=_FakeApi()), \
             patch.object(adapter, "_rate_limiter"):
            result = adapter._download_report(
                "00700", 2025, "annual_report", "ANNUAL_RESULTS", None, None
            )

        assert result.success is False
        assert result.error_code == "MULTI_FILE"
        assert "多檔案" in result.error_message

    def test_normal_report_still_downloads(self):
        """非多檔案条目不受新防护影响"""
        adapter = self._adapter()

        class _FakeApi:
            def search_documents(self, **kwargs):
                return [{
                    "file_link": "https://www1.hkexnews.hk/x.pdf",
                    "file_info": "",
                    "date_time": "31/12/2025 16:00",
                }]

        fake_dl = MagicMock(return_value={"file_path": "/tmp/x.pdf", "file_size": 123})

        with patch.object(adapter, "_get_stock_id", return_value="123"), \
             patch.object(adapter, "_get_api", return_value=_FakeApi()), \
             patch.object(adapter, "_rate_limiter"), \
             patch.object(adapter._http_client, "download_file", fake_dl):
            result = adapter._download_report(
                "00700", 2025, "annual_report", "ANNUAL_RESULTS", None, None
            )

        assert result.success is True
        fake_dl.assert_called_once()


# ═══ B2: translator key 走 wrapper env ═══


class TestTranslatorKeyViaEnv:
    def test_wrapper_path_key_not_in_argv(self, tmp_path, monkeypatch):
        """wrapper 存在时: key 不进 cmd argv, 经 env 传子进程"""
        from unified_downloader.infra import translator

        pdf = tmp_path / "t.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 128)
        captured = {}

        class _FakeProc:
            returncode = 1
            stdout = ""
            stderr = "fake failure (output discovery not under test)"

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(translator.subprocess, "run", fake_run)
        monkeypatch.setattr(translator.time, "sleep", lambda s: None)

        from unified_downloader.exceptions import TranslationError
        with pytest.raises(TranslationError):
            translator.PDFTranslator.translate(
                pdf, api_key="sk-SECRET", use_cache=False
            )

        assert "--openai-api-key" not in captured["cmd"], "key 不得进 argv (ps 可见)"
        assert "sk-SECRET" not in " ".join(captured["cmd"])
        assert captured["env"]["UNIFIED_DOWNLOADER_TRANSLATE_KEY"] == "sk-SECRET"


# ═══ B3: bulk_common 统一语义 ═══


class TestBulkCommonSemantics:
    def _common(self):
        spec = importlib.util.spec_from_file_location(
            "bulk_common_bt", ROOT / "scripts" / "bulk_common.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_compute_summary_strict_ima_semantics(self):
        bc = self._common()
        state = {
            "annual": {
                "2025": {"status": "downloaded", "ima": "semantic_upload_rate_limited"},
                "2024": {"status": "downloaded", "ima": None},
                "2023": {"status": "skipped"},
            }
        }
        sm = bc.compute_summary(state)
        # downloaded 但 ima 非常规失败值 → 不算 ok, 计入 ima_failed
        assert sm["annual_ok"] == 1
        assert sm["annual_skipped"] == 1
        assert sm["ima_failed"] == 1

    def test_upload_ima_rejects_zero_success_marker(self, tmp_path):
        """宽松的 '✅ 出现' 会把 '成功 0' 判成功 — 完整判定版必须拒绝"""
        bc = self._common()
        f = tmp_path / "x.pdf"
        f.write_bytes(b"%PDF x")
        captured = {}

        def fake_run(cmd, timeout=120, project_root=None):
            captured["cmd"] = cmd
            return 0, "✅ 成功 0", ""

        with patch.object(bc, "run", side_effect=fake_run), \
             patch.object(bc, "resolve_ima_script", return_value=f):
            assert bc.upload_ima(f, project_root=tmp_path) is False

        # 真正的成功标记 → 通过
        def fake_run2(cmd, timeout=120, project_root=None):
            return 0, "✅ 成功 1", ""

        with patch.object(bc, "run", side_effect=fake_run2), \
             patch.object(bc, "resolve_ima_script", return_value=f):
            assert bc.upload_ima(f, project_root=tmp_path) is True

    def test_three_scripts_share_common_module(self):
        """三脚本的 compute_summary 均委托 bulk_common (单一事实源)"""
        for name in ("bulk_download.py", "bulk_scheduler.py", "fix_us_html_ima.py"):
            text = (ROOT / "scripts" / name).read_text()
            assert "bulk_common" in text, f"{name} 未接入 bulk_common"
