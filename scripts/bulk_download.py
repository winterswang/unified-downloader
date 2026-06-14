#!/usr/bin/env python3
"""
批量下载单只股票的年报/季报 → IMA 知识库（细粒度状态追踪版）。

用法:
    cd <repo-root> && python3 scripts/bulk_download.py \\
        --code 600519 --market a --name 贵州茅台

输出文件:
    data/bulk_state/{code}.json    每只股票独立状态文件（per年份/per季度）
    logs/bulk_download.log         全局结构化日志
    logs/failures.log              失败明细日志
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── 日志 ──
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def setup_logging(code: str):
    logger = logging.getLogger("bulk")
    logger.setLevel(logging.DEBUG)
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    # 文件 handler
    fh = logging.FileHandler(LOG_DIR / "bulk_download.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                       datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    # 控制台 handler（info 以上）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(ch)
    return logger

log = logging.getLogger("bulk")

# ── 路径常量 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "data" / "bulk_state"
ADR_MAP_FILE = PROJECT_ROOT / "config" / "adr_map.json"
DEFAULT_IMA_SYNC_SCRIPT = Path.home() / ".openclaw" / "workspace" / "skills" / "unified-downloader" / "scripts" / "sync_to_ima.sh"
IMA_SYNC_SCRIPT = Path(os.environ.get("IMA_SYNC_SCRIPT_PATH", DEFAULT_IMA_SYNC_SCRIPT))
TZ_CN = timezone(timedelta(hours=8))

ANNUAL_MAP = {"CN": "annual_report", "HK": "annual_report", "US": "10k", "US_20F": "20f"}
QUARTERLY_MAP = {
    "CN": [("q1_report", "Q1"), ("interim_report", "Q2"), ("q3_report", "Q3")],
    "HK": [("quarterly", "Q1+Q3"), ("interim_report", "Q2")],
    # US domestic issuers file 10-Q; foreign private issuers/ADRs commonly
    # disclose interim reports via 6-K. Try both and keep state idempotent.
    "US": [("10q", "Q1-Q3"), ("6k", "Q1-Q3")],
    "US_20F": [("6k", "Q1-Q3")],
}
MARKET_FLAGS = {"CN": "a", "HK": "h", "US": "m"}


def run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def load_adr_map():
    return json.loads(ADR_MAP_FILE.read_text()) if ADR_MAP_FILE.exists() else {}

def _us_key_variants(code):
    """Return normalized lookup keys for US tickers with/without .US suffix."""
    cu = code.upper()
    base = cu[:-3] if cu.endswith(".US") else cu
    return (cu, base, f"{base}.US")

def _lookup_us_map(mapping, code):
    for key in _us_key_variants(code):
        if key in mapping:
            return key, mapping[key]
    return None, None

def resolve_target(code, market, adr_map):
    mkt = market.upper()
    cu = code.upper()
    if mkt == "US":
        key, info = _lookup_us_map(adr_map.get("use_hk_source", {}), cu)
        if info:
            return info["hk_code"], "HK", True, False
        key, info = _lookup_us_map(adr_map.get("use_sec_20f_only", {}), cu)
        if info:
            base = cu[:-3] if cu.endswith(".US") else cu
            return base, "US", False, True
    return code, mkt, False, False

def quarterly_search_market(is_adr_hk, is_20f, mkt_key):
    if is_adr_hk:
        return "HK"
    if is_20f:
        return "US"
    return mkt_key

def search_docs(code, market, rtype, limit=50):
    flag = MARKET_FLAGS[market]
    rc, out, _ = run(["python3","-m","unified_downloader.cli","search","list",code,
                      "-t",rtype,"-m",flag,"-l",str(limit)], timeout=30)
    docs = []
    for line in out.split("\n"):
        # A/H outputs include Chinese dates ("2024 年"); US SEC outputs use
        # ISO filing dates ("10-Q - 2026-05-01", "6-K - 2025-08-25").
        m = re.search(r'(\d{4})\s*年', line) or re.search(r'\b(\d{4})-\d{2}-\d{2}\b', line)
        if m:
            docs.append((m.group(1), line.strip()))
    return docs

def download_one(code, market, year, rtype, retries=3):
    flag = MARKET_FLAGS[market]
    for n in range(retries + 1):
        if n > 0:
            delay = [10,30,60][n-1]
            log.warning("Retry %d/%d after %ds", n, retries, delay)
            time.sleep(delay)
        cmd = ["python3", "-m", "unified_downloader.cli", "download", "single",
               code, "-y", year, "-t", rtype, "-m", flag]
        # 美股 SEC 原始文件通常是 HTML；IMA 文件上传不接受本地 HTML。
        # PR #19 fixed cache-hit + --pdf so cache can be reused safely here.
        if market == "US":
            cmd.append("--pdf")
        rc, out, err = run(cmd, timeout=180 if market == "US" else 120)
        if rc != 0:
            log.warning("  download command failed rc=%s cmd=%s stdout=%s stderr=%s",
                        rc, " ".join(cmd), out[-500:], err[-500:])
        m = re.search(r'文件[：:]\s*(\S+)', out)
        fpath = PROJECT_ROOT / m.group(1) if m else None
        if rc == 0 and fpath and fpath.exists():
            fsize = fpath.stat().st_size
            log.info("  Downloaded: %s (%s KB)", fpath.name, fsize//1024)
            if market == "US" and fpath.suffix.lower() not in (".pdf",):
                log.error("  US download did not produce PDF: %s", fpath)
                return None
            return fpath
        combined = (out+err).lower()
        if any(kw in combined for kw in ["not found","no document","no data","无数据","no matching","暂无"]):
            return None
    log.error("  FAILED after %d retries", retries)
    return None

def validate_file(fpath):
    if not fpath or not fpath.exists():
        return False
    if fpath.stat().st_size < 100:
        return False
    if fpath.suffix == '.html':
        text = fpath.read_text()[:2000]
        if any(kw in text for kw in ["Access Denied", "403", "Page Not Found"]):
            return False
    return True

def upload_ima(fpath, kb="年报季度报知识库"):
    if not IMA_SYNC_SCRIPT.exists():
        log.error("    IMA sync script missing: %s", IMA_SYNC_SCRIPT)
        return False
    rc, out, err = run(["bash", str(IMA_SYNC_SCRIPT), "--file", str(fpath),
                        "--kb-name", kb, "--force"], timeout=300)
    combined = out + "\n" + err
    lower = combined.lower()

    # sync_to_ima.sh may exit 0 even when create_media fails; rely on
    # per-file success/duplicate markers and reject explicit failure summary.
    success_mark = (
        "Upload successful" in combined
        or "已添加到知识库" in combined
        or re.search(r"✅\s*成功\s*[1-9]", combined) is not None
    )
    duplicate_ok = "already exists" in lower or "已存在" in combined
    failure_mark = (
        "create_media 失败" in combined
        or "请求超量" in combined
        or re.search(r"失败\s*[1-9]", combined) is not None
    )
    skipped_mark = (
        "unsupported" in lower
        or "不支持" in combined
        or "Web pages must be added via URL" in combined
        or "跳过 1" in combined
        or "skipped 1" in lower
    )
    ok = (rc == 0 and (success_mark or duplicate_ok) and not skipped_mark and not failure_mark)
    if ok:
        log.info("    IMA: ✓ %s", fpath.name)
    else:
        log.error("    IMA: ✗ %s rc=%s stdout=%s stderr=%s",
                  fpath.name, rc, out[-500:], err[-500:])
    return ok

def log_failure(code, name, detail):
    p = LOG_DIR / "failures.log"
    t = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    with open(p, "a") as f:
        f.write(f"[{t}] {code} {name} | {detail}\n")

# ── 状态管理 ──

def load_state(code: str) -> dict:
    """加载单只股票的状态文件"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / f"{code}.json"
    if f.exists():
        return json.loads(f.read_text())
    return {
        "code": code, "annual": {}, "quarterly": {},
        "created_at": datetime.now(TZ_CN).isoformat(),
    }

