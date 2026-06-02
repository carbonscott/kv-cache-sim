"""Stage 2 tests: command parsing, the accumulate-then-send turn model, and that
the session drives the engine the same way direct events do.

Run from the project root:  python -m pytest tests/test_session.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from cli.session import Session, parse_duration
from sim.config import load_config
from sim.state import CacheState

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.json"
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


# -- duration parsing --------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [("90s", 90), ("6m", 360), ("1h", 3600), ("300", 300), (" 2m ", 120)],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


def test_parse_duration_rejects_junk():
    with pytest.raises(ValueError):
        parse_duration("soon")


# -- accumulate-then-send ----------------------------------------------------

def test_accumulate_then_send_builds_one_turn(config):
    s = warm_session(config, prefix=50_000)
    s.handle("user 1000")          # user input
    s.handle("tool read_file 1500") # a fake tool result
    s.handle("assistant 400")             # assistant output
    assert s.pending_input == 2_500
    assert s.pending_output == 400

    s.handle("send")
    # One committed turn: read 50K, write 50K-prefix-gap(0)+2500 input.
    assert s.turn_index == 1
    assert s.state.prefix_tokens == 50_000 + 2_500 + 400
    assert s.pending_input == 0 and s.pending_output == 0


def test_blank_line_commits_like_send(config):
    s = warm_session(config)
    s.handle("user 1000")
    s.handle("")  # blank line == send
    assert s.turn_index == 1
    assert s.pending_input == 0


def test_send_with_empty_pending_does_nothing(config):
    s = warm_session(config)
    out = s.handle("send")
    assert s.turn_index == 0
    assert any("nothing to send" in line for line in out)


def test_user_text_uses_tokenizer(config):
    s = warm_session(config)
    s.handle("user hello world this is some prose to tokenize")
    # Non-numeric -> tokenized; should be a small positive count, not the literal words.
    assert s.pending_input > 0


# -- session drives the engine like direct events ----------------------------

def test_advance_then_send_triggers_cold_resume(config):
    s = warm_session(config, prefix=50_000)  # 300s TTL
    s.handle("advance 10m")                  # > TTL
    s.handle("user 1000")
    out = s.handle("send")
    assert any("cold resume" in line for line in out)


def test_model_switch_invalidates(config):
    s = warm_session(config, prefix=50_000)
    s.handle("model opus-4.8")
    assert s.state.model == "opus-4.8"
    assert s.state.cached_tokens == 0


def test_unknown_model_is_rejected(config):
    s = warm_session(config)
    out = s.handle("model gpt-9")
    assert any("unknown model" in line for line in out)
    assert s.state.model == "sonnet-4.6"  # unchanged


def test_effort_switch_invalidates(config):
    s = warm_session(config, prefix=50_000)
    s.handle("effort medium")
    assert s.state.effort == "medium"
    assert s.state.cached_tokens == 0


def test_unknown_command_is_reported(config):
    s = warm_session(config)
    out = s.handle("frobnicate 3")
    assert any("unknown command" in line for line in out)


def test_quit_sets_done(config):
    s = warm_session(config)
    s.handle("quit")
    assert s.done is True


# -- Stage 3: upgrade --------------------------------------------------------

def test_upgrade_parses_and_invalidates(config):
    s = warm_session(config, prefix=50_000)
    out = s.handle("upgrade")
    assert s.state.cached_tokens == 0          # cache wiped
    assert any("Upgrade" in line for line in out)  # ledger row labels it Upgrade


def test_turn_after_upgrade_pays_full(config):
    s = warm_session(config, prefix=50_000)
    s.handle("upgrade")
    s.handle("user 1000")
    out = s.handle("send")
    # The post-upgrade turn is a full rebuild: no hits, the whole prefix rewritten.
    assert s.state.cached_tokens == 51_000
    assert any("Turn(in=1000" in line for line in out)
    # hit% reads 0 in the ledger row for this turn.
    assert any("  0%" in line for line in out)


# -- Stage 3: rewind ---------------------------------------------------------

def test_rewind_parses_and_re_hits(config):
    s = warm_session(config, prefix=50_000)
    s.handle("user 2000")
    s.handle("send")              # turn 1
    s.handle("user 1000")
    s.handle("send")              # turn 2
    cached_before = s.state.cached_tokens
    assert cached_before > 30_000

    out = s.handle("rewind 30000")
    assert s.state.prefix_tokens == 30_000
    assert s.state.cached_tokens == 30_000
    assert any("Rewind(to=30000)" in line for line in out)  # ledger label
    assert any("re-hit" in line for line in out)            # note appears

    # A subsequent turn shows a cache hit against the rewound prefix.
    s.handle("user 1000")
    out = s.handle("send")
    assert s.state.cached_tokens == 31_000
    assert not any("  0%" in line for line in out)  # hit% is non-zero


def test_rewind_bad_arg_is_rejected(config):
    s = warm_session(config, prefix=50_000)
    before = s.state
    out = s.handle("rewind")
    assert any("usage: rewind" in line for line in out)
    out = s.handle("rewind soon")
    assert any("usage: rewind" in line for line in out)
    assert s.state == before        # no state change
    assert s.turn_index == 0


# -- Stage 3: compact --------------------------------------------------------

def layered_session(config, system=8_000, prefix=50_000):
    """A warm session with a protected leading prefix of `system` tokens."""
    state = CacheState(
        model="sonnet-4.6",
        effort="high",
        ttl_seconds=config.ttl_seconds["5m"],
        last_used=0.0,
        now=0.0,
        prefix_tokens=prefix,
        cached_tokens=prefix,
        system_tokens=system,
    )
    return Session(state, config)


def test_compact_parses_and_re_anchors(config):
    s = layered_session(config, system=8_000, prefix=50_000)
    out = s.handle("compact 2000")
    assert any("Compact(summary=2000)" in line for line in out)  # ledger label
    assert any("compact" in line for line in out)                # note appears
    assert s.state.cached_tokens == 8_000                        # protected prefix kept

    # A subsequent turn re-hits the protected prefix (non-zero hit%).
    s.handle("user 1000")
    out = s.handle("send")
    assert not any("  0%" in line for line in out)


def test_compact_bad_arg_is_rejected(config):
    s = layered_session(config)
    before = s.state
    out = s.handle("compact")
    assert any("usage: compact" in line for line in out)
    out = s.handle("compact junk")
    assert any("usage: compact" in line for line in out)
    assert s.state == before
    assert s.turn_index == 0


# -- Stage 3: context-edit / tool-result clearing ----------------------------

def test_clear_tools_parses_and_invalidates(config):
    s = layered_session(config, system=8_000, prefix=50_000)
    out = s.handle("clear-tools 10000")
    assert any("ClearToolResults(freed=10000)" in line for line in out)  # ledger label
    assert any("cleared" in line for line in out)                        # note appears
    assert s.state.prefix_tokens == 40_000
    assert s.state.cached_tokens == 8_000

    # A subsequent turn re-hits the protected prefix (non-zero hit%).
    s.handle("user 1000")
    out = s.handle("send")
    assert not any("  0%" in line for line in out)


def test_clear_tools_bad_arg_is_rejected(config):
    s = layered_session(config)
    before = s.state
    out = s.handle("clear-tools")
    assert any("usage: clear-tools" in line for line in out)
    out = s.handle("clear-tools junk")
    assert any("usage: clear-tools" in line for line in out)
    assert s.state == before
    assert s.turn_index == 0
