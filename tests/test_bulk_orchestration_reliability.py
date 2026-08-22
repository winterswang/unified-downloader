"""
Regression tests for bulk 链路编排可靠性 (W40-#50 P1-编排批次).

覆盖四个生产 cron 场景缺陷 + python3 硬编码:
1. run() 超时隔离: TimeoutExpired 之前穿透炸掉整只股票
2. scheduler failed 可重试: 之前 failed 是终态, cron 永不再碰
3. 状态/队列 JSON 原子写 + 损坏容错: 之前超时 kill 写一半 → 截断 JSON →
   每次 cron 在同一行崩溃, 队列永久卡死
4. IMA 脚本查找链: 之前硬编码仓库外 ~/.openclaw 路径, 新机器部署即全量失败
5. sys.executable 替代裸 "python3"
"""
from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "_under_test"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    if hasattr(module, "log"):
        module.log = logging.getLogger(f"test-{name}")
    return module


# ═══ bulk_download.run() 超时隔离 ═══


def test_run_returns_124_on_timeout_instead_of_raising(monkeypatch):
    """TimeoutExpired 必须被吞掉并转为 rc=124, 不能炸调用方"""
    bulk = load_script("bulk_download.py")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=30, output=b"partial out",
                                        stderr=b"boom")

    monkeypatch.setattr(bulk.subprocess, "run", fake_run)

    rc, out, err = bulk.run(["echo", "hi"], timeout=30)

    assert rc == 124
    assert "partial out" in out          # bytes 输出正确解码
    assert "boom" in err and "timeout after 30s" in err


def test_run_timeout_with_none_output(monkeypatch):
    """TimeoutExpired stdout/stderr 为 None 时不崩"""
    bulk = load_script("bulk_download.py")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=10)

    monkeypatch.setattr(bulk.subprocess, "run", fake_run)

    rc, out, err = bulk.run(["echo"], timeout=10)
    assert rc == 124
    assert out == ""
    assert "timeout after 10s" in err


def test_download_one_timeout_counts_as_failure(monkeypatch, tmp_path):
    """全程超时的下载按 (None, "failed") 处理并重试满, 不抛异常"""
    bulk = load_script("bulk_download.py")
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)
    sleeps = []

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=kwargs.get("timeout", 120))

    monkeypatch.setattr(bulk.subprocess, "run", fake_run)
    monkeypatch.setattr(bulk.time, "sleep", lambda s: sleeps.append(s))

    result = bulk.download_one("AAPL", "US", "2026", "10k", retries=3)

    assert result == (None, "failed")
    assert len(sleeps) == 3


def test_search_docs_timeout_returns_empty(monkeypatch, tmp_path):
    """搜索超时返回空列表 (走 not-in-search 路径), 不炸整只股票"""
    bulk = load_script("bulk_download.py")
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=kwargs.get("timeout", 120))

    monkeypatch.setattr(bulk.subprocess, "run", fake_run)

    assert bulk.search_docs("AAPL", "US", "10k") == []


# ═══ bulk_download 状态文件原子写 + 损坏容错 ═══


def test_load_state_moves_corrupt_file_aside_and_starts_fresh(monkeypatch, tmp_path):
    bulk = load_script("bulk_download.py")
    state_dir = tmp_path / "bulk_state"
    state_dir.mkdir()
    monkeypatch.setattr(bulk, "STATE_DIR", state_dir)
    (state_dir / "AAPL.json").write_text('{"code": "AAPL", "annu', encoding="utf-8")  # 截断

    state = bulk.load_state("AAPL")

    assert state["code"] == "AAPL"
    assert state["annual"] == {} and state["quarterly"] == {}
    # 坏文件被移 aside, 原位置不再是损坏 JSON
    assert not (state_dir / "AAPL.json").exists() or bulk._read_json_safe(state_dir / "AAPL.json") is None
    aside = list(state_dir.glob("AAPL.json.corrupt-*"))
    assert len(aside) == 1, "损坏文件应保留为 .corrupt-* 以便排查"


