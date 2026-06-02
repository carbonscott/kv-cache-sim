"""Tests for the REPL's pure Tab-completion helper.

readline's real Tab behavior needs a live terminal and isn't unit-testable, but
the command-matching logic is pure and easy to check here.

Run from the project root:  python -m pytest tests/test_repl.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.repl import command_completions
from cli.session import COMMAND_NAMES


def test_single_match():
    assert command_completions("us", "us") == ["user"]


def test_prefix_with_multiple_hits():
    # "c" matches both (in COMMAND_NAMES order), and the hyphenated command is
    # offered whole rather than split at the '-'.
    assert command_completions("c", "c") == ["compact", "clear-tools"]


def test_empty_lists_all_commands():
    assert command_completions("", "") == list(COMMAND_NAMES)


def test_no_completion_past_the_command_word():
    assert command_completions("", "model ") == []


def test_no_match():
    assert command_completions("xyz", "xyz") == []