def save_state(state: dict):
    code = state["code"]
    state["updated_at"] = datetime.now(TZ_CN).isoformat()
    (STATE_DIR / f"{code}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))

def set_doc_status(state: dict, category: str, year: str, quarter: str,
                   status: str, filepath=None, reason=None, fsize=None, form_type=None):
    """设置单份文档的状态"""
    if category == "annual":
        state.setdefault("annual", {})
        state["annual"].setdefault(year, {})
        entry = state["annual"][year]
        entry["status"] = status
        entry["updated"] = datetime.now(TZ_CN).isoformat()
        if filepath:
            entry["file"] = str(filepath)
        if fsize:
            entry["size"] = fsize
        if reason:
            entry["reason"] = reason
        if form_type:
            entry["form_type"] = form_type
        if status == "uploaded":
            entry["ima"] = "uploaded"
        elif status == "download_failed" or status == "ima_failed":
            entry["ima"] = "failed"
    else:
        state.setdefault("quarterly", {})
        state["quarterly"].setdefault(year, {})
        state["quarterly"][year].setdefault(quarter, {})
        entry = state["quarterly"][year][quarter]
        entry["status"] = status
        entry["updated"] = datetime.now(TZ_CN).isoformat()
        if filepath:
            entry["file"] = str(filepath)
        if fsize:
            entry["size"] = fsize
        if reason:
            entry["reason"] = reason
        if form_type:
            entry["form_type"] = form_type
        if status == "uploaded":
            entry["ima"] = "uploaded"
        elif status == "download_failed" or status == "ima_failed":
            entry["ima"] = "failed"


def _quarterly_entry(state, year, qlabel):
    return state.get("quarterly", {}).get(year, {}).get(qlabel, {})

def should_skip_quarterly_candidate(state, year, qlabel):
    """Skip fallback forms only after a successful material result.

    Failed attempts must remain retryable in later cron runs; unavailable
    fallback forms are prevented from overwriting failures by
    should_mark_quarterly_unavailable().
    """
    status = _quarterly_entry(state, year, qlabel).get("status")
    return status in ("uploaded", "downloaded")

def should_mark_quarterly_unavailable(state, year, qlabel, form_type=None):
    """Only mark unavailable when it will not hide a material failure.

    US fallback routing tries 10-Q and then 6-K under the same logical qlabel.
    If one form already downloaded but IMA failed, a later unavailable fallback
    must not rewrite that failure to skipped/not available. Old skipped markers
    and same-form failures remain retryable/overwritable.
    """
    entry = _quarterly_entry(state, year, qlabel)
    status = entry.get("status")
    existing_form = entry.get("form_type")
    if status in ("uploaded", "downloaded"):
        return False
    if status in ("ima_failed", "download_failed") and existing_form and form_type and existing_form != form_type:
        return False
    return True

def compute_summary(state: dict) -> dict:
    """从细粒度状态计算汇总"""
    s = {"annual_ok": 0, "annual_skipped": 0, "annual_failed": 0,
         "quarterly_ok": 0, "quarterly_skipped": 0, "quarterly_failed": 0,
         "ima_ok": 0, "ima_failed": 0}
    for y, d in state.get("annual", {}).items():
        st = d.get("status", "")
        if st in ("downloaded", "uploaded"):
            s["annual_ok"] += 1
        elif st == "skipped":
            s["annual_skipped"] += 1
        elif "failed" in st:
            s["annual_failed"] += 1
        if d.get("ima") == "uploaded":
            s["ima_ok"] += 1
        elif d.get("ima") == "failed":
            s["ima_failed"] += 1
    for y, qs in state.get("quarterly", {}).items():
        for q, d in qs.items():
            st = d.get("status", "")
            if st in ("downloaded", "uploaded"):
                s["quarterly_ok"] += 1
            elif st == "skipped":
                s["quarterly_skipped"] += 1
            elif "failed" in st:
                s["quarterly_failed"] += 1
            if d.get("ima") == "uploaded":
                s["ima_ok"] += 1
            elif d.get("ima") == "failed":
                s["ima_failed"] += 1
    return s

# ── 主流程 ──

def process_stock(code, market, name, annual_years=10, quarterly_years=5):
    global log
    log = setup_logging(code)

    adr_map = load_adr_map()
    dl_code, dl_mkt, is_adr_hk, is_20f = resolve_target(code, market, adr_map)
    mkt_key = "US_20F" if is_20f else dl_mkt
    ann_type = ANNUAL_MAP[mkt_key]
    qtypes = QUARTERLY_MAP[mkt_key]

    # 加载/初始化状态
    state = load_state(code)
    state["name"] = name
    state["market"] = market
    if is_adr_hk:
        state["adr_source"] = f"{dl_code}.HK"
    if is_20f:
        state["note"] = "ADR 20-F only"
    save_state(state)

    cur = datetime.now(TZ_CN).year
    search_mkt_for_ann = "US" if is_20f else mkt_key

    log.info("="*50)
    log.info("START %s (%s) market=%s", code, name, market)
    if is_adr_hk:
        log.info("  ADR → HK source: %s.HK", dl_code)
    if is_20f:
        log.info("  ADR 20-F only (no quarterly)")

    # ── 年报 ──
    log.info("📊 Annual (%dy)", annual_years)
    docs = search_docs(dl_code, search_mkt_for_ann, ann_type)
    avail = set(y for y,_ in docs)
    log.info("  Available: %s", sorted(avail, reverse=True) if avail else "none")
    failures = []

    for year in [str(y) for y in range(cur, cur - annual_years, -1)]:
        if year in state.get("annual", {}) and state["annual"][year].get("status") in ("uploaded","downloaded"):
            log.info("  [%s] Already done, skip", year)
            continue

        if year in avail:
            log.info("  [%s] Downloading...", year)
            fpath = download_one(dl_code, search_mkt_for_ann, year, ann_type)
            if fpath and validate_file(fpath):
                fsize = fpath.stat().st_size
                set_doc_status(state, "annual", year, "", "downloaded", fpath, fsize=fsize)
                save_state(state)
                if upload_ima(fpath):
                    set_doc_status(state, "annual", year, "", "uploaded", fpath, fsize=fsize)
                else:
                    set_doc_status(state, "annual", year, "", "ima_failed", fpath, fsize=fsize)
                    failures.append(f"{year} annual: IMA upload failed")
                    log_failure(code, name, f"{year} annual: IMA upload failed")
            elif fpath:
                fpath.unlink(missing_ok=True)
                set_doc_status(state, "annual", year, "", "download_failed", reason="validation failed")
                failures.append(f"{year} annual: validation failed")
                log_failure(code, name, f"{year} annual: validation failed")
            else:
                set_doc_status(state, "annual", year, "", "skipped", reason="download returned no file / not found")
        else:
            # Try download anyway
            log.info("  [%s] Not in search, trying...", year)
            fpath = download_one(dl_code, search_mkt_for_ann, year, ann_type)
            if fpath and validate_file(fpath):
                fsize = fpath.stat().st_size
                set_doc_status(state, "annual", year, "", "downloaded", fpath, fsize=fsize)
                save_state(state)
                if upload_ima(fpath):
                    set_doc_status(state, "annual", year, "", "uploaded", fpath, fsize=fsize)
                else:
                    set_doc_status(state, "annual", year, "", "ima_failed", fpath, fsize=fsize)
                    failures.append(f"{year} annual: IMA upload failed")
            elif fpath:
                fpath.unlink(missing_ok=True)
                set_doc_status(state, "annual", year, "", "skipped", reason="download invalid")
            else:
                set_doc_status(state, "annual", year, "", "skipped", reason="not available")
        save_state(state)

    # ── 季报 ──
    if qtypes:
        log.info("📋 Quarterly (%dy)", quarterly_years)
        search_mkt_for_q = quarterly_search_market(is_adr_hk, is_20f, mkt_key)

        for qtype, qlabel in qtypes:
            docs = search_docs(dl_code, search_mkt_for_q, qtype)
            avail = set(y for y,_ in docs)
            log.info("  %s available: %s", qlabel, sorted(avail, reverse=True) if avail else "none")

            for year in [str(y) for y in range(cur, cur - quarterly_years, -1)]:
                # 检查是否已处理
                if should_skip_quarterly_candidate(state, year, qlabel):
                    log.info("  [%s %s] Already has material result, skip", year, qlabel)
                    continue

                if year in avail:
                    log.info("  [%s %s] Downloading...", year, qlabel)
                    fpath = download_one(dl_code, search_mkt_for_q, year, qtype)
                    if fpath and validate_file(fpath):
                        fsize = fpath.stat().st_size
                        set_doc_status(state, "quarterly", year, qlabel, "downloaded", fpath, fsize=fsize, form_type=qtype)
                        save_state(state)
                        if upload_ima(fpath):
                            set_doc_status(state, "quarterly", year, qlabel, "uploaded", fpath, fsize=fsize, form_type=qtype)
                        else:
                            set_doc_status(state, "quarterly", year, qlabel, "ima_failed", fpath, fsize=fsize, form_type=qtype)
                            failures.append(f"{year} {qlabel}: IMA upload failed")
                    elif fpath:
                        fpath.unlink(missing_ok=True)
                        set_doc_status(state, "quarterly", year, qlabel, "download_failed", reason="validation failed", form_type=qtype)
                    else:
                        set_doc_status(state, "quarterly", year, qlabel, "skipped", reason="not available", form_type=qtype)
                else:
                    if should_mark_quarterly_unavailable(state, year, qlabel, qtype):
                        set_doc_status(state, "quarterly", year, qlabel, "skipped", reason="not available", form_type=qtype)
                    else:
                        log.info("  [%s %s] Not available, keeping existing material result", year, qlabel)
                save_state(state)
    else:
        log.info("📋 Quarterly: N/A (20-F only)")

    # ── 汇总 ──
    summary = compute_summary(state)
    state["summary"] = summary
    state["status"] = "partial_failure" if failures else "ok"
    state["failures"] = failures
    save_state(state)

    log.info("─"*50)
    log.info("SUMMARY %s: annual=%d/%d quarterly=%d/%d ima=%d failed=%d",
             code, summary["annual_ok"], summary["annual_ok"]+summary["annual_skipped"],
             summary["quarterly_ok"], summary["quarterly_ok"]+summary["quarterly_skipped"],
             summary["ima_ok"], len(failures))
    if failures:
        log.warning("FAILURES: %s", failures)
        print("\n__FEISHU_NOTIFY__")
        print(f"⚠️ {code} {name}")
        for f in failures:
            print(f"  • {f}")
        print("__END_FEISHU_NOTIFY__")

    return state


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--code", required=True)
    p.add_argument("--market", required=True, choices=["a","h","m","CN","HK","US"])
    p.add_argument("--name", default="")
    p.add_argument("--annual-years", type=int, default=10)
    p.add_argument("--quarterly-years", type=int, default=5)
    args = p.parse_args()
    mkt = {"a":"CN","h":"HK","m":"US","CN":"CN","HK":"HK","US":"US"}[args.market]
    result = process_stock(args.code, mkt, args.name, args.annual_years, args.quarterly_years)
    sys.exit(1 if result.get("status") == "partial_failure" else 0)

if __name__ == "__main__":
    main()
