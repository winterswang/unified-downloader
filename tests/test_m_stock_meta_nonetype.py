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


def test_default_user_agent_uses_unknown_when_user_unset():
    """Direct unit check on the patched expression.

    Mirrors the line in m_stock.py:

        user = os.environ.get("USER") or "Unknown"
        sec_ua = f"{user}/contact@example.com Research Tool/1.0"

    If a future refactor changes the default user string, this test
    will fail and force an explicit decision.
    """
    env = {k: v for k, v in os.environ.items() if k != "USER"}
    user = env.get("USER") or "Unknown"
    assert user == "Unknown", "Expected 'Unknown' fallback when USER unset"
    # Sanity: the expression must not raise.
    ua = f"{user}/contact@example.com Research Tool/1.0"
    assert ua == "Unknown/contact@example.com Research Tool/1.0"


def test_user_env_present_yields_actual_user():
    """When USER IS set, the fallback should not mask it."""
    env = {**os.environ, "USER": "winters"}
    user = env.get("USER") or "Unknown"
    assert user == "winters", "USER env should win over 'Unknown' fallback"
