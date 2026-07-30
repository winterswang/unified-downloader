"""Test that ensures click.Choice market values stay in sync with Market enum.

W36 PR #42 review found 5 places where click.Choice was hardcoded
['a', 'm', 'h'] and missed adding 'e' when Market.E was added. This test
fails loudly if a future PR adds a new Market value without updating
all the CLI Choice lists.

Run: python3 -m pytest tests/test_market_choice_consistency.py -v
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CLI_FILE = _PROJECT_ROOT / "unified_downloader" / "cli.py"
_ENUMS_FILE = _PROJECT_ROOT / "unified_downloader" / "models" / "enums.py"


def _extract_market_values() -> set[str]:
    """Read Market enum and return set of value strings.

    e.g. Market.A='a', Market.M='m', Market.H='h', Market.E='e' -> {'a','m','h','e'}
    """
    source = _ENUMS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    market_class = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Market":
            market_class = node
            break
    if market_class is None:
        raise RuntimeError("class Market not found in models/enums.py")

    values: set[str] = set()
    for stmt in market_class.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id.startswith(("A", "M", "H", "E", "U")):
                    # Market.A = "a" form
                    if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                        values.add(stmt.value.value)
    return values


def _extract_choice_values() -> list[tuple[int, set[str]]]:
    """Return list of (line_number, set_of_choice_values) for each click.Choice([...]) in cli.py.

    Only matches Choice where the literal contains a single-char string (likely market
    code) and at least one of {a, m, h}. Document-type Choice lists (annual_report,
    10k, etc.) and format Choice lists (table, json) are ignored.
    """
    source = _CLI_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    choices: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # click.Choice([...])  /  Choice([...]) in attribute call
        is_click_choice = (
            (isinstance(func, ast.Attribute) and func.attr == "Choice")
            or (isinstance(func, ast.Name) and func.id == "Choice")
        )
        if not is_click_choice:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.List):
            continue
        values: set[str] = set()
        for elt in first.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                v = elt.value
                if len(v) <= 2:  # market codes are 1-2 chars ('a','m','h','e','auto','all')
                    values.add(v)
        # Heuristic: only count Choice that looks like market codes
        # (must contain at least one of a/m/h)
        if values & {"a", "m", "h"}:
            choices.append((node.lineno, values))
    return choices


class MarketChoiceConsistencyTest(unittest.TestCase):
    def test_market_enum_has_expected_values(self):
        values = _extract_market_values()
        # Sanity: 4 real markets should exist after W36 PR #42. UNKNOWN
        # is an internal sentinel, present too.
        for v in ("a", "m", "h", "e", "unknown"):
            self.assertIn(v, values, f"Market value '{v}' must exist")
        # And exactly 5 (4 user + 1 sentinel) as of W36 7-30 baseline.
        self.assertEqual(
            values,
            {"a", "m", "h", "e", "unknown"},
            "Unexpected Market enum values. If a new Market was added, "
            "update this test AND every click.Choice in cli.py.",
        )

    def test_all_cli_market_choices_cover_every_market_value(self):
        """Every click.Choice with market codes must include every Market value.

        W36 PR #42 review bug: 5 hardcoded ['a','m','h'] Choice missed 'e'.
        This test fails if any Choice drops a Market value (or adds a stale
        code like 'auto'/'all' which are CLI-only and OK to differ).

        UNKNOWN is excluded because it's an internal sentinel for "market
        not recognized", not a user-selectable CLI option.
        """
        market_values = _extract_market_values() - {"unknown"}
        choices = _extract_choice_values()
        # 'auto' and 'all' are CLI-only sentinels, not Market values
        cli_sentinels = {"auto", "all"}

        errors: list[str] = []
        for lineno, values in choices:
            missing = market_values - values
            # CLI can add sentinels, so only fail on missing Market values
            if missing:
                errors.append(
                    f"cli.py:{lineno} Choice {sorted(values)} is missing Market values "
                    f"{sorted(missing)}"
                )
        if errors:
            self.fail(
                "W36 PR #42 review lesson: every click.Choice for market must cover "
                "every Market enum value. Failures:\n  " + "\n  ".join(errors)
            )

    def test_choice_count_is_stable(self):
        """Guard against accidental Choice deletion. 6 Choice should match W36 count."""
        choices = _extract_choice_values()
        # 6 Choice as of W36 7-30: 4 download-related + 1 file list + 1 reset
        self.assertEqual(
            len(choices),
            6,
            f"Expected 6 click.Choice with market codes (W36 7-30 baseline), "
            f"got {len(choices)}. If a new command was added, update this test.",
        )


if __name__ == "__main__":
    unittest.main()
