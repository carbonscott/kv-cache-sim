"""Tests for the REPL's pure Tab-completion helper.

readline's real Tab behavior needs a live terminal and isn't unit-testable, but
the command-matching logic is pure and easy to check here.

Run from the project root:  python -m pytest tests/test_repl.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.repl import command_completions, read_history, write_history
from cli.session import COMMAND_NAMES


def test_single_match():
    assert command_completions("us", "us") == ["user"]


def test_prefix_with_multiple_hits():
    # "c" matches all three (in COMMAND_NAMES order), and the hyphenated command is
    # offered whole rather than split at the '-'.
    assert command_completions("c", "c") == ["call", "compact", "clear-tools"]


def test_empty_lists_all_commands():
    assert command_completions("", "") == list(COMMAND_NAMES)


def test_call_grammar_replaces_assistant_and_send():
    # The call-as-atom grammar swap: `call` is a completion; the old lumped-turn
    # verbs `assistant`/`send` are gone.
    assert "call" in COMMAND_NAMES
    assert "assistant" not in COMMAND_NAMES
    assert "send" not in COMMAND_NAMES


def test_no_completion_past_the_command_word():
    assert command_completions("", "model ") == []


def test_no_match():
    assert command_completions("xyz", "xyz") == []


# -- save/load file I/O helpers ----------------------------------------------
# The interactive input() confirmation in the load intercept needs a live
# terminal and is verified manually; the pure file helpers are checked here.

def test_write_then_read_history_round_trips(tmp_path):
    history = ["user 1000", "send", "model opus-4.8", "reset"]
    path = str(tmp_path / "sess.txt")
    write_history(path, history)
    # The leading comment line is ignored by handle() on replay; read_history
    # returns it verbatim, so strip comments to compare the commands.
    lines = [line for line in read_history(path) if not line.startswith("#")]
    assert lines == history
