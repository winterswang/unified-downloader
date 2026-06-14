#!/usr/bin/env python3
"""
批量下载调度器（细粒度状态版）
- 每小时 cron 触发
- 从队列取下一只处理
- 显示详细的 per-年/per-季度 状态
"""

from __future__ import annotations

import json, subprocess, sys
from subprocess import TimeoutExpired
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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


def init_queue():
    sys.path.insert(0, str(Path.home() / "code" / "morning-brief"))
    from src.utils.database import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT stock_code,stock_name,market FROM watchlist WHERE is_active=1 AND is_index=0 ORDER BY market,stock_code")
    rows = cur.fetchall()
    conn.close()

    queue = []
    for code, name, market in rows:
        if market == "US" and code in DEDUP_US:
            continue
        queue.append({"code":code,"name":name,"market":market,"status":"pending",
                       "annual_ok":0,"quarterly_ok":0,"failures":0})
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps({
        "created_at": datetime.now(TZ_CN).isoformat(),
        "total":len(queue),"pending":len(queue),"done":0,"failed":0,
        "stocks":queue
    }, ensure_ascii=False, indent=2))
    print(f"Queue: {len(queue)} stocks (CN:{sum(1 for s in queue if s['market']=='CN')} "
          f"HK:{sum(1 for s in queue if s['market']=='HK')} "
          f"US:{sum(1 for s in queue if s['market']=='US')})")


def _find_state(code: str) -> Path | None:
    """查找状态文件（支持带/不带后缀的代码）"""
    sf = STATE_DIR / f"{code}.json"
    if sf.exists():
        return sf
    # Try stripping suffix
    clean = code.replace(".US","").replace(".SH","").replace(".SZ","").replace(".HK","")
    sf2 = STATE_DIR / f"{clean}.json"
    return sf2 if sf2.exists() else None



def compute_summary_from_state(st: dict) -> dict:
    """Compute summary from per-document state when summary is missing/stale."""
    sm = {
        "annual_ok": 0, "annual_skipped": 0, "annual_failed": 0,
        "quarterly_ok": 0, "quarterly_skipped": 0, "quarterly_failed": 0,
        "ima_ok": 0, "ima_failed": 0,
    }
    for d in st.get("annual", {}).values():
        status = d.get("status", "")
        if status in ("downloaded", "uploaded"):
            sm["annual_ok"] += 1
        elif status == "skipped":
            sm["annual_skipped"] += 1
        elif "failed" in status:
            sm["annual_failed"] += 1
        if d.get("ima") == "uploaded":
            sm["ima_ok"] += 1
        elif d.get("ima") == "failed":
            sm["ima_failed"] += 1
    for qs in st.get("quarterly", {}).values():
        for d in qs.values():
            status = d.get("status", "")
            if status in ("downloaded", "uploaded"):
                sm["quarterly_ok"] += 1
            elif status == "skipped":
                sm["quarterly_skipped"] += 1
            elif "failed" in status:
                sm["quarterly_failed"] += 1
            if d.get("ima") == "uploaded":
                sm["ima_ok"] += 1
            elif d.get("ima") == "failed":
                sm["ima_failed"] += 1
    return sm


def get_state_summary(st: dict) -> dict:
    """Return a reliable summary even for older state files without summary."""
    sm = st.get("summary") or {}
    computed = compute_summary_from_state(st)
    if not sm or all(sm.get(k, 0) == 0 for k in ("annual_ok", "quarterly_ok", "ima_ok")):
        return computed
    return {**computed, **sm}


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

def show_status():
    if not QUEUE_FILE.exists():
        print("No queue. Run --init first.")
        return

    q = json.loads(QUEUE_FILE.read_text())
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
        if sf:
            st = json.loads(sf.read_text())
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
    st = json.loads(sf.read_text())
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


def process_next():
    if not QUEUE_FILE.exists():
        print("ERROR: no queue. Run --init first."); sys.exit(1)
    q = json.loads(QUEUE_FILE.read_text())
    idx = next((i for i,s in enumerate(q["stocks"]) if s["status"]=="pending"), None)
    if idx is None:
        print("All done!")
        show_status()
        return

    stock = q["stocks"][idx]
    code, name, market = stock["code"], stock["name"], stock["market"]
    stock["status"] = "processing"
    q["pending"] = max(0, q["pending"]-1)
    QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2))

    mflag = {"CN":"a","HK":"h","US":"m"}.get(market,"a")
    clean_code = code.replace(".US","").replace(".SH","").replace(".SZ","").replace(".HK","")

    print(f"\nProcessing: {code} ({name}) market={market}")
    print(f"Progress: {q['done']}/{q['total']} done, {q['pending']} pending\n")

    timed_out = False
    returncode = 1
    try:
        rc = subprocess.run(["python3", str(SCRIPT), "--code", clean_code,
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
    if sf:
        st = json.loads(sf.read_text())
        if timed_out:
            st["interrupted"] = {
                "reason": "scheduler timeout",
                "at": datetime.now(TZ_CN).isoformat(),
            }
            sf.write_text(json.dumps(st, ensure_ascii=False, indent=2))
        sm = get_state_summary(st)
        stock["annual_ok"] = sm.get("annual_ok",0)
        stock["quarterly_ok"] = sm.get("quarterly_ok",0)
        stock["failures"] = sm.get("annual_failed",0)+sm.get("quarterly_failed",0)
        stock["summary_text"] = build_stock_summary(code, stock, st, timed_out=timed_out)

    if returncode == 0:
        stock["status"] = "done"; q["done"] += 1
    else:
        stock["status"] = "failed"; q["failed"] += 1
        if timed_out:
            stock["failure_reason"] = "timeout"

    stock["updated_at"] = datetime.now(TZ_CN).isoformat()
    q["last_updated"] = datetime.now(TZ_CN).isoformat()
    QUEUE_FILE.write_text(json.dumps(q, ensure_ascii=False, indent=2))

    print(f"\n{stock['status']} {code} | A:{stock['annual_ok']} Q:{stock['quarterly_ok']} F:{stock['failures']}")
    if stock.get("summary_text"):
        print(stock["summary_text"])
    if q["pending"] <= 0:
        show_status()


def main():
    if "--init" in sys.argv:
        init_queue()
    elif "--status" in sys.argv:
        show_status()
    elif "--detail" in sys.argv:
        idx = sys.argv.index("--detail")+1
        show_detail(sys.argv[idx] if idx<len(sys.argv) else None)
    elif "--log" in sys.argv:
        if LOG_FILE.exists():
            print(LOG_FILE.read_text()[-5000:])
        else:
            print("No log yet")
    else:
        process_next()

if __name__ == "__main__":
    main()