def test_save_state_atomic_roundtrip_no_tmp_leftover(monkeypatch, tmp_path):
    bulk = load_script("bulk_download.py")
    state_dir = tmp_path / "bulk_state"
    state_dir.mkdir()
    monkeypatch.setattr(bulk, "STATE_DIR", state_dir)

    state = {"code": "AAPL", "annual": {"2026": {"status": "uploaded"}}}
    bulk.save_state(state)

    # 内容可完整读回
    saved = json.loads((state_dir / "AAPL.json").read_text(encoding="utf-8"))
    assert saved["annual"]["2026"]["status"] == "uploaded"
    # 不留 .tmp 残留
    assert list(state_dir.glob("*.tmp")) == []


# ═══ IMA 脚本查找链 ═══


def test_ima_script_env_override_wins(monkeypatch):
    bulk = load_script("bulk_download.py")
    monkeypatch.setenv("IMA_SYNC_SCRIPT_PATH", "/tmp/custom-sync.sh")
    assert str(bulk._resolve_ima_script()) == "/tmp/custom-sync.sh"


def test_ima_script_prefers_repo_copy_over_legacy(monkeypatch, tmp_path):
    """仓库内有 scripts/sync_to_ima.sh 时优先于 ~/.openclaw 旧路径"""
    bulk = load_script("bulk_download.py")
    monkeypatch.delenv("IMA_SYNC_SCRIPT_PATH", raising=False)
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "sync_to_ima.sh").write_text("#!/bin/bash\n")
    monkeypatch.setattr(bulk, "PROJECT_ROOT", repo)

    assert bulk._resolve_ima_script() == repo / "scripts" / "sync_to_ima.sh"


def test_ima_script_falls_back_to_legacy_openclaw_path(monkeypatch, tmp_path):
    """仓库内没有时回落到旧 ~/.openclaw 路径 (服务器兼容)"""
    bulk = load_script("bulk_download.py")
    monkeypatch.delenv("IMA_SYNC_SCRIPT_PATH", raising=False)
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)  # 无 scripts/sync_to_ima.sh

    assert bulk._resolve_ima_script() == bulk.LEGACY_IMA_SYNC_SCRIPT


def test_subprocess_cmds_use_current_interpreter(monkeypatch, tmp_path):
    """search/download 命令用 sys.executable 而非裸 "python3" """
    bulk = load_script("bulk_download.py")
    monkeypatch.setattr(bulk, "PROJECT_ROOT", tmp_path)
    cmds = []

    def fake_run(cmd, timeout=120):
        cmds.append(cmd)
        return 1, "not found", ""

    monkeypatch.setattr(bulk, "run", fake_run)

    bulk.search_docs("AAPL", "US", "10k")
    bulk.download_one("AAPL", "US", "2026", "10k", retries=0)

    assert all(c[0] == bulk.sys.executable for c in cmds)
    assert not any(c[0] == "python3" for c in cmds)


# ═══ scheduler: failed 可重试 ═══


def _scheduler():
    return load_script("bulk_scheduler.py")


def test_failed_stock_is_retryable_after_backoff():
    sched = _scheduler()
    old = (datetime.now(sched.TZ_CN) - timedelta(hours=5)).isoformat()
    stock = {"status": "failed", "attempts": 1, "failed_at": old}

    assert sched._is_retryable_failed(stock, datetime.now(sched.TZ_CN)) is True


def test_failed_stock_blocked_during_backoff_window():
    sched = _scheduler()
    fresh = (datetime.now(sched.TZ_CN) - timedelta(minutes=30)).isoformat()
    stock = {"status": "failed", "attempts": 1, "failed_at": fresh}

    assert sched._is_retryable_failed(stock, datetime.now(sched.TZ_CN)) is False


def test_failed_stock_capped_after_max_attempts():
    sched = _scheduler()
    old = (datetime.now(sched.TZ_CN) - timedelta(days=3)).isoformat()
    stock = {"status": "failed", "attempts": sched.MAX_FAILED_ATTEMPTS, "failed_at": old}

    assert sched._is_retryable_failed(stock, datetime.now(sched.TZ_CN)) is False


def test_pick_retryable_failed_skips_backoff_and_cap():
    sched = _scheduler()
    now = datetime.now(sched.TZ_CN)
    q = {"stocks": [
        {"code": "FRESH", "status": "failed", "attempts": 1,
         "failed_at": (now - timedelta(minutes=10)).isoformat()},        # 退避中
        {"code": "CAPPED", "status": "failed", "attempts": 99,
         "failed_at": (now - timedelta(days=1)).isoformat()},            # 超上限
        {"code": "OK", "status": "failed", "attempts": 1,
         "failed_at": (now - timedelta(hours=5)).isoformat()},           # 可重试
    ]}

    idx = sched._pick_retryable_failed(q)
    assert idx == 2


