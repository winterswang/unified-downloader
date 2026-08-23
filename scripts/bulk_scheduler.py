#!/usr/bin/env python3
"""
批量下载调度器（细粒度状态版）
- 每小时 cron 触发
- 从队列取下一只处理
- 显示详细的 per-年/per-季度 状态
"""

from __future__ import annotations

import argparse, json, os, subprocess, sys
from subprocess import TimeoutExpired
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# W40-#50: compute_summary 统一到 bulk_common (原本地副本即严格版语义)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bulk_common import compute_summary as _common_compute_summary  # noqa: E402
QUEUE_FILE = PROJECT_ROOT / "data" / "bulk_download_queue.json"
STATE_DIR = PROJECT_ROOT / "data" / "bulk_state"
LOG_FILE = PROJECT_ROOT / "logs" / "bulk_download.log"
FAIL_LOG = PROJECT_ROOT / "logs" / "failures.log"
SCRIPT = PROJECT_ROOT / "scripts" / "bulk_download.py"
TZ_CN = timezone(timedelta(hours=8))

STATUS_ICONS = {
    "uploaded":  "✅", "downloaded": "📥", "skipped": "⬜",
    "download_failed": "❌", "ima_failed": "⚠️", "pending": "⬜",
}

DEDUP_US = {"TCEHY.US", "BEKE.US", "HTHT.US"}
PROCESSING_STALE_SECONDS = 2 * 3600
# W40-#50 P1: failed 不再是终态 — 与 bulk_download "Failed attempts must
# remain retryable in later cron runs" 的设计对齐。限制: 每只股票最多
# MAX_FAILED_ATTEMPTS 次, 且两次尝试间隔 >= FAILED_RETRY_BACKOFF_SECONDS,
# 避免坏股票每小时空转占掉整个 cron 窗口。
MAX_FAILED_ATTEMPTS = 5
FAILED_RETRY_BACKOFF_SECONDS = 4 * 3600


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp+rename 原子写。W40-#50 P1: 之前直接 write_text, 调度器超时 kill
    子进程可能落在写一半 → 截断 JSON → 下次 cron json.loads 崩溃且每次
    在同一点崩, 队列永久卡死需人工删文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_json_safe(path: Path):
    """读 JSON; 损坏 (截断/非法) 时移 aside 保留排查并返回 None。
    单个坏状态文件不应炸掉整个调度循环。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        corrupt = path.with_suffix(
            path.suffix + f".corrupt-{datetime.now(TZ_CN).strftime('%Y%m%d%H%M%S')}"
        )
        try:
            path.replace(corrupt)
            print(f"WARN: corrupted JSON moved aside: {path} -> {corrupt} ({e})")
        except OSError:
            print(f"WARN: corrupted JSON unreadable and could not be moved: {path} ({e})")
        return None


def _is_retryable_failed(stock: dict, now: datetime) -> bool:
    """failed 股票是否可重试: 未超尝试上限 且 距上次失败已过退避窗口。"""
    if stock.get("status") != "failed":
        return False
    if stock.get("attempts", 1) >= MAX_FAILED_ATTEMPTS:
        return False
    failed_at = _parse_iso_datetime(stock.get("failed_at"))
    if failed_at and (now - failed_at).total_seconds() < FAILED_RETRY_BACKOFF_SECONDS:
        return False
    return True


def _normalize_watchlist_rows(rows: list) -> list[tuple[str, str, str]]:
    """Normalize watchlist rows from JSON or external providers."""
    normalized = []
    for item in rows:
        if isinstance(item, dict):
            code = item.get("code") or item.get("stock_code") or item.get("symbol")
            name = item.get("name") or item.get("stock_name") or ""
            market = item.get("market")
        else:
            code, name, market = item[:3]
        if not code or not market:
            raise SystemExit(
                "Invalid watchlist row. Expected code/name/market fields, "
                f"got: {item!r}"
            )
        normalized.append((str(code), str(name), str(market).upper()))
    return normalized


def load_watchlist_file(path: str | Path) -> list[tuple[str, str, str]]:
    """Load watchlist rows from an explicit JSON file.

    Supported JSON shapes:
    - [{"code": "AAPL.US", "name": "Apple", "market": "US"}, ...]
    - {"stocks": [{"stock_code": "600519.SH", "stock_name": "贵州茅台", "market": "CN"}]}
    """
    watchlist_path = Path(path).expanduser()
    if not watchlist_path.exists():
        raise SystemExit(f"watchlist file not found: {watchlist_path}")
    data = json.loads(watchlist_path.read_text(encoding="utf-8"))
    rows = data.get("stocks", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit("watchlist JSON must be a list or an object with a 'stocks' list")
    return _normalize_watchlist_rows(rows)


def load_watchlist_from_morning_brief(path: str | Path | None = None) -> list[tuple[str, str, str]]:
    """Opt-in compatibility loader for a local morning-brief checkout."""
    raw_path = str(path or os.environ.get("MORNING_BRIEF_PATH", "")).strip()
    if not raw_path:
        raise SystemExit(
            "--from-morning-brief requires --morning-brief-path or MORNING_BRIEF_PATH. "
            "Prefer --watchlist-file for a decoupled queue source."
        )
    mb_path = Path(raw_path).expanduser()
    if not mb_path.exists():
        raise SystemExit(
            f"morning-brief not found at {mb_path}. Set MORNING_BRIEF_PATH, "
            "pass --morning-brief-path, or use --watchlist-file."
        )
    sys.path.insert(0, str(mb_path))
    try:
        from src.utils.database import get_connection
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"Cannot import morning-brief database helper from {mb_path}: {exc}. "
            "Use --watchlist-file to avoid cross-repo imports."
        ) from exc
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT stock_code,stock_name,market FROM watchlist WHERE is_active=1 AND is_index=0 ORDER BY market,stock_code")
        return _normalize_watchlist_rows(cur.fetchall())
    finally:
        conn.close()


def build_queue(rows: list[tuple[str, str, str]]) -> list[dict]:
    queue = []
    for code, name, market in rows:
        if market == "US" and code.upper() in DEDUP_US:
            continue
        queue.append({"code": code, "name": name, "market": market, "status": "pending",
                       "annual_ok": 0, "quarterly_ok": 0, "failures": 0})
    return queue


def write_queue(queue: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(QUEUE_FILE, json.dumps({
        "created_at": datetime.now(TZ_CN).isoformat(),
        "total": len(queue), "pending": len(queue), "done": 0, "failed": 0,
        "stocks": queue,
    }, ensure_ascii=False, indent=2))
    print(f"Queue: {len(queue)} stocks (CN:{sum(1 for s in queue if s['market']=='CN')} "
          f"HK:{sum(1 for s in queue if s['market']=='HK')} "
          f"US:{sum(1 for s in queue if s['market']=='US')})")


def init_queue(watchlist_file: str | None = None, from_morning_brief: bool = False,
               morning_brief_path: str | None = None):
    if watchlist_file:
        rows = load_watchlist_file(watchlist_file)
    elif from_morning_brief:
        rows = load_watchlist_from_morning_brief(morning_brief_path)
    else:
        raise SystemExit(
            "Queue source required. Use --init --watchlist-file FILE.json, or "
            "opt in to the legacy cross-repo source with --init --from-morning-brief "
            "--morning-brief-path PATH (or MORNING_BRIEF_PATH)."
        )
    write_queue(build_queue(rows))


def _find_state(code: str) -> Path | None:
    """查找状态文件（支持带/不带后缀的代码）"""
    sf = STATE_DIR / f"{code}.json"
    if sf.exists():
        return sf
    # Try stripping suffix
    clean = code.replace(".US","").replace(".SH","").replace(".SZ","").replace(".HK","")
    sf2 = STATE_DIR / f"{clean}.json"
    return sf2 if sf2.exists() else None



def _is_ima_success(value: str | None) -> bool:
    return value == "uploaded"


def _is_ima_failure(value: str | None) -> bool:
    return bool(value and value != "uploaded")


def compute_summary_from_state(st: dict) -> dict:
    """Compute summary from per-document state (W40-#50: 委托 bulk_common,
    语义不变 — 严格版本来就是从这里统一的)."""
    return _common_compute_summary(st)


def get_state_summary(st: dict) -> dict:
    """Return reliable summary using per-document records as source of truth."""
    return compute_summary_from_state(st)


def summarize_years(entries: dict, status_values: set) -> list[str]:
    return sorted(
        [y for y, d in entries.items() if d.get("status") in status_values],
        reverse=True,
    )


def format_years(years: list[str]) -> str:
    return ", ".join(years) if years else "无"


def build_stock_summary(code: str, stock: dict, st: dict | None, timed_out: bool = False) -> str:
    """Human-readable precise summary for cron/user notification."""
    name = stock.get("name", "")
    if not st:
        return f"{code} {name}: 无状态文件"
    sm = get_state_summary(st)
    annual = st.get("annual", {})
    uploaded = summarize_years(annual, {"uploaded", "downloaded"})
    skipped = summarize_years(annual, {"skipped"})
    failed = sorted(
        [y for y, d in annual.items() if "failed" in d.get("status", "")],
        reverse=True,
    )
    parts = [
        f"{code} {name}: 年报成功 {len(uploaded)} ({format_years(uploaded)})",
    ]
    if skipped:
        parts.append(f"未找到/跳过 {len(skipped)} ({format_years(skipped)})")
    if failed:
        parts.append(f"失败 {len(failed)} ({format_years(failed)})")
    if timed_out or st.get("interrupted"):
        intr = st.get("interrupted", {})
        at = intr.get("at", "未知位置")
        parts.append(f"超时/中断于 {at}，下次需从未完成年份继续或重试")
    parts.append(f"IMA {sm.get('ima_ok', 0)}")
    return "；".join(parts)



def reconcile_queue_counts(q: dict) -> None:
    q["done"] = sum(1 for s in q.get("stocks", []) if s.get("status") == "done")
    q["failed"] = sum(1 for s in q.get("stocks", []) if s.get("status") == "failed")
    q["pending"] = sum(1 for s in q.get("stocks", []) if s.get("status") == "pending")


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ_CN)


def _processing_age_seconds(stock: dict, state_path: Path | None) -> float:
    updated = _parse_iso_datetime(stock.get("updated_at"))
    if updated:
        return (datetime.now(TZ_CN) - updated).total_seconds()
    if state_path and state_path.exists():
        return datetime.now(TZ_CN).timestamp() - state_path.stat().st_mtime
    if QUEUE_FILE.exists():
        return datetime.now(TZ_CN).timestamp() - QUEUE_FILE.stat().st_mtime
    return 0.0


def recover_stale_processing(q: dict, max_age_seconds: int = PROCESSING_STALE_SECONDS) -> list[str]:
    """Recover stale processing rows so cron can make forward progress.

    A previous cron run may exit while the child process keeps running or after a
    partial state write.  We only recover rows older than ``max_age_seconds`` to
    avoid racing a legitimately running cron invocation.
    """
    recovered = []
    for stock in q.get("stocks", []):
        if stock.get("status") != "processing":
            continue
        code = stock.get("code", "")
        sf = _find_state(code)
        if _processing_age_seconds(stock, sf) < max_age_seconds:
            continue
        st = _read_json_safe(sf) if sf and sf.exists() else None
        sm = get_state_summary(st) if st else {}
        failures = sm.get("annual_failed", 0) + sm.get("quarterly_failed", 0) + sm.get("ima_failed", 0)
        ok = sm.get("annual_ok", 0) + sm.get("quarterly_ok", 0)
        if failures > 0:
            stock["status"] = "pending"
            stock["recovered_from"] = "processing"
            stock["recovery_reason"] = "state_has_failures_retry"
        elif ok > 0:
            # Partial success should still be retried to finish any missing docs,
            # but the next run can reuse persisted state/cache.
            stock["status"] = "pending"
            stock["recovered_from"] = "processing"
            stock["recovery_reason"] = "partial_state_retry"
        else:
            stock["status"] = "pending"
            stock["recovered_from"] = "processing"
            stock["recovery_reason"] = "empty_or_missing_state_retry"
        stock["updated_at"] = datetime.now(TZ_CN).isoformat()
        recovered.append(code)
    if recovered:
        q["last_updated"] = datetime.now(TZ_CN).isoformat()
        reconcile_queue_counts(q)
    return recovered


def has_unfinished_queue_items(q: dict) -> bool:
    now = datetime.now(TZ_CN)
    for s in q.get("stocks", []):
        if s.get("status") in {"pending", "processing"}:
            return True
        if _is_retryable_failed(s, now):
            return True
    return False

def show_status():
    if not QUEUE_FILE.exists():
        print("No queue. Run --init first.")
        return

    q = _read_json_safe(QUEUE_FILE)
    if q is None:
        print("ERROR: queue file corrupted (moved aside). Run --init to rebuild.")
        return
    print(f"\n{'='*70}")
    print(f"  批量下载状态 — {q['total']} stocks  "
          f"✅{q['done']}  ❌{q['failed']}  ⬜{q['pending']}")
    print(f"{'='*70}\n")

    for s in q["stocks"]:
        marker = {"pending":"⬜","processing":"🔄","done":"✅","failed":"❌"}.get(s["status"],"?")
        code = s["code"]

        # 读取细粒度状态
        sf = _find_state(code)
        detail = ""
        if sf and sf.exists():
            st = _read_json_safe(sf)
            if st is None:
                detail = " (状态文件损坏已移出)"
                print(f"  {marker} {code:15s} {s['name']:12s} {s['market']}  {detail}")
                continue
            sm = get_state_summary(st)
            detail = f" A:{sm.get('annual_ok',0)}/{sm.get('annual_ok',0)+sm.get('annual_skipped',0)} "
            detail += f"Q:{sm.get('quarterly_ok',0)}/{sm.get('quarterly_ok',0)+sm.get('quarterly_skipped',0)} "
            detail += f"I:{sm.get('ima_ok',0)}"
            if sm.get("quarterly_failed",0)+sm.get("annual_failed",0)>0:
                detail += f" ❌{sm['quarterly_failed']+sm['annual_failed']}"

        print(f"  {marker} {code:15s} {s['name']:12s} {s['market']}  {detail}")

    # 失败日志摘要
    if FAIL_LOG.exists():
        lines = FAIL_LOG.read_text().strip().split("\n")
        recent = [l for l in lines if datetime.now(TZ_CN).strftime("%Y-%m-%d") in l]
        if recent:
            print(f"\n📋 今日失败记录 ({len(recent)} 条):")
            for l in recent[-5:]:
                print(f"  {l}")


def show_detail(code: str = None):
    """显示单只股票的详细状态"""
    if not code:
        print("Usage: --detail CODE")
        return
    sf = STATE_DIR / f"{code}.json"
    if not sf.exists():
        print(f"No state file for {code}")
        return
    st = _read_json_safe(sf)
    if st is None:
        print(f"State file for {code} was corrupted and has been moved aside.")
        return
    print(f"\n{'='*70}")
    print(f"  {code}  {st.get('name','?')}  market={st.get('market','?')}")
    print(f"  status={st.get('status','?')}  updated={st.get('updated_at','?')}")
    if st.get('adr_source'): print(f"  ADR source: {st['adr_source']}")
    if st.get('note'): print(f"  Note: {st['note']}")
    print(f"{'='*70}")

    # 年报
    print(f"\n📊 年报:")
    for y in sorted(st.get("annual",{}).keys(), reverse=True):
        d = st["annual"][y]
        icon = STATUS_ICONS.get(d.get("status",""), "?")
        info = f"  {icon} {y} {d.get('status','')}"
        if d.get("size"): info += f" ({d['size']//1024}KB)"
        if d.get("ima"): info += f" IMA:{d['ima']}"
        if d.get("reason"): info += f" — {d['reason'][:50]}"
        print(info)

    # 季报
    print(f"\n📋 季报:")
    for y in sorted(st.get("quarterly",{}).keys(), reverse=True):
        for q, d in st["quarterly"][y].items():
            icon = STATUS_ICONS.get(d.get("status",""), "?")
            info = f"  {icon} {y} {q} {d.get('status','')}"
            if d.get("size"): info += f" ({d['size']//1024}KB)"
            if d.get("ima"): info += f" IMA:{d['ima']}"
            if d.get("reason"): info += f" — {d['reason'][:50]}"
            print(info)

    # 汇总
    sm = get_state_summary(st)
    print(f"\n📈 Summary: A:{sm.get('annual_ok',0)}/{sm.get('annual_ok',0)+sm.get('annual_skipped',0)} "
          f"Q:{sm.get('quarterly_ok',0)}/{sm.get('quarterly_ok',0)+sm.get('quarterly_skipped',0)} "
          f"IMA:{sm.get('ima_ok',0)} Failed:{sm.get('annual_failed',0)+sm.get('quarterly_failed',0)}")


def _pick_retryable_failed(q: dict) -> int | None:
    """W40-#50 P1: 无 pending 时挑一个可重试的 failed 股票 (未超尝试上限
    且已过退避窗口)。之前 failed 是终态, 后续 cron 永不再碰 — 与
    bulk_download 自己声明的 '失败必须保持可重试' 直接矛盾。"""
    now = datetime.now(TZ_CN)
    for i, s in enumerate(q.get("stocks", [])):
        if _is_retryable_failed(s, now):
            return i
    return None


def process_next():
    if not QUEUE_FILE.exists():
        print("ERROR: no queue. Run --init first."); sys.exit(1)
    q = _read_json_safe(QUEUE_FILE)
    if q is None:
        # W40-#50 P1: 队列文件损坏 (之前截断 JSON 会让每次 cron 在同一行
        # 崩溃)。移 aside 后明确报错退出, 需人工 --init 重建, 但不再崩溃循环。
        print("ERROR: queue file corrupted (moved aside). Run --init to rebuild.")
        sys.exit(1)
    recovered = recover_stale_processing(q, PROCESSING_STALE_SECONDS)
    if recovered:
        _atomic_write_text(QUEUE_FILE, json.dumps(q, ensure_ascii=False, indent=2))
        print(f"Recovered stale processing rows: {', '.join(recovered)}")

    idx = next((i for i,s in enumerate(q["stocks"]) if s["status"]=="pending"), None)
    if idx is None:
        # W40-#50 P1: failed 可重试 — pending 空了先试退避窗口外的 failed
        idx = _pick_retryable_failed(q)
        if idx is not None:
            q["stocks"][idx]["status"] = "pending"
            print(f"Retrying previously failed stock: {q['stocks'][idx]['code']}")
    if idx is None:
        if has_unfinished_queue_items(q):
            print("No pending stocks; waiting for active processing rows, stale "
                  "recovery window, or failed-retry backoff.")
        else:
            print("All stocks processed!")
        show_status()
        return

    stock = q["stocks"][idx]
    code, name, market = stock["code"], stock["name"], stock["market"]
    stock["status"] = "processing"
    stock.pop("retry_of_failed", None)
    q["pending"] = max(0, q["pending"]-1)
    _atomic_write_text(QUEUE_FILE, json.dumps(q, ensure_ascii=False, indent=2))

    mflag = {"CN":"a","HK":"h","US":"m"}.get(market,"a")
    clean_code = code.replace(".US","").replace(".SH","").replace(".SZ","").replace(".HK","")

    print(f"\nProcessing: {code} ({name}) market={market}")
    print(f"Progress: {q['done']}/{q['total']} done, {q['pending']} pending\n")

    timed_out = False
    returncode = 1
    try:
        # W40-#50 P1: sys.executable 而非裸 "python3" (PATH 可能解析到
        # 没装 click/edgartools 的另一个解释器)
        rc = subprocess.run([sys.executable, str(SCRIPT), "--code", clean_code,
                             "--market", mflag, "--name", name,
                             "--annual-years", "10", "--quarterly-years", "5"],
                            cwd=str(PROJECT_ROOT), timeout=3600)
        returncode = rc.returncode
    except TimeoutExpired:
        timed_out = True
        returncode = 124
        print(f"\nTIMEOUT {code}: bulk_download exceeded 3600s")

    # 同步状态文件
    sf = _find_state(code)
    st = None
    if sf and sf.exists():
        st = _read_json_safe(sf)
    if st is not None:
        if timed_out:
            st["interrupted"] = {
                "reason": "scheduler timeout",
                "at": datetime.now(TZ_CN).isoformat(),
            }
            _atomic_write_text(sf, json.dumps(st, ensure_ascii=False, indent=2))
        sm = get_state_summary(st)
        stock["annual_ok"] = sm.get("annual_ok",0)
        stock["quarterly_ok"] = sm.get("quarterly_ok",0)
        stock["failures"] = sm.get("annual_failed",0)+sm.get("quarterly_failed",0)
        stock["summary_text"] = build_stock_summary(code, stock, st, timed_out=timed_out)

    if returncode == 0:
        stock["status"] = "done"; q["done"] += 1
    else:
        stock["status"] = "failed"; q["failed"] += 1
        stock["attempts"] = stock.get("attempts", 0) + 1
        stock["failed_at"] = datetime.now(TZ_CN).isoformat()
        if timed_out:
            stock["failure_reason"] = "timeout"

    stock["updated_at"] = datetime.now(TZ_CN).isoformat()
    q["last_updated"] = datetime.now(TZ_CN).isoformat()
    reconcile_queue_counts(q)
    _atomic_write_text(QUEUE_FILE, json.dumps(q, ensure_ascii=False, indent=2))

    print(f"\n{stock['status']} {code} | "
          f"A:{stock.get('annual_ok', 0)} Q:{stock.get('quarterly_ok', 0)} "
          f"F:{stock.get('failures', 0)}")
    if stock.get("summary_text"):
        print(stock["summary_text"])
    if not has_unfinished_queue_items(q):
        print("All stocks processed!")
        show_status()
    elif q["pending"] <= 0:
        show_status()


def main():
    parser = argparse.ArgumentParser(description="Bulk download scheduler")
    parser.add_argument("--init", action="store_true", help="Initialize queue from an explicit source")
    parser.add_argument("--watchlist-file", help="JSON watchlist file for --init")
    parser.add_argument("--from-morning-brief", action="store_true", help="Opt in to loading watchlist from a local morning-brief checkout")
    parser.add_argument("--morning-brief-path", help="Path to morning-brief checkout; alternatively set MORNING_BRIEF_PATH")
    parser.add_argument("--status", action="store_true", help="Show queue status")
    parser.add_argument("--detail", help="Show detailed state for one code")
    parser.add_argument("--log", action="store_true", help="Show recent bulk log")
    args = parser.parse_args()

    if args.init:
        init_queue(args.watchlist_file, args.from_morning_brief, args.morning_brief_path)
    elif args.status:
        show_status()
    elif args.detail:
        show_detail(args.detail)
    elif args.log:
        if LOG_FILE.exists():
            print(LOG_FILE.read_text()[-5000:])
        else:
            print("No log yet")
    else:
        process_next()

if __name__ == "__main__":
    main()
