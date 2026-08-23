"""Regression test for W36 PR #49: META NoneType fix.

Covers the None-user-agent crash that produced:

    unsupported operand type(s) for +: 'NoneType' and 'str'

when ``os.environ['USER']`` was unset (e.g. when OpenClaw's cron
wrapper or systemd strips the env before invoking the CLI).

The fix is a single-line change: `os.environ.get("USER")` →
`os.environ.get("USER") or "Unknown"`. This test pins the behaviour so
future refactors don't reintroduce the implicit None+str assumption.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# `unified-downloader` ships a console script. We invoke it the same way
# morning-brief's trigger_download does — via the CLI rather than
# importing the adapter directly — so we exercise the same code path
# that produced the production failure.
_CLI = "unified-downloader"


def _run_cli(*args: str, env: dict) -> subprocess.CompletedProcess:
    """Run `unified-downloader` with a controlled env, returning the result."""
    return subprocess.run(
        [_CLI, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def _base_env() -> dict:
    """Inherit the parent env, then strip USER (the trigger for the bug)."""
    env = os.environ.copy()
    env.pop("USER", None)
    # The CLI also reads SEC_USER_AGENT and EDGAR_IDENTITY; clear any
    # test-time leftovers so the default branch is exercised.
    env.pop("SEC_USER_AGENT", None)
    return env


def test_download_succeeds_without_user_env():
    """The exact failure mode from morning-brief 7-29 must be gone.

    Reproduces: USER env unset → META download → previously TypeError
    in m_stock sec_ua construction. After W36 PR #49 this should
    succeed (or fail for an unrelated reason like network, but never
    with the NoneType+str traceback).
    """
    env = _base_env()
    assert "USER" not in env, "test setup invariant: USER must be stripped"

    # We use AAPL here (not META) because AAPL has stable SEC filings and
    # the goal is to exercise the user-agent code path, not the network.
    # AAPL has the same code path through m_stock.get_annual_form_type
    # and the same headers construction.
    r = _run_cli("download", "single", "AAPL", "-y", "2025", "-t", "10q", "-m", "m", env=env)

    combined = (r.stdout or "") + (r.stderr or "")
    # The bug signature must not appear.
    assert "unsupported operand type(s) for +: 'NoneType' and 'str'" not in combined, (
        f"Regression: the USER=None TypeError is back.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    # Either we download successfully, or we hit a non-UA-related error
    # (network, SEC rate limit, etc.). What we MUST NOT see is the
    # "DOWNLOAD_ERROR" wrapper around the TypeError.
    assert "unsupported operand" not in combined


def test_default_user_agent_uses_unknown_when_user_unset(monkeypatch):
    """W40-#50 重写: 真实调用 _resolve_sec_user_agent (旧用例把被测
    表达式复制进断言自证, m_stock 真回归它照样绿)。"""
    sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
    from adapters.m_stock import _resolve_sec_user_agent

    class _NoUaCfg:
        sec_user_agent = None

    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)

    ua = _resolve_sec_user_agent(_NoUaCfg())

    assert ua == "Unknown/contact@example.com Research Tool/1.0"


def test_resolve_sec_user_agent_priority(monkeypatch):
    """三档优先级: config 显式 > SEC_USER_AGENT env > USER 兜底"""
    sys.path.insert(0, str(Path(__file__).parent.parent / "unified_downloader"))
    from adapters.m_stock import _resolve_sec_user_agent

    class _CfgUa:
        sec_user_agent = "Custom Agent/1.0"

    class _NoUaCfg:
        sec_user_agent = None

    # 1. config 显式配置最高优先
    assert _resolve_sec_user_agent(_CfgUa()) == "Custom Agent/1.0"

    # 2. SEC_USER_AGENT env 次之
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("SEC_USER_AGENT", "EnvAgent/2.0")
    assert _resolve_sec_user_agent(_NoUaCfg()) == "EnvAgent/2.0"

    # 3. USER 兜底构造
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("USER", "winters")
    assert _resolve_sec_user_agent(_NoUaCfg()) == (
        "winters/contact@example.com Research Tool/1.0"
    )
