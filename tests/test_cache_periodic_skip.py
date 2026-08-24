"""
Regression tests for #64: 季度/临时类文档不做内容缓存.

现象 (2026-08-24 实测): 修复 #60/#62 后, PDD 2026Q2 第一次下载命中窗口内
8/21 董事去世公告 (11.1KB) → 被缓存; 再次下载 Q2 **直接命中缓存返回公告**
(绕过 #62 防御), 清缓存后才重新触发防御. 根因: 缓存 key
md5(market:code:year:doc_type) 不含 report_period → PDD Q1/Q2/Q3 共享同一
key → 跨季度污染 + 命中缓存绕过季度选择逻辑.

修复: 季度/临时类文档 (6k/10q/quarterly/interim_report/q1_report/q3_report/
quarterly_report/季度) 直接跳过缓存 get/put, 每次真实走季度窗口选择.

这些测试只测缓存行为, 不下载真实文件.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
from unified_downloader.core.downloader import UnifiedDownloader, _is_periodic_doc_type
from unified_downloader.models.enums import Market
from unified_downloader.models.entities import DownloadResult


class TestIsPeriodicDocType:
    @pytest.mark.parametrize(
        "doc_type",
        ["6k", "6K", "10q", "10-Q", "quarterly", "interim_report",
         "q1_report", "q3_report", "quarterly_report", "季度"],
    )
    def test_periodic_types(self, doc_type):
        assert _is_periodic_doc_type(doc_type) is True

    @pytest.mark.parametrize(
        "doc_type",
        ["10k", "annual_report", "20f", "8k", "s1", "f1", "424b4",
         "prospectus", None, ""],
    )
    def test_non_periodic_types(self, doc_type):
        assert _is_periodic_doc_type(doc_type) is False


class TestQuarterlySkipsCache:
    """季度类文档: 不命中缓存, 下载后不写缓存."""

    def _downloader(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        downloader = UnifiedDownloader()
        # 真实下载返回新文件
        fresh = tmp_path / "PDD_2024_6K_fresh.pdf"
        fresh.write_bytes(b"%PDF-1.4\nfresh\n" + b"x" * 128)
        ok = DownloadResult(success=True, file_path=str(fresh))
        monkeypatch.setattr(
            downloader._adapters[Market.M], "download", lambda **kw: ok
        )
        return downloader, fresh

    def test_6k_does_not_hit_stale_cache(self, monkeypatch, tmp_path):
        """#64 核心: 预置 6k 缓存条目也不命中 → 走真实下载."""
        downloader, fresh = self._downloader(monkeypatch, tmp_path)

        stale = tmp_path / "PDD_2024_6K_stale.pdf"
        stale.write_bytes(b"%PDF-1.4\nstale 8/21 announcement\n" + b"x" * 128)
        downloader._cache_manager.put("m", "PDD", 2024, "6k", stale)
        stale.unlink()

        result = downloader.download("PDD", 2024, "6k", market=Market.M)
        assert result.success is True
        assert result.cached is False
        assert result.file_path == str(fresh)

    def test_quarterly_download_does_not_write_cache(self, monkeypatch, tmp_path):
        """#64: 下载成功后不写缓存 — 下次仍走真实选择."""
        downloader, fresh = self._downloader(monkeypatch, tmp_path)

        result = downloader.download("PDD", 2024, "6k", market=Market.M)
        assert result.success is True

        # 下载成功但缓存里无 6k 条目
        hit = downloader._cache_manager.get("m", "PDD", 2024, "6k")
        assert hit is None

    def test_annual_doc_still_cached(self, monkeypatch, tmp_path):
        """回归保护: 年度文档 (10k) 仍走缓存 (不受 #64 影响)."""
        monkeypatch.chdir(tmp_path)
        downloader = UnifiedDownloader()
        source = tmp_path / "AAPL_2024_10K.pdf"
        source.write_bytes(b"%PDF-1.4\nannual fixture\n" + b"x" * 128)
        downloader._cache_manager.put("m", "AAPL", 2024, "10k", source)
        source.unlink()

        result = downloader.download("AAPL", 2024, "10k", market=Market.M)
        assert result.success is True
        assert result.cached is True  # 年度文档缓存保留

    def test_10q_skips_cache_even_when_prepopulated(self, monkeypatch, tmp_path):
        """10q (FPI→6-K) 预置缓存也不命中 (跨季度污染防护)."""
        downloader, fresh = self._downloader(monkeypatch, tmp_path)
        stale = tmp_path / "PDD_2024_6K_stale.pdf"
        stale.write_bytes(b"%PDF-1.4\nstale\n" + b"x" * 128)
        downloader._cache_manager.put("m", "PDD", 2024, "10q", stale)
        stale.unlink()

        result = downloader.download("PDD", 2024, "10q", market=Market.M)
        assert result.success is True
        assert result.cached is False
        assert result.file_path == str(fresh)
