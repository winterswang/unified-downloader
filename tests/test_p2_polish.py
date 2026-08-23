"""
Regression tests for P2 打磨批次 (W40-#50).

覆盖: MCP market 枚举补 e + 守卫测试扫 MCP schema、_primary_doc_ext
(.htm/.html 与 %20exhibit)、adr_map 损坏容错、a_stock https、限速单
key、eu_stock async 异常一致性、batch --errors 真实生效。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unified_downloader.utils.adr_map import load_adr_map  # noqa: E402
from unified_downloader.adapters.m_stock import _primary_doc_ext  # noqa: E402


# ═══ MCP market 枚举 ═══


class TestMcpMarketEnum:
    """W40-#50: MCP 的 4 处 market 枚举之前都漏了 EU 的 "e" —
    专门防"加市场漏改入口"的一致性测试只扫 cli.py, 恰好没覆盖到
    唯一真漏改的入口。"""

    MCP_FILE = ROOT / "src" / "unified_downloader_mcp_server.py"
    CLI_FILE = ROOT / "unified_downloader" / "cli.py"

    CANONICAL = {"a", "h", "m", "e"}  # 与 models.enums.Market 对齐

    def _mcp_market_enums(self):
        text = self.MCP_FILE.read_text()
        # 所有 market 属性下的 enum 列表
        enums = []
        for m in re.finditer(r'"market":\s*\{[^{}]*?"enum":\s*\[([^\]]+)\]', text, re.S):
            enums.append(set(re.findall(r'"(\w)"', m.group(1))))
        return enums

    def _cli_single_letter_choices(self):
        text = self.CLI_FILE.read_text()
        choices = []
        for m in re.finditer(r'click\.Choice\(\s*\[([^\]]+)\]', text):
            flags = re.findall(r'"(\w)"', m.group(1))
            if flags:
                choices.append(set(flags))
        return choices

    def test_mcp_has_market_enums_to_check(self):
        assert len(self._mcp_market_enums()) >= 4

    def test_mcp_market_enums_match_canonical(self):
        for i, enum in enumerate(self._mcp_market_enums()):
            assert enum == self.CANONICAL, (
                f"MCP 第 {i + 1} 处 market 枚举 {sorted(enum)} != "
                f"{sorted(self.CANONICAL)} — 加市场时漏改了 MCP 入口"
            )

    def test_cli_market_choices_match_canonical(self):
        """守卫扩展到 cli.py: 任何单字母 Choice 列表都必须是完整市场集
        (防'加市场漏改某个命令')"""
        singles = [c for c in self._cli_single_letter_choices() if len(c) <= 4]
        assert singles, "cli.py 应至少有一个 market Choice"
        for i, c in enumerate(singles):
            assert c == self.CANONICAL, (
                f"cli.py 第 {i + 1} 处单字母 Choice {sorted(c)} 缺市场: "
                f"{sorted(self.CANONICAL - c)}"
            )

    def test_mcp_uses_to_thread_for_blocking_download(self):
        text = self.MCP_FILE.read_text()
        assert "asyncio.to_thread(\n                downloader.download" in text, (
            "MCP async handler 里的同步下载必须走 to_thread (之前冻结 stdio 循环)"
        )

    def test_mcp_status_paths_anchored_to_project_root(self):
        text = self.MCP_FILE.read_text()
        assert 'Path("data/cache")' not in text
        assert 'Path("downloads")' not in text


# ═══ _primary_doc_ext ═══


class TestPrimaryDocExt:
    def test_htm(self):
        assert _primary_doc_ext("https://sec.gov/Archives/x.htm") == ".html"

    def test_html_not_saved_as_txt(self):
        # W40-#50 回归核心: ".htm" 差一个 l 漏判 ".html" → 存 .txt
        assert _primary_doc_ext("https://sec.gov/Archives/x.html") == ".html"

    def test_exhibit_encoded_space(self):
        # " exhibit" 是死条件 (URL 空格编码为 %20)
        assert _primary_doc_ext("https://sec.gov/Archives/x%20exhibit") == ".html"

    def test_other_extents_stay_txt(self):
        assert _primary_doc_ext("https://sec.gov/Archives/x.txt") == ".txt"
        assert _primary_doc_ext("https://sec.gov/Archives/noext") == ".txt"


# ═══ adr_map 容错 ═══


class TestAdrMapCorruption:
    def test_corrupt_json_returns_empty_map(self, tmp_path):
        bad = tmp_path / "adr_map.json"
        bad.write_text('{"use_hk_source": {"MNSO.US": ', encoding="utf-8")

        assert load_adr_map(bad) == {}  # 之前 json.loads 直接抛

    def test_valid_json_still_loads(self, tmp_path):
        good = tmp_path / "adr_map.json"
        good.write_text(json.dumps({"use_sec_20f_only": {"NVO.US": "20-F only"}}),
                        encoding="utf-8")

        assert load_adr_map(good)["use_sec_20f_only"]["NVO.US"] == "20-F only"

    def test_a_stock_uses_https(self):
        from unified_downloader.adapters.a_stock import CninfoAPI

        assert CninfoAPI.PDF_BASE_URL.startswith("https://")


# ═══ 限速: 常态路径只等实际要用的 key ═══


class TestRateLimiterSingleKey:
    def test_edgar_success_waits_only_edgar_key(self):
        from unittest.mock import MagicMock, patch

        sys.path.insert(0, str(ROOT / "unified_downloader"))
        from adapters.m_stock import MStockAdapter

        adapter = MStockAdapter(MagicMock(), [])
        waits = []

        with patch.object(adapter, "_rate_limiter") as rl, \
             patch.object(adapter, "_init_edgar", return_value=True), \
             patch.object(adapter, "_search_edgar", return_value=[]) as mock_search:
            rl.wait.side_effect = lambda key: waits.append(key)

            result = adapter._search_filings("AAPL", "10-K", 2026)

            assert result == []
            mock_search.assert_called_once()
            # W40-#50: 之前常态路径连等 edgar_search + sec_api_search 两个
            # key, 为从未发生的 sec-api 调用白睡 ~5s
            assert waits == ["edgar_search"]


# ═══ eu_stock async 异常一致性 ═══


class TestAsyncAdapterErrorConsistency:
    def test_not_implemented_becomes_failure_result(self, tmp_path, monkeypatch):
        """eu_stock 骨架抛 NotImplementedError — 之前单发 download() 直接
        炸给调用方 (sync 路径却兜成失败结果)"""
        monkeypatch.chdir(tmp_path)
        from unified_downloader.core.async_downloader import AsyncUnifiedDownloader
        from unified_downloader.models.enums import Market

        ad = AsyncUnifiedDownloader()
        r = asyncio.run(ad.download("LVMH", 2026, "annual_report", market=Market.E))

        assert r.success is False
        assert r.error_code == "DOWNLOAD_ERROR"


# ═══ batch --errors 真实生效 ═══


class TestBatchMaxErrors:
    def test_batch_aborts_when_errors_exceeded(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from unified_downloader.core.downloader import UnifiedDownloader
        from unified_downloader.models.enums import Market

        d = UnifiedDownloader()
        gate = threading.Event()
        started = []
        executed = []

        def fake_download(self, code, year=None, document_type="annual_report",
                          market=None, use_cache=True, on_progress=None, **kw):
            started.append(code)
            if code == "F1":          # 第一个任务失败并卡住后续调度窗口
                gate.wait(2)
                from unified_downloader.models.entities import DownloadResult
                return DownloadResult(success=False, error_code="X",
                                      error_message="boom")
            gate.wait(2)              # 让 F1 的失败先计入
            executed.append(code)
            from unified_downloader.models.entities import DownloadResult
            return DownloadResult(success=True)

        monkeypatch.setattr(UnifiedDownloader, "download", fake_download)

        tasks = [{"code": c, "market": Market.M} for c in ["F1", "OK1", "OK2", "OK3"]]
        result = d.batch_download(tasks, max_workers=1, max_errors=0)
        gate.set()

        assert result.failed >= 1
        assert result.metadata and result.metadata.get("aborted") is True
        # max_workers=1 串行: F1 失败后 (failed=1 > max_errors=0) 取消剩余
        assert "OK3" not in executed
