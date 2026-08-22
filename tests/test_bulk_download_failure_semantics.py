"""
Regression tests for bulk_download.py 失败语义 (W40-#50).

之前 download_one 对「数据源确认无文档」和「重试耗尽的真实失败」返回同一个
None, 调用方一律记 skipped + 退出码 0 → scheduler 标 done, 真实失败
(网络/限流/超时) 被静默吞掉 (SEC 限流一小时 → 整批 "成功" 但零文件)。

修复后 download_one 返回 (path, reason):
- (None, "not_found") → skipped (合理)
- (None, "failed")    → download_failed + failures + 退出码 1
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bulk_download.py"


def load_bulk_module():
    spec = importlib.util.spec_from_file_location("bulk_download", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.log = logging.getLogger("test-bulk")
    return module


# ── download_one 契约 ──


def test_download_one_returns_not_found_on_keyword(monkeypatch, tmp_path):
    """数据源确认无文档 (关键词命中) → (None, "not_found"), 不重试"""
    bulk = load_bulk_module()
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)
    calls = []

    def fake_run(cmd, timeout=120):
        calls.append(cmd)
        return 1, "error: document not found for 2031", ""

    monkeypatch.setattr(bulk, "run", fake_run)

    assert bulk.download_one("AAPL", "US", "2031", "10k") == (None, "not_found")
    assert len(calls) == 1  # not_found 立即返回, 不烧重试


def test_download_one_returns_failed_after_retries(monkeypatch, tmp_path):
    """重试耗尽且无 not_found 关键词 → (None, "failed") (真实失败)"""
    bulk = load_bulk_module()
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)
    sleeps = []

    def fake_run(cmd, timeout=120):
        # rc!=0 且输出无关键词: 网络错误/限流/UNEXPECTED_ERROR 场景
        return 2, "Traceback ... RateLimitError: 429 Too Many Requests", ""

    monkeypatch.setattr(bulk, "run", fake_run)
    monkeypatch.setattr(bulk.time, "sleep", lambda s: sleeps.append(s))

    result = bulk.download_one("AAPL", "US", "2026", "10k", retries=3)

    assert result == (None, "failed")
    assert len(sleeps) == 3  # 确实重试满 3 次


def test_download_one_success_returns_path_and_none(monkeypatch, tmp_path):
    bulk = load_bulk_module()
    output = tmp_path / "downloads/m/AAP/AAPL_2026_10K.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"<html>" + b"x" * 512 + b"</html>")
    rel = output.relative_to(tmp_path)
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)

    def fake_run(cmd, timeout=120):
        return 0, f"✓ 下载成功\n  文件: {rel}", ""

    monkeypatch.setattr(bulk, "run", fake_run)

    assert bulk.download_one("AAPL", "US", "2026", "10k") == (output, None)


# ── process_stock 级: 真实失败必须进 failures (退出码非 0 的来源) ──


def _patch_bulk_for_process_stock(monkeypatch, tmp_path, download_one_result):
    """把 process_stock 的外部依赖全部打桩, 只测年度循环的失败标记逻辑"""
    bulk = load_bulk_module()

    monkeypatch.setattr(bulk, "STATE_DIR", tmp_path / "bulk_state")
    monkeypatch.setattr(bulk, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    # adr_map 解析: 纯美股, 无 ADR 特殊路由
    monkeypatch.setattr(bulk, "load_adr_map", lambda: {})
    monkeypatch.setattr(
        bulk, "resolve_target", lambda code, market, m: (code, "US", False, False)
    )

    cur_year = 2026
    monkeypatch.setattr(
        bulk, "search_docs", lambda *a, **k: [(str(cur_year), "10-K - 2026-02-01")]
    )
    monkeypatch.setattr(
        bulk, "download_one", lambda *a, **k: download_one_result
    )
    monkeypatch.setattr(bulk, "upload_ima", lambda *a, **k: True)
    return bulk


def test_real_download_failure_marks_failed_and_partial(monkeypatch, tmp_path):
    """重试耗尽 → download_failed + failures + status=partial_failure (退出码 1)"""
    bulk = _patch_bulk_for_process_stock(monkeypatch, tmp_path, (None, "failed"))

    state = bulk.process_stock(
        "AAPL", "US", "Apple", annual_years=1, quarterly_years=0
    )

    entry = state["annual"]["2026"]
    assert entry["status"] == "download_failed"
    assert entry["reason"] == "download failed after retries"
    assert any("2026 annual: download failed after retries" in f for f in state["failures"])
    assert state["status"] == "partial_failure"  # main() 据此退出码 1


def test_genuine_not_found_still_marks_skipped(monkeypatch, tmp_path):
    """数据源确认无文档 → 维持 skipped + status ok (不误报失败)"""
    bulk = _patch_bulk_for_process_stock(monkeypatch, tmp_path, (None, "not_found"))

    state = bulk.process_stock(
        "AAPL", "US", "Apple", annual_years=1, quarterly_years=0
    )

    entry = state["annual"]["2026"]
    assert entry["status"] == "skipped"
    assert state["status"] == "ok"
    assert state["failures"] == []


def test_quarterly_validation_failure_counts_as_failure(monkeypatch, tmp_path):
    """季报 validation 失败之前不计入 failures (年报分支计), 状态误判 ok"""
    bulk = _patch_bulk_for_process_stock(monkeypatch, tmp_path, (None, "not_found"))

    # 季报: 搜索显示 2026 可用, 但下载回来的是无效文件 (校验失败)
    bad_file = tmp_path / "downloads/m/AAP/AAPL_2026_10Q.html"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_bytes(b"<html>Access Denied</html>")  # validate_file 会拒绝

    def fake_download_one(code, market, year, rtype, retries=3):
        if rtype == "10q":
            return bad_file, None  # 下载"成功"但内容无效
        return None, "not_found"

    monkeypatch.setattr(bulk, "download_one", fake_download_one)

    state = bulk.process_stock(
        "AAPL", "US", "Apple", annual_years=0, quarterly_years=1
    )

    q_entry = state["quarterly"]["2026"]["Q1-Q3"]
    assert q_entry["status"] == "download_failed"
    assert q_entry["reason"] == "validation failed"
    assert any("2026 Q1-Q3: validation failed" in f for f in state["failures"])
    assert state["status"] == "partial_failure"
    assert not bad_file.exists()  # 无效文件已删除
