"""
Regression tests for P1-并发/infra 批次 (W40-#50).

覆盖:
1. 断点续传 MD5: 增量哈希 → 全文件校验 (之前续传 + expected_md5 100% 校验
   失败并删除整个完整文件)
2. 熔断器线程安全 + config 接线 + half_open 预算生效
3. adapter 标志恢复 (之前一次 --pdf 永久污染共享 adapter)
4. 缓存写失败不连坐市场失败 / 不误开熔断
5. checkpoint 原子写 + 损坏容错 (之前毒化该 task 全部后续下载)
6. 缓存 LRU 超限清理 (之前单日写超 max_size 时清理完全失效)
7. 429 重试: Retry-After 解析 / 末次不白睡 / 异步版对齐重试
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unified_downloader.infra.http_client import (
    HTTPClient,
    AsyncHTTPClient,
    _parse_retry_after,
    _md5_of_file,
)
from unified_downloader.infra.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerManager,
)
from unified_downloader.infra.checkpoint import CheckpointManager
from unified_downloader.infra.cache import CacheManager


# ═══ Retry-After 解析 ═══


class TestParseRetryAfter:
    def test_numeric_seconds(self):
        assert _parse_retry_after("30") == 30

    def test_http_date_falls_back_to_default(self):
        # W40-#50: 之前裸 int() 遇 HTTP-date 抛 ValueError
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 60

    def test_none_and_garbage(self):
        assert _parse_retry_after(None) == 60
        assert _parse_retry_after("soon") == 60
        assert _parse_retry_after("5", default=10) == 5


# ═══ 断点续传 MD5 全文件校验 ═══


class _FakeResponse:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {}

    def iter_content(self, chunk_size=8192):
        yield from self._chunks

    def raise_for_status(self):
        pass

    def close(self):
        pass


class TestMd5Resume:
    def test_md5_of_file_matches_hashlib(self, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"0123456789")
        assert _md5_of_file(f) == hashlib.md5(b"0123456789").hexdigest()

    def test_resume_with_expected_md5_verifies_full_file(self, tmp_path, monkeypatch):
        """续传 6 字节前缀 + 4 字节增量, expected_md5 是全 10 字节的哈希 —
        之前增量哈希必不相等 → 删整个文件; 现在校验通过"""
        client = HTTPClient()
        target = tmp_path / "out.bin"
        prefix, rest = b"012345", b"6789"
        target.write_bytes(prefix)  # 已下载前缀

        fake_session = MagicMock()
        fake_session.request.return_value = _FakeResponse(206, [rest])
        monkeypatch.setattr(client, "_session", fake_session)

        result = client.download_file(
            url="https://example.com/f.bin",
            file_path=target,
            checkpoint={"downloaded_bytes": len(prefix)},
            expected_md5=hashlib.md5(prefix + rest).hexdigest(),
        )

        assert result["file_size"] == 10
        assert target.read_bytes() == prefix + rest  # 文件完好, 没被删

    def test_fresh_download_corrupt_md5_still_deletes(self, tmp_path, monkeypatch):
        """非续传路径的完整性校验语义保持: 内容不符仍删文件"""
        client = HTTPClient()
        target = tmp_path / "out.bin"

        fake_session = MagicMock()
        fake_session.request.return_value = _FakeResponse(200, [b"wrong-content"])
        monkeypatch.setattr(client, "_session", fake_session)

        from unified_downloader.exceptions import FileIntegrityError

        with pytest.raises(FileIntegrityError):
            client.download_file(
                url="https://example.com/f.bin",
                file_path=target,
                expected_md5=hashlib.md5(b"expected").hexdigest(),
            )
        assert not target.exists()


# ═══ 熔断器 ═══


class TestCircuitBreaker:
    def test_config_wired_through_manager(self):
        cfg = CircuitBreakerConfig(failure_threshold=10)
        manager = CircuitBreakerManager(default_config=cfg)
        # W40-#50: 之前 YAML 熔断配置被静默丢弃, 实际永远是默认 5
        assert manager.get_breaker("m").config.failure_threshold == 10

    def test_half_open_probe_budget_enforced(self):
        """半开状态最多放 half_open_max_calls 个试探请求"""
        from datetime import datetime, timedelta

        breaker = CircuitBreaker("m", CircuitBreakerConfig(
            failure_threshold=2, timeout=10, half_open_max_calls=3))
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open

        # 时间旅行跳过熔断冷却窗口 → 进入半开
        breaker._last_failure_time = datetime.now() - timedelta(seconds=20)
        assert breaker.is_half_open

        assert breaker.can_execute() is True   # 试探 1
        assert breaker.can_execute() is True   # 试探 2
        assert breaker.can_execute() is True   # 试探 3
        assert breaker.can_execute() is False  # 超出预算 — 之前无人调用此门控

    def test_concurrent_failures_open_breaker(self):
        """线程池并发打失败不再丢计数 (1000 次失败, 阈值 5, 必须开)"""
        breaker = CircuitBreaker("m", CircuitBreakerConfig(failure_threshold=5))

        def hammer():
            for _ in range(100):
                breaker.record_failure()

        threads = [threading.Thread(target=hammer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert breaker.is_open

    def test_concurrent_mixed_calls_no_corruption(self):
        breaker = CircuitBreaker("m", CircuitBreakerConfig(failure_threshold=100))

        def succeeder():
            for _ in range(200):
                breaker.record_success()

        def failer():
            for _ in range(200):
                breaker.record_failure()

        threads = [
            threading.Thread(target=succeeder) for _ in range(5)
        ] + [threading.Thread(target=failer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        status = breaker.get_status()
        assert 0 <= status["failure_count"] <= 1000  # 未 corrupt


# ═══ checkpoint ═══


class TestCheckpoint:
    def test_corrupt_checkpoint_returns_none_not_raise(self, tmp_path):
        mgr = CheckpointManager(tmp_path)
        cp = tmp_path / "task.json"
        cp.write_text('{"task_id": "task", "url', encoding="utf-8")  # 截断

        # W40-#50: 之前抛 CheckpointError 毒化该 task_id 的全部后续下载
        assert mgr.get("task") is None
        assert not cp.exists()  # 坏文件已清理

    def test_save_is_atomic_and_roundtrips(self, tmp_path):
        mgr = CheckpointManager(tmp_path)
        mgr.save("t1", "https://x", "/tmp/f", 100)

        data = mgr.get("t1")
        assert data["downloaded_bytes"] == 100
        assert list(tmp_path.glob("*.tmp")) == []  # 无残留 tmp


# ═══ 缓存 LRU 清理 ═══


class TestCacheLruCleanup:
    def test_same_day_overflow_evicts_lru(self, tmp_path):
        """单日写超 max_size 时按 last_access 淘汰 (之前 7 天清理删不掉任何
        今天创建的条目, 缓存无限增长)"""
        # 3 × 10KB = 30KB, 上限 ~21KB → 需淘汰 1 个
        mgr = CacheManager(tmp_path, ttl_days=30, max_size_gb=0.00002)
        payload = b"x" * 10240

        fa = tmp_path / "a.pdf"; fa.write_bytes(payload)
        fb = tmp_path / "b.pdf"; fb.write_bytes(payload)
        fc = tmp_path / "c.pdf"; fc.write_bytes(payload)

        mgr.put("m", "AAA", 2025, "10k", fa)
        mgr.put("m", "BBB", 2025, "10k", fb)
        # 访问 A → A 的 last_access 最新, B 变最旧
        mgr.get("m", "AAA", 2025, "10k")
        mgr.put("m", "CCC", 2025, "10k", fc)  # 触发超限清理

        assert mgr.get("m", "AAA", 2025, "10k") is not None  # 最新访问保留
        assert mgr.get("m", "CCC", 2025, "10k") is not None
        assert mgr.get("m", "BBB", 2025, "10k") is None      # 最久未用被淘汰
        assert mgr.get_size() <= mgr._max_size_bytes


# ═══ UnifiedDownloader: 标志恢复 / 缓存隔离 / config 接线 ═══


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # config/cache/audit 目录全落 tmp
    from unified_downloader.core.downloader import UnifiedDownloader
    from unified_downloader.core.config import get_default_config

    d = UnifiedDownloader(get_default_config())
    yield d
    d.close()


def _mock_adapter_success(d, tmp_path):
    from unified_downloader.models.enums import Market
    from unified_downloader.models.entities import DownloadResult

    out = tmp_path / "downloads" / "m" / "AAP" / "AAPL_2026_10K.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"<html>" + b"y" * 2048 + b"</html>")

    adapter = d._adapters[Market.M]
    result = DownloadResult(
        success=True, file_path=str(out), file_size=out.stat().st_size, source="edgar"
    )
    adapter.download = MagicMock(return_value=result)
    return adapter, result


class TestDownloaderP1:
    def test_breaker_config_from_config_yaml(self, downloader):
        # W40-#50: manager 拿到 config.circuit_breaker (之前被丢弃)
        breaker = downloader._circuit_breaker_manager.get_breaker("m")
        assert breaker.config is downloader.config.circuit_breaker

    def test_convert_to_pdf_flag_restored_after_call(self, downloader, tmp_path):
        adapter, _ = _mock_adapter_success(downloader, tmp_path)
        assert adapter._convert_to_pdf is False

        downloader.download("AAPL", 2026, "10k", convert_to_pdf=True)

        # W40-#50: 之前一次 True 永久污染, 后续所有调用也转 PDF
        assert adapter._convert_to_pdf is False

    def test_translate_flag_restored_after_call(self, downloader, tmp_path):
        adapter, _ = _mock_adapter_success(downloader, tmp_path)

        downloader.download("AAPL", 2026, "10k", translate=True)

        assert adapter._translate_enabled is False  # 恢复原值

    def test_use_cache_flag_restored_after_call(self, downloader, tmp_path):
        adapter, _ = _mock_adapter_success(downloader, tmp_path)
        adapter._use_translate_cache = True

        downloader.download("AAPL", 2026, "10k", use_cache=False)

        assert adapter._use_translate_cache is True  # 恢复, 不再永久禁用

    def test_cache_write_failure_does_not_fail_download(
        self, downloader, tmp_path, monkeypatch
    ):
        from unified_downloader.exceptions import CacheError

        _mock_adapter_success(downloader, tmp_path)
        monkeypatch.setattr(
            downloader._cache_manager, "put",
            MagicMock(side_effect=CacheError("database is locked")),
        )

        result = downloader.download("AAPL", 2026, "10k")

        # W40-#50: 之前穿透到 except → UNEXPECTED_ERROR + record_failure,
        # 连续 5 次误开熔断; 文件其实已完整落盘
        assert result.success is True
        assert not result.error_code
        breaker = downloader._circuit_breaker_manager.get_breaker("m")
        assert breaker.is_closed is True  # 缓存故障没有计入熔断

    def test_checkpoint_resume_failure_tolerated(self, downloader, tmp_path, monkeypatch):
        from unified_downloader.exceptions import CheckpointError

        _mock_adapter_success(downloader, tmp_path)
        monkeypatch.setattr(
            downloader._checkpoint_manager, "resume",
            MagicMock(side_effect=CheckpointError("boom")),
        )

        result = downloader.download("AAPL", 2026, "10k")

        # W40-#50: 之前该 task_id 的每一次下载都直接抛 CheckpointError
        assert result.success is True


# ═══ 异步: config 统一 / 熔断+缓存接线 / 429 重试 ═══


class TestAsyncDownloaderP1:
    def test_single_config_instance(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from unified_downloader.core.async_downloader import AsyncUnifiedDownloader

        ad = AsyncUnifiedDownloader()
        # W40-#50: 之前 self.config (裸 Config) 与内部 downloader 的
        # get_default_config() 是两个实例
        assert ad.config is ad._downloader.config

    def test_success_writes_cache_and_breaker(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from unified_downloader.core.async_downloader import AsyncUnifiedDownloader
        from unified_downloader.models.enums import Market
        from unified_downloader.models.entities import DownloadResult

        ad = AsyncUnifiedDownloader()
        out = tmp_path / "dl.html"
        out.write_bytes(b"<html>" + b"z" * 1024 + b"</html>")
        result = DownloadResult(success=True, file_path=str(out), file_size=2048)

        adapter = ad._downloader._adapters[Market.M]
        adapter.async_download = AsyncMock(return_value=result)
        put_mock = MagicMock()
        monkeypatch.setattr(ad._downloader._cache_manager, "put", put_mock)

        r = asyncio.run(
            ad.download("AAPL", 2026, "10k", market=Market.M, use_cache=True)
        )

        assert r.success is True
        put_mock.assert_called_once()  # W40-#50: 之前异步成果永远不进缓存

    def test_breaker_open_short_circuits_async(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from unified_downloader.core.async_downloader import AsyncUnifiedDownloader
        from unified_downloader.models.enums import Market

        ad = AsyncUnifiedDownloader()
        breaker = ad._downloader._circuit_breaker_manager.get_breaker("m")
        cfg = breaker.config
        for _ in range(cfg.failure_threshold):
            breaker.record_failure()

        adapter = ad._downloader._adapters[Market.M]
        adapter.async_download = AsyncMock()

        r = asyncio.run(ad.download("AAPL", 2026, "10k", market=Market.M))

        assert r.success is False
        assert r.error_code == "CIRCUIT_BREAKER_OPEN"
        adapter.async_download.assert_not_awaited()  # W40-#50: 之前完全绕过


class _FakeAsyncCtx:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}
        self.content_type = "application/octet-stream"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    async def read(self):
        return b"body"


class _FakeAsyncSession:
    def __init__(self, statuses):
        self._statuses = statuses
        self.calls = 0

    def request(self, method, url, **kwargs):
        status = self._statuses[min(self.calls, len(self._statuses) - 1)]
        self.calls += 1
        return _FakeAsyncCtx(status, headers={"Retry-After": "0"})


class TestAsync429Retry:
    def _patch_session(self, client, fake, monkeypatch):
        async def fake_prop(self):
            await asyncio.sleep(0)
            return fake

        monkeypatch.setattr(type(client), "session", property(fake_prop))

    def test_429_retries_then_succeeds(self, monkeypatch):
        """W40-#50: 异步版之前第一次 429 就抛 RateLimitError, 完全不重试"""
        from unified_downloader.exceptions import RateLimitError

        client = AsyncHTTPClient(max_retries=3)
        fake = _FakeAsyncSession([429, 200])
        self._patch_session(client, fake, monkeypatch)

        result = asyncio.run(client._request("GET", "https://example.com"))

        assert fake.calls == 2
        assert result["status"] == 200

    def test_429_exhausted_raises_without_sleeping_on_last(self, monkeypatch):
        from unified_downloader.exceptions import RateLimitError

        client = AsyncHTTPClient(max_retries=2)
        fake = _FakeAsyncSession([429, 429])
        self._patch_session(client, fake, monkeypatch)
        sleeps = []
        monkeypatch.setattr(client, "_sleep", AsyncMock(side_effect=lambda s: sleeps.append(s)))

        with pytest.raises(RateLimitError):
            asyncio.run(client._request("GET", "https://example.com"))

        assert fake.calls == 2
        assert len(sleeps) == 1  # 末次尝试不白睡
