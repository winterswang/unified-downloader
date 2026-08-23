#!/usr/bin/env python3
"""bulk 链路共享工具 (W40-#50: 三脚本副本语义统一).

之前 run/compute_summary/upload_ima/_resolve_ima_script 在
bulk_download.py / bulk_scheduler.py / fix_us_html_ima.py 各有一份且
语义漂移 (bulk 把 downloaded 一律算 ok, scheduler 要求 ima 非失败才算
ok; fix 的 ✅ 判定宽松到"成功 0"也通过)。统一采用最严格的语义。

所有函数显式传参 (project_root/logger), 不读本模块级常量 — 调用方
(及其测试的 monkeypatch) 传入什么就用什么。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# 旧 openclaw 服务器路径, 仅作查找链 fallback (标准位置是仓库内
# scripts/sync_to_ima.sh, 见 resolve_ima_script)
LEGACY_IMA_SYNC_SCRIPT = (
    Path.home() / ".openclaw" / "workspace" / "skills"
    / "unified-downloader" / "scripts" / "sync_to_ima.sh"
)


def run(cmd, timeout: int = 120, project_root: Path | None = None):
    """跑子命令, TimeoutExpired 转 rc=124 不穿透 (超时隔离版语义)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(project_root) if project_root else None,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired as e:
        def _s(v):
            return v.decode("utf-8", "replace").strip() if isinstance(v, bytes) else (v or "").strip()
        err = _s(e.stderr) or f"timeout after {timeout}s"
        if f"timeout after {timeout}s" not in err:
            err = f"{err} (timeout after {timeout}s)"
        return 124, _s(e.stdout), err


def resolve_ima_script(project_root: Path) -> Path:
    """IMA 同步脚本查找链: 环境变量 → 仓库内 scripts/sync_to_ima.sh →
    旧 ~/.openclaw 路径。"""
    env = os.environ.get("IMA_SYNC_SCRIPT_PATH")
    if env:
        return Path(env)
    in_repo = Path(project_root) / "scripts" / "sync_to_ima.sh"
    if in_repo.exists():
        return in_repo
    return LEGACY_IMA_SYNC_SCRIPT


def _is_ima_success(value: str | None) -> bool:
    return value == "uploaded"


def _is_ima_failure(value: str | None) -> bool:
    return bool(value and value != "uploaded")


def compute_summary(state: dict) -> dict:
    """从 per-document 状态计算汇总 (scheduler 严格版语义).

    per-document 记录是 source of truth; IMA 失败可能是非常规 reason
    字符串 (如 semantic_upload_rate_limited), 不能只认 "failed"。
    """
    sm = {
        "annual_ok": 0, "annual_skipped": 0, "annual_failed": 0,
        "quarterly_ok": 0, "quarterly_skipped": 0, "quarterly_failed": 0,
        "ima_ok": 0, "ima_failed": 0,
    }
    for d in state.get("annual", {}).values():
        status = d.get("status", "")
        if status == "uploaded" or (
            status == "downloaded" and not _is_ima_failure(d.get("ima"))
        ):
            sm["annual_ok"] += 1
        elif status == "skipped":
            sm["annual_skipped"] += 1
        elif "failed" in status:
            sm["annual_failed"] += 1
        if _is_ima_success(d.get("ima")):
            sm["ima_ok"] += 1
        elif _is_ima_failure(d.get("ima")) or status == "ima_failed":
            sm["ima_failed"] += 1
    for qs in state.get("quarterly", {}).values():
        for d in qs.values():
            status = d.get("status", "")
            if status == "uploaded" or (
                status == "downloaded" and not _is_ima_failure(d.get("ima"))
            ):
                sm["quarterly_ok"] += 1
            elif status == "skipped":
                sm["quarterly_skipped"] += 1
            elif "failed" in status:
                sm["quarterly_failed"] += 1
            if _is_ima_success(d.get("ima")):
                sm["ima_ok"] += 1
            elif _is_ima_failure(d.get("ima")) or status == "ima_failed":
                sm["ima_failed"] += 1
    return sm


def upload_ima(fpath, kb: str = "年报季度报知识库",
               project_root: Path | None = None, logger=None) -> bool:
    """上传单文件到 IMA (完整判定版: 严格成功标记 + 显式失败/跳过标记)。

    sync_to_ima.sh 可能 exit 0 但 create_media 失败, 依赖 per-file
    成功/重复标记判定; "✅ 成功 [1-9]" 严格匹配 (宽松的 "✅ 出现" 会把
    "成功 0" 判成功)。
    """
    if logger is None:
        import logging
        logger = logging.getLogger("bulk")
    ima_script = resolve_ima_script(project_root)
    if not ima_script.exists():
        logger.error(
            "    IMA sync script missing: %s (set IMA_SYNC_SCRIPT_PATH or put "
            "scripts/sync_to_ima.sh in repo)", ima_script,
        )
        return False
    rc, out, err = run(
        ["bash", str(ima_script), "--file", str(fpath),
         "--kb-name", kb, "--force"],
        timeout=300, project_root=project_root,
    )
    combined = out + "\n" + err
    lower = combined.lower()

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
    ok = (rc == 0 and (success_mark or duplicate_ok)
          and not skipped_mark and not failure_mark)
    if ok:
        logger.info("    IMA: ✓ %s", Path(fpath).name)
    else:
        logger.error(
            "    IMA: ✗ %s rc=%s stdout=%s stderr=%s",
            Path(fpath).name, rc, out[-500:], err[-500:],
        )
    return ok
