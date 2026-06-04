"""Stage 2 tests: command parsing, the call-as-atom grammar (free user/tool appends
billed by one `call`), and that the session drives the engine the same way direct
events do.

Run from the project root:  python -m pytest tests/test_session.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from cli.session import Session, default_state, parse_duration
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


# -- accumulate-then-call ----------------------------------------------------

def test_accumulate_then_call_builds_one_request(config):
    s = warm_session(config, prefix=50_000)
    s.handle("user 1000")           # free local append: +1 block
    s.handle("tool read_file 1500") # a fake tool result: +1 block
    assert s.pending_input == 2_500
    assert s.pending_input_blocks == 2

    s.handle("call 400")            # the one billed API request, out=400
    # One billed call: read 50K, write 50K-prefix-gap(0)+2500 input.
    assert s.turn_index == 1
    assert s.state.prefix_tokens == 50_000 + 2_500 + 400
    assert s.pending_input == 0 and s.pending_input_blocks == 0


def test_blank_line_is_a_noop(config):
    s = warm_session(config)
    s.handle("user 1000")
    out = s.handle("")  # blank line no longer commits anything
    assert out == []
    assert s.turn_index == 0
    assert s.pending_input == 1_000  # still pending


def test_call_without_out_is_rejected(config):
    s = warm_session(config)
    out = s.handle("call")
    assert any("usage: call" in line for line in out)
    assert s.turn_index == 0


def test_tool_counts_one_input_block(config):
    """A tool result is a single input block (only the tool_result is input; the
    tool_use block belongs to the requesting call's output)."""
    s = warm_session(config)
    s.handle("tool sql 124")
    assert s.pending_input_blocks == 1


def test_call_tu_sets_output_blocks(config):
    """call <out> tu=N emits N tool_use output blocks."""
    s = warm_session(config, prefix=50_000)
    s.handle("user 1000")
    captured = {}
    real_apply = s._apply

    def spy(event):
        captured["event"] = event
        return real_apply(event)

    s._apply = spy
    s.handle("call 200 tu=3")
    assert captured["event"].output_blocks == 3


def test_call_tu_zero_emits_one_text_block(config):
    """call <out> with no tu (or tu=0) emits one text block: max(1, tu)."""
    s = warm_session(config, prefix=50_000)
    s.handle("user 1000")
    captured = {}
    real_apply = s._apply

    def spy(event):
        captured["event"] = event
        return real_apply(event)

    s._apply = spy
    s.handle("call 200")
    assert captured["event"].output_blocks == 1


def test_call_zero_is_a_warm_up_turn(config):
    """call 0 is a valid pre-warm/warm-up request: it bills the accumulated input
    and emits no output tokens, but still counts as one call."""
    s = warm_session(config, prefix=50_000)
    s.handle("user 2000")
    s.handle("call 0")
    assert s.turn_index == 1
    assert s.state.prefix_tokens == 50_000 + 2_000  # input grew, no output
    assert s.pending_input == 0


def test_user_text_uses_tokenizer(config):
    s = warm_session(config)
    s.handle("user hello world this is some prose to tokenize")
    # Non-numeric -> tokenized; should be a small positive count, not the literal words.
    assert s.pending_input > 0


# -- session drives the engine like direct events ----------------------------

def test_advance_then_call_triggers_cold_resume(config):
    s = warm_session(config, prefix=50_000)  # 300s TTL
    s.handle("advance 10m")                  # > TTL
    s.handle("user 1000")
    out = s.handle("call 400")
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


def test_ttl_switch_changes_rate_without_invalidating(config):
    s = warm_session(config, prefix=50_000)
    s.handle("ttl 1h")
    assert s.state.ttl_seconds == config.ttl_seconds["1h"]   # 3600
    assert s.state.cached_tokens == 50_000                   # NOT invalidated


def test_unknown_ttl_is_rejected(config):
    s = warm_session(config)
    out = s.handle("ttl 2h")
    assert any("usage: ttl" in line for line in out)
    assert s.state.ttl_seconds == config.ttl_seconds["5m"]   # unchanged


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


def test_call_after_upgrade_pays_full(config):
    s = warm_session(config, prefix=50_000)
    s.handle("upgrade")
    s.handle("user 1000")
    out = s.handle("call 400")
    # The post-upgrade call is a full rebuild: no hits, the whole prefix rewritten.
    assert s.state.cached_tokens == 51_000
    assert any("Call(in=1000" in line for line in out)
    # hit% reads 0 in the ledger row for this call.
    assert any("  0%" in line for line in out)


# -- Stage 3: rewind ---------------------------------------------------------

def test_rewind_parses_and_re_hits(config):
    s = warm_session(config, prefix=50_000)
    s.handle("user 2000")
    s.handle("call 0")            # call 1
    s.handle("user 1000")
    s.handle("call 0")            # call 2
    cached_before = s.state.cached_tokens
    assert cached_before > 30_000

    out = s.handle("rewind 30000")
    assert s.state.prefix_tokens == 30_000
    assert s.state.cached_tokens == 30_000
    assert any("Rewind(to=30000)" in line for line in out)  # ledger label
    assert any("re-hit" in line for line in out)            # note appears

    # A subsequent call shows a cache hit against the rewound prefix.
    s.handle("user 1000")
    out = s.handle("call 0")
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

    # A subsequent call re-hits the protected prefix (non-zero hit%).
    s.handle("user 1000")
    out = s.handle("call 0")
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

    # A subsequent call re-hits the protected prefix (non-zero hit%).
    s.handle("user 1000")
    out = s.handle("call 0")
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


# -- reset: wipe everything back to a fresh cold session ---------------------

def test_reset_restores_defaults(config):
    # Warm session on a non-default model, with cost and calls accumulated.
    s = warm_session(config, prefix=50_000)
    s.handle("user 1000")
    s.handle("call 400")
    s.handle("user 500")
    s.handle("call 300")
    assert s.running_total > 0 and s.turn_index == 2

    out = s.handle("reset")
    assert any("session reset" in line for line in out)
    first_model = next(iter(config.models))
    assert s.state.model == first_model
    assert s.state.effort == "high"
    assert s.state.ttl_seconds == config.ttl_seconds["5m"]
    assert s.state.cached_tokens == 0
    assert s.state.prefix_tokens == 0
    assert s.running_total == 0.0
    assert s.turn_index == 0


def test_reset_clears_pending_input(config):
    s = warm_session(config)
    s.handle("user 1000")
    s.handle("tool read_file 500")
    assert s.pending_input == 1500 and s.pending_input_blocks == 2

    s.handle("reset")
    assert s.pending_input == 0 and s.pending_input_blocks == 0


def test_reset_sets_banner_flag(config):
    s = warm_session(config)
    assert s.just_reset is False
    s.handle("reset")
    assert s.just_reset is True


def test_reset_keeps_session_running(config):
    s = warm_session(config)
    s.handle("reset")
    assert s.done is False


# -- save/load: command-log recording and replay -----------------------------

def test_history_records_state_commands(config):
    s = warm_session(config)
    s.handle("user 1000")
    s.handle("tool read_file 500")
    s.handle("call 200")
    s.handle("model opus-4.8")
    s.handle("status")  # read-only: not recorded
    s.handle("help")    # read-only: not recorded
    assert s.history == [
        "user 1000",
        "tool read_file 500",
        "call 200",
        "model opus-4.8",
    ]
    assert "status" not in s.history
    assert "help" not in s.history


def test_blank_line_is_not_recorded(config):
    s = warm_session(config)
    s.handle("")  # blank line is a no-op: records nothing
    assert s.history == []

    s.handle("user 1000")
    s.handle("")  # still a no-op; only the call below bills and records
    s.handle("call 200")
    assert s.history == ["user 1000", "call 200"]


def cold_session(config):
    """A fresh cold session, like the REPL starts with -- so replay from a saved
    history (which also starts cold) reconstructs the same state."""
    return Session(default_state(config), config)


def test_load_commands_round_trips_state(config):
    # Drive an original cold session through several turns + a model switch.
    original = cold_session(config)
    original.handle("user 1000")
    original.handle("tool read_file 800")
    original.handle("call 200")
    original.handle("model opus-4.8")
    original.handle("user 500")
    original.handle("call 300 tu=2")
    history = list(original.history)

    # Replay the captured history into a fresh session.
    replayed = cold_session(config)
    replayed.load_commands(history)

    assert replayed.state.model == original.state.model
    assert replayed.state.effort == original.state.effort
    assert replayed.state.prefix_tokens == original.state.prefix_tokens
    assert replayed.state.cached_tokens == original.state.cached_tokens
    assert replayed.running_total == original.running_total
    assert replayed.turn_index == original.turn_index
    # A subsequent save round-trips: history matches the recordable lines replayed.
    assert replayed.history == history


def test_reset_keeps_history_and_records(config):
    s = warm_session(config, prefix=50_000)
    s.handle("user 1000")
    s.handle("call 200")
    s.handle("reset")
    # History keeps the pre-reset commands plus "reset", so replay reproduces it.
    assert s.history == ["user 1000", "call 200", "reset"]


def test_malformed_recordable_line_is_replay_safe(config):
    # A bad model name is a no-op when typed, but still recorded.
    original = cold_session(config)
    original.handle("user 1000")
    original.handle("call 200")
    original.handle("model gpt-9")  # rejected no-op
    assert "model gpt-9" in original.history

    replayed = cold_session(config)
    replayed.load_commands(original.history)
    assert replayed.state.model == original.state.model
    assert replayed.state.cached_tokens == original.state.cached_tokens
    assert replayed.turn_index == original.turn_index


# -- system: set the protected prefix on a cold session ----------------------

def test_system_sets_protected_prefix_records_and_round_trips(config):
    s = cold_session(config)
    out = s.handle("system 2000")
    assert s.state.system_tokens == 2_000
    assert any("2000" in line for line in out)
    assert "system 2000" in s.history          # recorded for save/replay

    # The cold guard rejects `system` once the conversation has grown.
    s.handle("user 7000")
    s.handle("call 0")                          # prefix_tokens now > 0
    refused = s.handle("system 3000")
    assert any("cold session" in line for line in refused)
    assert s.state.system_tokens == 2_000       # unchanged

    # A save -> load round-trip preserves the value.
    replayed = cold_session(config)
    replayed.load_commands(list(s.history))
    assert replayed.state.system_tokens == 2_000


def test_system_refuses_undersized_cold_first_call(config):
    s = cold_session(config)
    s.handle("system 2000")
    s.handle("user 500")
    out = s.handle("call 0")                  # prospective 500 < system 2000
    assert s.state.prefix_tokens == 0         # refused, not billed
    assert s.state.system_tokens == 2_000     # unchanged
    assert s.pending_input == 500             # preserved for a corrected call
    assert any("protected prefix" in line for line in out)
    s.handle("user 1500")                     # pending now 2000
    s.handle("call 0")                        # prospective 2000 == system → allowed
    assert s.state.prefix_tokens == 2_000
    assert s.state.system_tokens == 2_000


def test_rewind_below_system_is_refused(config):
    s = cold_session(config)
    s.handle("system 2000")
    s.handle("user 7000")
    s.handle("call 0")                        # prefix 7000, system 2000
    assert s.state.prefix_tokens == 7_000
    out = s.handle("rewind 500")              # inside the protected prefix
    assert s.state.prefix_tokens == 7_000     # unchanged, refused
    assert s.state.system_tokens == 2_000
    assert any("nothing to rewind" in line for line in out)
    s.handle("rewind 3000")                   # above the marker → allowed
    assert s.state.prefix_tokens == 3_000


# -- status reflects effective (TTL-aware) cache -----------------------------

def test_status_reports_zero_cached_after_idle_past_ttl(config):
    """Once the session is idle past its TTL, status shows cached=0 (the cache the
    next turn would actually find) rather than the stale snapshot, while the total
    prefix length is unchanged."""
    s = warm_session(config, prefix=50_000)  # ttl_seconds == 300
    s.handle("user 500")
    s.handle("call 0")                        # a real call; cache is warm at now=0

    warm_out = s.handle("status")
    assert any("cached=50500/50500 tok" in line for line in warm_out)

    s.handle("advance 300s")
    s.handle("advance 300s")                  # now=600s, idle past the 300s TTL
    expired_out = s.handle("status")
    assert any("cached=0/50500 tok" in line for line in expired_out)
    # The underlying prefix is untouched; only the displayed cache reads as cold.
    assert s.state.prefix_tokens == 50_500
    assert s.state.cached_tokens == 50_500
