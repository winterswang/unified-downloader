"""
Regression tests for #49 路径统一专项 (W40-#49).

核心行为:
1. 缓存层记住语义路径 — 新条目命中直接返回 downloads/ 路径, 零还原零网络
   (此前美股每次命中调 edgar.Company() 发 SEC 请求, A/H 直接返回
   data/cache hash 乱码路径 → morning-brief DB file_path 不统一的根因)
2. 老条目 (无 semantic_path) / 语义副本被清理 → 从 hash 副本还原;
   A/H 用 adapter 复刻下载命名的本地推导, 无网络
3. 老库 schema 迁移 (ALTER TABLE 加列)
4. put 的防御: 入参是缓存路径时不写坏 semantic_path
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unified_downloader.infra.cache import CacheManager, CacheHit  # noqa: E402


def _make_pdf(path: Path, size: int = 2048) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + b"x" * size)
    return path


# ═══ 老库 schema 迁移 ═══


class TestSchemaMigration:
    def test_old_db_gets_semantic_path_column(self, tmp_path):
        """手工建 8 列老 schema → CacheManager 初始化 → 列补齐、老行可读"""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        db = cache_dir / ".cache.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                CREATE TABLE cache_entries (
                    key TEXT PRIMARY KEY, file_path TEXT NOT NULL,
                    size INTEGER NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, md5 TEXT,
                    access_count INTEGER DEFAULT 0, last_access TEXT
                )
            """)
            old_hash = cache_dir / "a" / "600" / "abc123.PDF"
            _make_pdf(old_hash)
            conn.execute(
                "INSERT INTO cache_entries (key, file_path, size, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?)",
                ("k1", str(old_hash), 100, "2026-08-01T00:00:00",
                 "2099-01-01T00:00:00"),
            )

        mgr = CacheManager(cache_dir)  # 触发迁移

        with sqlite3.connect(str(db)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cache_entries)")}
        assert "semantic_path" in cols

        hit = mgr.get("a", "OLD", None, "annual_report") if False else None
        # 老行直接按 key 读 (不走 _make_key, 用 db 查)
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT file_path, semantic_path FROM cache_entries WHERE key='k1'"
            ).fetchone()
        assert row[0].endswith("abc123.PDF")
        assert row[1] is None  # 老行 semantic 为 NULL → 走还原


# ═══ CacheHit / put 防御 ═══


class TestCacheHitSemantics:
    def test_put_stores_semantic_path_and_get_returns_it(self, tmp_path):
        mgr = CacheManager(tmp_path / "cache")
        semantic = _make_pdf(tmp_path / "downloads" / "a" / "600" / "600519_2025_ANNUAL_REPORT.PDF")

        mgr.put("a", "600519", 2025, "annual_report", file_path=semantic)

        hit = mgr.get("a", "600519", 2025, "annual_report")
        assert isinstance(hit, CacheHit)
        assert hit.semantic_path == str(semantic)
        assert hit.path == str(semantic)          # 直接语义, 不走还原
        assert hit.hash_path.startswith(str(tmp_path / "cache"))

    def test_semantic_copy_deleted_hash_alive_still_hits(self, tmp_path):
        """语义副本 (downloads/) 被清理 → 不删条目, 返回 hash 路径由调用方还原"""
        mgr = CacheManager(tmp_path / "cache")
        semantic = _make_pdf(tmp_path / "downloads" / "a" / "600" / "600519_2025_ANNUAL_REPORT.PDF")
        mgr.put("a", "600519", 2025, "annual_report", file_path=semantic)
        semantic.unlink()  # 用户清理了 downloads/

        hit = mgr.get("a", "600519", 2025, "annual_report")
        assert hit is not None                     # hash 副本健在 → 条目有效
        assert hit.semantic_path is None           # 语义副本丢了
        assert hit.path == hit.hash_path           # 调用方走还原

    def test_put_with_cache_path_input_keeps_old_semantic(self, tmp_path):
        """V5 防御: 入参本身是缓存路径时不写坏 semantic_path"""
        mgr = CacheManager(tmp_path / "cache")
        semantic = _make_pdf(tmp_path / "downloads" / "m" / "AAP" / "AAPL_2025_10K.html")
        mgr.put("m", "AAPL", 2025, "10k", file_path=semantic)
        hit1 = mgr.get("m", "AAPL", 2025, "10k")
        assert hit1.semantic_path == str(semantic)

        # 用 hash 副本路径再次 put (理论调用路径) — 不得覆盖 semantic
        mgr.put("m", "AAPL", 2025, "10k", file_path=Path(hit1.hash_path))

        hit2 = mgr.get("m", "AAPL", 2025, "10k")
        assert hit2.semantic_path == str(semantic)

    def test_set_semantic_path_backfill(self, tmp_path):
        mgr = CacheManager(tmp_path / "cache")
        semantic = _make_pdf(tmp_path / "downloads" / "h" / "007" / "00700_2025_ANNUAL_REPORT.pdf")
        mgr.put("h", "00700", 2025, "annual_report", file_path=semantic)
        with sqlite3.connect(str(mgr._db_path)) as conn:
            conn.execute("UPDATE cache_entries SET semantic_path = NULL")

        mgr.set_semantic_path("h", "00700", 2025, "annual_report", str(semantic))

        hit = mgr.get("h", "00700", 2025, "annual_report")
        assert hit.semantic_path == str(semantic)


