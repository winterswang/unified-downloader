#!/usr/bin/env python3
"""Fix US annual filings that were marked uploaded while still being local HTML.

For each affected annual entry in data/bulk_state/*.json:
1. Re-download via unified_downloader with --pdf --no-cache so SEC HTML is converted to PDF.
2. Upload the PDF to IMA.
3. Update the per-stock state entry to the PDF path and real upload status.

This is intentionally limited to existing US annual entries with HTML file refs; it does
not do a full queue rerun and does not alter unrelated markets or pending stocks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "data" / "bulk_state"
QUEUE_FILE = PROJECT_ROOT / "data" / "bulk_download_queue.json"
LOG_DIR = PROJECT_ROOT / "logs"
FIX_LOG = LOG_DIR / "fix_us_html_ima.log"
DEFAULT_IMA_SYNC_SCRIPT = Path.home() / ".openclaw" / "workspace" / "skills" / "unified-downloader" / "scripts" / "sync_to_ima.sh"
IMA_SYNC_SCRIPT = Path(os.environ.get("IMA_SYNC_SCRIPT_PATH", DEFAULT_IMA_SYNC_SCRIPT))
TZ_CN = timezone(timedelta(hours=8))

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(FIX_LOG, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fix_us_html_ima")


def run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    r = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def load_queue_names() -> dict[str, str]:
    if not QUEUE_FILE.exists():
        return {}
    q = json.loads(QUEUE_FILE.read_text())
    return {s["code"].replace(".US", ""): s.get("name", "") for s in q.get("stocks", []) if s.get("market") == "US"}


def compute_summary(state: dict) -> dict:
    s = {
        "annual_ok": 0,
        "annual_skipped": 0,
        "annual_failed": 0,
        "quarterly_ok": 0,
        "quarterly_skipped": 0,
        "quarterly_failed": 0,
        "ima_ok": 0,
        "ima_failed": 0,
    }
    for d in state.get("annual", {}).values():
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
    for qs in state.get("quarterly", {}).values():
        for d in qs.values():
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


def should_fix(state: dict, year: str, entry: dict) -> bool:
    if state.get("market") != "US":
        return False
    f = entry.get("file", "")
    if not f.lower().endswith((".html", ".htm")):
        return False
    return entry.get("status") in ("uploaded", "downloaded", "ima_failed") or entry.get("ima") == "uploaded"


def convert_local_html(old_file: str) -> Path | None:
    """Convert existing local SEC HTML to PDF without re-downloading."""
    html = Path(old_file)
    if not html.is_absolute():
        html = PROJECT_ROOT / html
    if not html.exists() or html.suffix.lower() not in (".html", ".htm"):
        return None
    pdf = html.with_suffix(".pdf")
    if pdf.exists() and pdf.stat().st_size >= 100:
        return pdf
    try:
        from unified_downloader.infra.converter import HTMLToPDFConverter
        return HTMLToPDFConverter.convert(html, pdf_path=pdf, keep_original=True)
    except Exception as e:
        log.warning("local HTML→PDF failed old=%s err=%s", html, e)
        return None


def download_pdf(code: str, year: str, form_type: str) -> Path | None:
    cmd = [
        "python3", "-m", "unified_downloader.cli", "download", "single",
        code, "-y", year, "-t", form_type, "-m", "m", "--pdf", "--no-cache",
    ]
    rc, out, err = run(cmd, timeout=240)
    if rc != 0:
        log.error("download failed code=%s year=%s form=%s rc=%s stdout=%s stderr=%s", code, year, form_type, rc, out[-600:], err[-600:])
        return None
    m = re.search(r"文件[：:]\s*(\S+)", out)
    if not m:
        log.error("download output missing file path code=%s year=%s stdout=%s", code, year, out[-600:])
        return None
    p = PROJECT_ROOT / m.group(1)
    if not p.exists() or p.suffix.lower() != ".pdf" or p.stat().st_size < 100:
        log.error("download invalid pdf code=%s year=%s path=%s exists=%s", code, year, p, p.exists())
        return None
    return p


def upload_ima(pdf: Path, kb: str = "年报季度报知识库") -> bool:
    rc, out, err = run(["bash", str(IMA_SYNC_SCRIPT), "--file", str(pdf), "--kb-name", kb, "--force"], timeout=360)
    combined = out + "\n" + err
    success_mark = ("✅" in combined or "已添加到知识库" in combined or "Upload successful" in combined)
    skipped = ("跳过 " in combined or "skipped " in combined.lower()) and not success_mark
    ok = rc == 0 and not skipped and success_mark
    if not ok:
        log.error("IMA upload failed file=%s rc=%s stdout=%s stderr=%s", pdf.name, rc, out[-800:], err[-800:])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", action="append", help="Limit to base US ticker, e.g. AAPL. Can repeat.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wanted = {c.upper().replace(".US", "") for c in args.code or []}
    names = load_queue_names()
    targets: list[tuple[Path, str, str]] = []

    for sf in sorted(STATE_DIR.glob("*.json")):
        state = json.loads(sf.read_text())
        code = state.get("code", sf.stem).upper().replace(".US", "")
        if wanted and code not in wanted:
            continue
        if state.get("market") != "US":
            continue
        for year, entry in sorted(state.get("annual", {}).items(), reverse=True):
            if should_fix(state, year, entry):
                targets.append((sf, code, year))

    log.info("targets=%d", len(targets))
    if args.dry_run:
        for sf, code, year in targets:
            state = json.loads(sf.read_text())
            log.info("DRY %s %s %s", code, year, state["annual"][year].get("file"))
        return 0

    ok = fail = 0
    by_file: dict[Path, dict] = {}
    for sf, code, year in targets:
        state = by_file.get(sf)
        if state is None:
            state = json.loads(sf.read_text())
            by_file[sf] = state

        entry = state["annual"][year]
        old_file = entry.get("file")
        form = "20f" if "20F" in Path(old_file or "").name.upper() else "10k"
        log.info("fix %s %s form=%s old=%s", code, year, form, old_file)
        pdf = convert_local_html(old_file) or download_pdf(code, year, form)
        if not pdf:
            entry["status"] = "download_failed"
            entry["ima"] = "failed"
            entry["reason"] = "PDF conversion/download failed during US HTML IMA fix"
            fail += 1
        elif upload_ima(pdf):
            entry["status"] = "uploaded"
            entry["ima"] = "uploaded"
            entry["file"] = str(pdf)
            entry["size"] = pdf.stat().st_size
            entry["fixed_from"] = old_file
            entry["fixed_at"] = datetime.now(TZ_CN).isoformat()
            entry.pop("reason", None)
            ok += 1
            log.info("ok %s %s -> %s", code, year, pdf.name)
        else:
            entry["status"] = "ima_failed"
            entry["ima"] = "failed"
            entry["file"] = str(pdf)
            entry["size"] = pdf.stat().st_size
            entry["reason"] = "PDF generated but IMA upload failed during US HTML IMA fix"
            fail += 1

        state["summary"] = compute_summary(state)
        state["status"] = "partial_failure" if state["summary"]["ima_failed"] or state["summary"]["annual_failed"] else "ok"
        state["updated_at"] = datetime.now(TZ_CN).isoformat()
        state["name"] = state.get("name") or names.get(code, "")
        sf.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    log.info("done ok=%d fail=%d", ok, fail)
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
