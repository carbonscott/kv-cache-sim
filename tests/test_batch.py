"""Stage 2 tests: the batch runner's core -- feeding a command file (the REPL
grammar, plus `#` comments and blank-line commits) through one Session.

The batch runner does no work the Session doesn't already do; it just reads a
file and prints. So we drive a small in-test command list through a Session and
assert the same behaviour the runner relies on, then run the shipped example
file end to end.

Run from the project root:  python -m pytest tests/test_batch.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from cli.repl import default_state
from cli.session import Session
from sim.config import load_config
from sim.state import CacheState

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.json"
)
EXAMPLE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples",
    "warm-then-cold.txt",
)


@pytest.fixture
def config():
    return load_config(CONFIG_PATH)


def warm_session(config, prefix=50_000):
    state = CacheState(
        model="sonnet-4.6",
        effort="high",
        ttl_seconds=config.ttl_seconds["5m"],
        last_used=0.0,
        now=0.0,
        prefix_tokens=prefix,
        cached_tokens=prefix,
    )
    return Session(state, config)


def run_lines(session, lines):
    """Feed lines to a session the way run_batch.py does, collecting all output."""
    output = []
    for line in lines:
        output.extend(session.handle(line))
        if session.done:
            break
    return output


# -- comments and blank lines ------------------------------------------------

def test_comment_line_is_ignored(config):
    s = warm_session(config)
    assert s.handle("# this is a comment") == []
    assert s.turn_index == 0          # produced no event
    assert s.pending_input == 0       # and touched nothing


def test_comment_does_not_commit_pending_turn(config):
    s = warm_session(config)
    s.handle("user 1000")
    s.handle("# spacer comment, should NOT commit")
    assert s.turn_index == 0          # still pending
    assert s.pending_input == 1000

    s.handle("")                      # blank line commits
    assert s.turn_index == 1
    assert s.pending_input == 0


# -- warm vs cold through the runner -----------------------------------------

def test_warm_turn_reads_cached_prefix(config):
    s = warm_session(config, prefix=50_000)
    out = run_lines(s, ["user 1000", "assistant 400", ""])
    assert s.turn_index == 1
    # The cached 50K prefix is read, not rewritten.
    assert s.state.cached_tokens >= 50_000
    assert any("Turn(in=1000" in line for line in out)  # a ledger row was emitted


def test_post_advance_turn_is_cold_resume(config):
    s = warm_session(config, prefix=50_000)
    out = run_lines(s, ["advance 10m", "user 1200", "assistant 500", ""])
    assert any("cold resume" in line for line in out)


def test_quit_in_file_sets_done(config):
    s = warm_session(config)
    out = run_lines(s, ["user 100", "", "quit", "user 999"])
    assert s.done is True
    assert any("bye" in line for line in out)
    # The line after quit never ran (run loop broke on session.done).
    assert s.pending_input == 0


# -- the shipped example file runs end to end --------------------------------

def test_example_file_runs_to_a_positive_total(config):
    session = Session(default_state(config), config)
    with open(EXAMPLE_PATH) as f:
        run_lines(session, [line.rstrip("\n") for line in f])
    assert session.done is True              # the file ends in `quit`
    assert session.running_total > 0.0       # turns cost something