# ═══ adapter 推导矩阵 (A/H 老条目还原依据) ═══


class TestAdapterSemanticDerivation:
    def test_a_stock_code_normalization(self):
        from unified_downloader.adapters.a_stock import AStockAdapter

        adapter = AStockAdapter(MagicMock(), [])
        p1 = adapter.build_semantic_cache_path("600519", 2025, "annual_report", ".PDF")
        p2 = adapter.build_semantic_cache_path("sh600519", 2025, "annual_report", ".PDF")
        assert p1 == p2 == Path("downloads/a/600/600519_2025_ANNUAL_REPORT.PDF")

    def test_a_stock_label_dispatch(self):
        from unified_downloader.adapters.a_stock import AStockAdapter

        adapter = AStockAdapter(MagicMock(), [])
        q1 = adapter.build_semantic_cache_path("600519", 2025, "quarterly_q1", ".PDF")
        unknown = adapter.build_semantic_cache_path("600519", 2025, "bogus", ".PDF")
        assert q1.name == "600519_2025_QUARTERLY_REPORT.PDF"   # q1 → QUARTERLY_REPORT
        assert unknown.name == "600519_2025_ANNUAL_REPORT.PDF"  # 未知 → 年报 (与 download 一致)

    def test_a_stock_year_none_no_year_in_name(self):
        from unified_downloader.adapters.a_stock import AStockAdapter

        adapter = AStockAdapter(MagicMock(), [])
        p = adapter.build_semantic_cache_path("600519", None, "prospectus", ".PDF")
        assert p.name == "600519_PROSPECTUS.PDF"

    def test_h_stock_zfill5_and_ext(self):
        from unified_downloader.adapters.h_stock import HStockAdapter

        adapter = HStockAdapter(MagicMock(), [])
        p1 = adapter.build_semantic_cache_path("700", 2025, "annual_report", ".pdf")
        p2 = adapter.build_semantic_cache_path("00700", 2025, "annual_report", ".pdf")
        assert p1 == p2 == Path("downloads/h/007/00700_2025_ANNUAL_REPORT.pdf")

    def test_base_default_raises(self):
        from unified_downloader.adapters.base import BaseStockAdapter

        # 最小具体子类 (实现两个抽象方法), 验证 base 默认实现
        Dummy = type(
            "DummyAdapter",
            (BaseStockAdapter,),
            {
                "__init__": lambda self: None,
                "download": lambda self, *a, **k: None,
                "async_download": lambda self, *a, **k: None,
            },
        )
        with pytest.raises(NotImplementedError):
            Dummy().build_semantic_cache_path("X", 2025, "annual_report", ".pdf")


# ═══ downloader 级: 命中路径 / 零网络 ═══


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from unified_downloader.core.downloader import UnifiedDownloader
    from unified_downloader.core.config import get_default_config

    d = UnifiedDownloader(get_default_config())
    yield d
    d.close()


class TestDownloaderSemanticCacheHit:
    def test_us_second_hit_zero_network(self, downloader, tmp_path):
        """新条目二次命中: 不调 get_annual_form_type (之前每次命中打 SEC)"""
        from unified_downloader.adapters.m_stock import MStockAdapter
        from unified_downloader.models.enums import Market
        from unified_downloader.models.entities import DownloadResult

        out = _make_html(tmp_path / "downloads" / "m" / "AAP" / "AAPL_2025_10K.html")
        adapter = downloader._adapters[Market.M]
        adapter.download = MagicMock(return_value=DownloadResult(
            success=True, file_path=str(out), file_size=out.stat().st_size, source="edgar"))

        r1 = downloader.download("AAPL", 2025, "10k", market=Market.M)
        assert r1.success

        with patch.object(
            MStockAdapter, "get_annual_form_type",
            side_effect=AssertionError("cache hit must not hit SEC"),
        ):
            r2 = downloader.download("AAPL", 2025, "10k", market=Market.M)

        assert r2.cached is True
        assert Path(r2.file_path) == out
        assert r2.metadata["cache_path"].startswith("data/cache/m/AAP/")  # 契约: 恒 hash 路径

    def test_ah_old_entry_restored_to_semantic_not_hash(self, downloader, tmp_path):
        """#49 核心: A 股老条目 (无 semantic) 命中 → 返回语义路径而非 hash 乱码"""
        from unified_downloader.models.enums import Market

        semantic = _make_pdf(
            tmp_path / "downloads" / "a" / "600" / "600519_2025_ANNUAL_REPORT.PDF")
        downloader._cache_manager.put(
            "a", "600519", 2025, "annual_report", file_path=semantic)
        with sqlite3.connect(str(downloader._cache_manager._db_path)) as conn:
            conn.execute("UPDATE cache_entries SET semantic_path = NULL")
        semantic.unlink()  # 语义副本不存在, hash 副本在

        r = downloader.download("600519", 2025, "annual_report", market=Market.A)

        assert r.success and r.cached
        assert not str(r.file_path).startswith("data/cache"), "不得返回 hash 乱码路径"
        assert Path(r.file_path).name == "600519_2025_ANNUAL_REPORT.PDF"
        assert Path(r.file_path).exists()          # hash 副本已复制还原

        # 第二次命中: 已懒回填 → 直接语义路径
        r2 = downloader.download("600519", 2025, "annual_report", market=Market.A)
        assert r2.file_path == r.file_path


def _make_html(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<html>" + b"y" * 2048 + b"</html>")
    return path
