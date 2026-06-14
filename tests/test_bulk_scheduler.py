"""Regression tests for bulk scheduler queue initialization."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def load_scheduler():
    path = Path(__file__).resolve().parent.parent / "scripts" / "bulk_scheduler.py"
    spec = importlib.util.spec_from_file_location("bulk_scheduler_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_init_queue_requires_explicit_source(tmp_path, monkeypatch):
    scheduler = load_scheduler()
    monkeypatch.setattr(scheduler, "QUEUE_FILE", tmp_path / "queue.json")

    with pytest.raises(SystemExit) as exc:
        scheduler.init_queue()

    assert "Queue source required" in str(exc.value)
    assert not scheduler.QUEUE_FILE.exists()


def test_init_queue_from_watchlist_file_writes_normalized_queue(tmp_path, monkeypatch):
    scheduler = load_scheduler()
    queue_file = tmp_path / "data" / "bulk_download_queue.json"
    monkeypatch.setattr(scheduler, "QUEUE_FILE", queue_file)
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "stocks": [
                    {"stock_code": "600519.SH", "stock_name": "贵州茅台", "market": "cn"},
                    {"code": "AAPL.US", "name": "Apple", "market": "US"},
                    {"code": "TCEHY.US", "name": "Tencent ADR", "market": "US"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    scheduler.init_queue(watchlist_file=str(watchlist))

    payload = json.loads(queue_file.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["pending"] == 2
    assert [s["code"] for s in payload["stocks"]] == ["600519.SH", "AAPL.US"]
    assert payload["stocks"][0]["market"] == "CN"


def test_morning_brief_loader_is_opt_in_and_has_actionable_error(monkeypatch):
    scheduler = load_scheduler()
    monkeypatch.delenv("MORNING_BRIEF_PATH", raising=False)

    with pytest.raises(SystemExit) as exc:
        scheduler.load_watchlist_from_morning_brief()

    assert "--from-morning-brief requires" in str(exc.value)
    assert "--watchlist-file" in str(exc.value)


def test_show_status_remains_read_only_for_existing_queue(tmp_path, monkeypatch, capsys):
    scheduler = load_scheduler()
    queue_file = tmp_path / "queue.json"
    queue_file.write_text(
        json.dumps({"total": 1, "done": 0, "failed": 0, "pending": 1, "stocks": [
            {"code": "AAPL.US", "name": "Apple", "market": "US", "status": "pending"}
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(scheduler, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(scheduler, "FAIL_LOG", tmp_path / "failures.log")

    before = queue_file.read_text(encoding="utf-8")
    scheduler.show_status()
    after = queue_file.read_text(encoding="utf-8")

    assert before == after
    assert "AAPL.US" in capsys.readouterr().out


def test_summary_treats_specific_ima_reason_as_failure():
    scheduler = load_scheduler()
    state = {
        "annual": {
            "2025": {"status": "uploaded", "ima": "uploaded"},
            "2024": {"status": "ima_failed", "ima": "semantic_upload_rate_limited"},
            "2023": {"status": "skipped"},
        },
        "summary": {"annual_ok": 2, "ima_ok": 2},  # stale optimistic summary
    }

    summary = scheduler.get_state_summary(state)

    assert summary["annual_ok"] == 1
    assert summary["annual_failed"] == 1
    assert summary["annual_skipped"] == 1
    assert summary["ima_ok"] == 1
    assert summary["ima_failed"] == 1


def test_recover_stale_processing_resets_rows_to_pending(tmp_path, monkeypatch):
    scheduler = load_scheduler()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(scheduler, "STATE_DIR", state_dir)
    (state_dir / "APP.json").write_text(
        json.dumps({"annual": {"2025": {"status": "uploaded", "ima": "uploaded"}}}),
        encoding="utf-8",
    )
    q = {
        "stocks": [
            {"code": "APP.US", "name": "AppLovin", "market": "US", "status": "processing"},
            {"code": "AAPL.US", "name": "Apple", "market": "US", "status": "done"},
        ]
    }

    recovered = scheduler.recover_stale_processing(q, max_age_seconds=0)

    assert recovered == ["APP.US"]
    assert q["stocks"][0]["status"] == "pending"
    assert q["stocks"][0]["recovered_from"] == "processing"
    assert q["pending"] == 1
    assert q["done"] == 1
    assert q["failed"] == 0


def test_process_next_outputs_cron_completion_marker_when_no_work(tmp_path, monkeypatch, capsys):
    scheduler = load_scheduler()
    queue_file = tmp_path / "queue.json"
    queue_file.write_text(
        json.dumps({"total": 1, "done": 1, "failed": 0, "pending": 0, "stocks": [
            {"code": "AAPL.US", "name": "Apple", "market": "US", "status": "done"}
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(scheduler, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(scheduler, "FAIL_LOG", tmp_path / "failures.log")

    scheduler.process_next()

    out = capsys.readouterr().out
    assert "All stocks processed!" in out


def test_process_next_recovers_processing_before_completion(tmp_path, monkeypatch, capsys):
    scheduler = load_scheduler()
    queue_file = tmp_path / "queue.json"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    queue_file.write_text(
        json.dumps({"total": 1, "done": 0, "failed": 0, "pending": 0, "stocks": [
            {"code": "ASML.US", "name": "ASML", "market": "US", "status": "processing"}
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "ASML.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(scheduler, "QUEUE_FILE", queue_file)
    monkeypatch.setattr(scheduler, "STATE_DIR", state_dir)
    monkeypatch.setattr(scheduler, "FAIL_LOG", tmp_path / "failures.log")
    monkeypatch.setattr(scheduler, "PROCESSING_STALE_SECONDS", 0)

    calls = []

    class Result:
        returncode = 0

    def fake_run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        (state_dir / "ASML.json").write_text(
            json.dumps({"annual": {"2025": {"status": "uploaded", "ima": "uploaded"}}}),
            encoding="utf-8",
        )
        return Result()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)

    scheduler.process_next()

    out = capsys.readouterr().out
    assert "Recovered stale processing rows: ASML.US" in out
    assert calls, "recovered pending row should be processed in the same run"
    payload = json.loads(queue_file.read_text(encoding="utf-8"))
    assert payload["stocks"][0]["status"] == "done"
    assert payload["done"] == 1
    assert payload["pending"] == 0
