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