def test_has_unfinished_considers_retryable_failed():
    sched = _scheduler()
    retryable = {"status": "failed", "attempts": 1,
                 "failed_at": (datetime.now(sched.TZ_CN) - timedelta(hours=5)).isoformat()}
    exhausted = {"status": "failed", "attempts": sched.MAX_FAILED_ATTEMPTS,
                 "failed_at": (datetime.now(sched.TZ_CN) - timedelta(days=3)).isoformat()}

    assert sched.has_unfinished_queue_items({"stocks": [retryable]}) is True
    assert sched.has_unfinished_queue_items({"stocks": [exhausted]}) is False


def test_process_next_retries_failed_stock_when_no_pending(tmp_path, monkeypatch, capsys):
    """pending 空了要挑退避窗口外的 failed 重试, 且失败后 attempts 递增"""
    sched = _scheduler()
    queue_file = tmp_path / "queue.json"
    queue_file.write_text(json.dumps({
        "total": 1, "done": 0, "failed": 1, "pending": 0,
        "stocks": [{
            "code": "AAPL.US", "name": "Apple", "market": "US", "status": "failed",
            "attempts": 1,
            "failed_at": (datetime.now(sched.TZ_CN) - timedelta(hours=5)).isoformat(),
        }],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sched, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(sched, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(sched, "FAIL_LOG", tmp_path / "failures.log")

    class Result:
        returncode = 1  # 再次失败

    monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: Result())

    sched.process_next()

    out = capsys.readouterr().out
    assert "Retrying previously failed stock: AAPL.US" in out
    payload = json.loads(queue_file.read_text(encoding="utf-8"))
    stock = payload["stocks"][0]
    assert stock["status"] == "failed"
    assert stock["attempts"] == 2
    assert stock["failed_at"]


# ═══ scheduler: 队列/状态损坏容错 ═══


def test_process_next_corrupt_queue_exits_cleanly_not_crash_loop(tmp_path, monkeypatch, capsys):
    """截断的队列 JSON: 移 aside + 明确报错退出, 不再每次 cron 同点崩溃"""
    sched = _scheduler()
    queue_file = tmp_path / "queue.json"
    queue_file.write_text('{"total": 3, "stocks": [{"code": "AA', encoding="utf-8")
    monkeypatch.setattr(sched, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(sched, "STATE_DIR", tmp_path / "state")

    import pytest
    with pytest.raises(SystemExit):
        sched.process_next()

    assert "corrupted" in capsys.readouterr().out
    assert not queue_file.exists()
    assert len(list(tmp_path.glob("queue.json.corrupt-*"))) == 1


def test_recover_stale_processing_tolerates_corrupt_state_file(tmp_path, monkeypatch):
    """恢复 stale processing 时状态文件损坏 → 按空状态处理, 不崩调度器"""
    sched = _scheduler()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "ASML.json").write_text('{"annual": {"20', encoding="utf-8")  # 截断
    monkeypatch.setattr(sched, "STATE_DIR", state_dir)
    q = {"stocks": [{
        "code": "ASML.US", "name": "ASML", "market": "US", "status": "processing",
        "updated_at": (datetime.now(sched.TZ_CN) - timedelta(hours=3)).isoformat(),
    }]}

    recovered = sched.recover_stale_processing(q, max_age_seconds=0)

    assert recovered == ["ASML.US"]
    assert q["stocks"][0]["status"] == "pending"
    assert q["stocks"][0]["recovery_reason"] == "empty_or_missing_state_retry"


def test_scheduler_queue_write_is_atomic_no_tmp_leftover(tmp_path, monkeypatch):
    sched = _scheduler()
    queue_file = tmp_path / "queue.json"
    monkeypatch.setattr(sched, "QUEUE_FILE", queue_file)

    sched.write_queue([{"code": "AAPL.US", "name": "Apple", "market": "US"}])

    payload = json.loads(queue_file.read_text(encoding="utf-8"))
    assert payload["total"] == 1
    assert list(tmp_path.glob("*.tmp")) == []
