"""Stage 1 engine tests: the validation target plus per-event bookkeeping.

Run from the project root:  python -m pytest tests/test_engine.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from sim.config import dollars, load_config
from sim.engine import apply_event
from sim.events import (
    Advance,
    ClearToolResults,
    Compact,
    Rewind,
    SwitchEffort,
    SwitchModel,
    Turn,
    Upgrade,
)
from sim.state import CacheState

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.json"
)


@pytest.fixture
def config():
    return load_config(CONFIG_PATH)


def warm_state(config, model="sonnet-4.6", prefix=50_000, ttl_key="5m"):
    """A warm session with `prefix` tokens already cached at time 0."""
    ttl = config.ttl_seconds[ttl_key]
    return CacheState(
        model=model,
        effort="high",
        ttl_seconds=ttl,
        last_used=0.0,
        now=0.0,
        prefix_tokens=prefix,
        cached_tokens=prefix,
    )


def test_validation_target_cached_turn_is_about_7x_cheaper(config):
    """research/05 section 2: 50K cached + 2K new on sonnet is ~7x cheaper than
    paying full base for all 51K input."""
    state = warm_state(config, model="sonnet-4.6", prefix=50_000)
    _, cost = apply_event(state, Turn(input_tokens=2_000, output_tokens=0), config)

    # Cached: 50K read at 0.1x ($0.30/MTok) + 2K write at 1.25x ($3.75/MTok).
    expected = dollars(50_000, 0.30) + dollars(2_000, 3.75)
    assert cost.total_cost == pytest.approx(expected)

    # No-cache baseline: all 51K at full base $3/MTok.
    no_cache = dollars(51_000, 3.0)
    ratio = no_cache / cost.total_cost
    assert ratio == pytest.approx(7.0, abs=0.5)


def test_turn_splits_read_and_write(config):
    """A warm turn reads the whole cached prefix and writes only the new input."""
    state = warm_state(config, prefix=50_000)
    _, cost = apply_event(state, Turn(input_tokens=2_000, output_tokens=500), config)

    assert cost.read_tokens == 50_000
    assert cost.write_tokens == 2_000
    assert cost.full_input_tokens == 0
    assert cost.output_tokens == 500
    assert cost.cache_hit_ratio == pytest.approx(50_000 / 52_000)


def test_output_is_cached_only_on_the_next_turn(config):
    """Turn 2's write must include turn 1's generated output."""
    state = warm_state(config, prefix=50_000)
    state, cost1 = apply_event(state, Turn(input_tokens=2_000, output_tokens=500), config)
    state, cost2 = apply_event(state, Turn(input_tokens=1_000, output_tokens=300), config)

    # Turn 1 wrote 2K (its input); turn 1's 500 output was NOT yet written.
    assert cost1.write_tokens == 2_000
    # Turn 2 reads the prefix up to turn 1's input breakpoint (50K + 2K)...
    assert cost2.read_tokens == 52_000
    # ...and writes turn 1's output (500) + turn 2's input (1000).
    assert cost2.write_tokens == 500 + 1_000
    # Hit ratio should climb as the cached prefix grows relative to new input.
    assert cost2.cache_hit_ratio > cost1.cache_hit_ratio


def test_ttl_expiry_forces_full_rebuild(config):
    """Idling past the TTL drops every cache hit on the next turn."""
    state = warm_state(config, prefix=50_000, ttl_key="5m")  # 300s TTL
    state, _ = apply_event(state, Advance(seconds=600), config)  # 10 min idle
    _, cost = apply_event(state, Turn(input_tokens=1_000, output_tokens=400), config)

    assert cost.read_tokens == 0
    assert cost.write_tokens == 50_000 + 1_000  # whole history + new input rewritten
    assert any("cold resume" in n for n in cost.notes)


def test_active_session_stays_warm_within_ttl(config):
    """A gap shorter than the TTL keeps the cache; each turn refreshes last_used."""
    state = warm_state(config, prefix=50_000, ttl_key="5m")
    state, _ = apply_event(state, Advance(seconds=200), config)
    state, cost = apply_event(state, Turn(input_tokens=1_000, output_tokens=0), config)
    assert cost.read_tokens == 50_000  # still warm


def test_switch_model_invalidates_at_zero_cost(config):
    """Switching model costs nothing itself but forces the next turn to rebuild."""
    state = warm_state(config, model="sonnet-4.6", prefix=50_000)
    state, switch_cost = apply_event(state, SwitchModel(model="opus-4.8"), config)

    assert switch_cost.total_cost == 0.0
    assert state.model == "opus-4.8"
    assert state.cached_tokens == 0

    _, cost = apply_event(state, Turn(input_tokens=1_000, output_tokens=0), config)
    assert cost.read_tokens == 0
    assert cost.write_tokens == 50_000 + 1_000
    # Billed at opus base ($5/MTok), confirming the model switch took effect.
    assert cost.write_cost == pytest.approx(dollars(51_000, 5.0 * 1.25))


def test_switch_model_to_same_model_keeps_cache(config):
    state = warm_state(config, model="sonnet-4.6", prefix=50_000)
    state, _ = apply_event(state, SwitchModel(model="sonnet-4.6"), config)
    assert state.cached_tokens == 50_000


def test_switch_effort_invalidates_but_noop_does_not(config):
    state = warm_state(config, prefix=50_000)  # effort="high"

    after_change, _ = apply_event(state, SwitchEffort(effort="medium"), config)
    assert after_change.cached_tokens == 0
    assert after_change.effort == "medium"

    after_noop, _ = apply_event(state, SwitchEffort(effort="high"), config)
    assert after_noop.cached_tokens == 50_000  # unchanged level keeps the cache


def test_one_hour_ttl_uses_2x_write_multiplier(config):
    """The write multiplier follows the active TTL."""
    state = warm_state(config, model="sonnet-4.6", prefix=0, ttl_key="1h")
    _, cost = apply_event(state, Turn(input_tokens=4_000, output_tokens=0), config)
    assert cost.write_cost == pytest.approx(dollars(4_000, 3.0 * 2.0))


# -- Stage 3: sub-minimum no-cache -------------------------------------------

def cold_state(config, model="sonnet-4.6", ttl_key="5m"):
    """A fresh, cold, empty session (no prefix, no cache)."""
    return CacheState(
        model=model,
        effort="high",
        ttl_seconds=config.ttl_seconds[ttl_key],
        last_used=0.0,
        now=0.0,
        prefix_tokens=0,
        cached_tokens=0,
    )


def test_sub_minimum_first_turn_is_not_cached(config):
    """A tiny first turn whose prefix is below min_cacheable establishes no entry:
    read==write==0, the whole input billed at 1.0x base, and no breakpoint."""
    state = cold_state(config, model="sonnet-4.6")  # min_cacheable 1024
    new_state, cost = apply_event(state, Turn(input_tokens=500, output_tokens=100), config)

    assert cost.read_tokens == 0
    assert cost.write_tokens == 0
    assert cost.full_input_tokens == 500
    # Billed at full base (sonnet $3/MTok), no read/write multiplier applied.
    assert cost.full_input_cost == pytest.approx(dollars(500, 3.0))
    assert cost.total_cost == pytest.approx(dollars(500, 3.0) + dollars(100, 15.0))
    assert cost.cache_hit_ratio == 0.0
    assert any("sub-minimum" in n for n in cost.notes)

    # No cache entry established.
    assert new_state.cached_tokens == 0
    assert new_state.breakpoints == ()


def test_above_minimum_turn_caches_normally(config):
    """Just over the minimum, the turn caches as in Stage 1 (sanity boundary check)."""
    state = cold_state(config, model="sonnet-4.6")
    _, cost = apply_event(state, Turn(input_tokens=2_000, output_tokens=0), config)
    assert cost.full_input_tokens == 0
    assert cost.write_tokens == 2_000  # cold rebuild writes the whole prefix


# -- Stage 3: upgrade cold resume --------------------------------------------

def test_upgrade_invalidates_cache_within_ttl(config):
    """An Upgrade zeroes the cache for free, even with time left on the TTL."""
    state = warm_state(config, prefix=50_000)  # now == last_used, well within TTL
    new_state, cost = apply_event(state, Upgrade(), config)

    assert cost.total_cost == 0.0
    assert new_state.cached_tokens == 0
    assert new_state.breakpoints == ()
    assert any("upgrade" in n.lower() for n in cost.notes)


def test_turn_after_upgrade_is_a_full_write(config):
    """The post-upgrade turn pays a full write with no hits, despite being in TTL."""
    state = warm_state(config, prefix=50_000)
    state, _ = apply_event(state, Upgrade(), config)
    _, cost = apply_event(state, Turn(input_tokens=1_000, output_tokens=400), config)

    assert cost.read_tokens == 0
    assert cost.write_tokens == 50_000 + 1_000  # whole history + new input rewritten
    assert cost.cache_hit_ratio == 0.0


# -- Stage 3: breakpoint maintenance -----------------------------------------

def test_warm_turn_keeps_trailing_breakpoint_consistent(config):
    """A warm turn's trailing breakpoint equals cached_tokens (cost math unchanged)."""
    state = warm_state(config, prefix=50_000)  # system_tokens defaults to 0
    new_state, _ = apply_event(state, Turn(input_tokens=2_000, output_tokens=500), config)
    assert new_state.breakpoints == (new_state.cached_tokens,)
    assert new_state.cached_tokens == 52_000


def test_breakpoints_stay_sorted_and_capped(config):
    """With a protected prefix, a turn keeps an anchored + trailing breakpoint,
    sorted ascending and within max_breakpoints."""
    state = CacheState(
        model="sonnet-4.6",
        effort="high",
        ttl_seconds=config.ttl_seconds["5m"],
        last_used=0.0,
        now=0.0,
        prefix_tokens=50_000,
        cached_tokens=50_000,
        system_tokens=8_000,
    )
    new_state, _ = apply_event(state, Turn(input_tokens=2_000, output_tokens=0), config)
    assert new_state.breakpoints == (8_000, 52_000)
    assert list(new_state.breakpoints) == sorted(new_state.breakpoints)
    assert len(new_state.breakpoints) <= config.max_breakpoints


# -- Stage 3: rewind ---------------------------------------------------------

def test_rewind_re_hits_earlier_prefix(config):
    """Rewinding to an offset inside the cached prefix re-hits: the truncated point
    stays cached, so the next turn reads it cheaply (unlike a model switch)."""
    state = warm_state(config, prefix=50_000)
    state, _ = apply_event(state, Turn(input_tokens=2_000, output_tokens=500), config)
    state, _ = apply_event(state, Turn(input_tokens=1_000, output_tokens=300), config)
    assert state.cached_tokens > 30_000  # warm and well past the rewind target

    rewound, cost = apply_event(state, Rewind(to_tokens=30_000), config)
    assert cost.total_cost == 0.0
    assert rewound.prefix_tokens == 30_000
    assert rewound.cached_tokens == 30_000
    assert rewound.breakpoints[-1] == 30_000  # trailing breakpoint follows the rewind
    assert any("re-hit" in n for n in cost.notes)

    # The follow-up turn reads the rewound prefix rather than rewriting it.
    _, turn_cost = apply_event(rewound, Turn(input_tokens=1_000, output_tokens=0), config)
    assert turn_cost.read_tokens == 30_000


def test_rewind_past_ttl_is_cold(config):
    """If the session has idled past its TTL, the earlier entry is gone too, so the
    rewind leaves a cold cache for the next turn to rebuild."""
    state = warm_state(config, prefix=50_000, ttl_key="5m")  # 300s TTL
    state, _ = apply_event(state, Advance(seconds=600), config)  # 10 min idle

    rewound, cost = apply_event(state, Rewind(to_tokens=30_000), config)
    assert rewound.prefix_tokens == 30_000
    assert rewound.cached_tokens == 0
    assert rewound.breakpoints == ()
    assert any("cold" in n for n in cost.notes)


def test_rewind_to_at_or_past_current_prefix_is_noop(config):
    """A rewind that does not strictly shorten the sequence changes nothing."""
    state = warm_state(config, prefix=50_000)
    rewound, cost = apply_event(state, Rewind(to_tokens=50_000), config)
    assert rewound == state
    assert any("nothing to rewind" in n for n in cost.notes)


# -- Stage 3: compact --------------------------------------------------------

def layered_warm_state(config, system=8_000, prefix=50_000, ttl_key="5m"):
    """A warm session with a protected leading prefix of `system` tokens."""
    return CacheState(
        model="sonnet-4.6",
        effort="high",
        ttl_seconds=config.ttl_seconds[ttl_key],
        last_used=0.0,
        now=0.0,
        prefix_tokens=prefix,
        cached_tokens=prefix,
        system_tokens=system,
    )


def test_compact_reads_warm_prefix_then_re_anchors(config):
    """Compacting bills a cheap read of the whole warm prefix plus the summary output,
    then truncates to the protected prefix; the next turn re-hits that prefix instead
    of re-reading the 50K conversation."""
    state = layered_warm_state(config, system=8_000, prefix=50_000)
    compacted, cost = apply_event(state, Compact(summary_tokens=2_000), config)

    # Summary generation: whole warm prefix read cheaply, no write, summary as output.
    assert cost.read_tokens == 50_000
    assert cost.write_tokens == 0
    assert cost.output_tokens == 2_000
    assert any("compact" in n for n in cost.notes)

    # The conversation layer is gone; only protected prefix + summary remain.
    assert compacted.prefix_tokens == 8_000 + 2_000
    assert compacted.cached_tokens == 8_000  # summary is fresh output, cached next turn
    assert compacted.breakpoints[-1] == 8_000

    # The follow-up turn re-hits the protected prefix, not the old 50K conversation.
    _, turn_cost = apply_event(compacted, Turn(input_tokens=1_000, output_tokens=0), config)
    assert turn_cost.read_tokens == 8_000


def test_compact_when_cold_pays_full_rebuild(config):
    """If the session idled past its TTL, summarizing must re-process the whole prefix
    (a full write), but it still re-anchors to the protected prefix afterwards."""
    state = layered_warm_state(config, system=8_000, prefix=50_000, ttl_key="5m")
    state, _ = apply_event(state, Advance(seconds=600), config)  # 10 min idle

    compacted, cost = apply_event(state, Compact(summary_tokens=2_000), config)
    assert cost.read_tokens == 0
    assert cost.write_tokens == 50_000  # whole prefix rewritten to summarize it
    assert any("cold resume" in n for n in cost.notes)
    assert compacted.prefix_tokens == 8_000 + 2_000
    assert compacted.cached_tokens == 8_000


def test_compact_summary_not_smaller_is_noop(config):
    """A summary at least as large as the conversation layer gains nothing: no-op."""
    state = layered_warm_state(config, system=8_000, prefix=50_000)  # conversation 42K
    compacted, cost = apply_event(state, Compact(summary_tokens=42_000), config)
    assert compacted == state
    assert cost.total_cost == 0.0
    assert any("nothing to compact" in n for n in cost.notes)


# -- Stage 3: context-edit / tool-result clearing ----------------------------

def test_clear_tool_results_invalidates_downstream(config):
    """Clearing old tool results keeps only the protected prefix cached and shrinks the
    sequence; the next turn re-hits the protected prefix and rewrites the suffix."""
    state = layered_warm_state(config, system=8_000, prefix=50_000)
    cleared, cost = apply_event(state, ClearToolResults(freed_tokens=10_000), config)

    assert cost.total_cost == 0.0
    assert cleared.prefix_tokens == 40_000          # 50K - 10K freed
    assert cleared.cached_tokens == 8_000           # only the protected prefix survives
    assert cleared.breakpoints[-1] == 8_000
    assert any("cleared" in n for n in cost.notes)

    # The follow-up turn reads the protected prefix and rewrites the shifted suffix.
    _, turn_cost = apply_event(cleared, Turn(input_tokens=1_000, output_tokens=0), config)
    assert turn_cost.read_tokens == 8_000
    assert turn_cost.write_tokens == (40_000 - 8_000) + 1_000


def test_clear_below_clear_at_least_is_noop(config):
    """Freeing fewer tokens than the model's min_cacheable is not worth the rewrite."""
    state = layered_warm_state(config, system=8_000, prefix=50_000)  # min_cacheable 1024
    cleared, cost = apply_event(state, ClearToolResults(freed_tokens=500), config)
    assert cleared == state
    assert cost.total_cost == 0.0
    assert any("clear skipped" in n for n in cost.notes)


def test_clear_more_than_conversation_layer_is_noop(config):
    """Clearing the whole (or more than the) conversation layer changes nothing."""
    state = layered_warm_state(config, system=8_000, prefix=50_000)  # conversation 42K
    cleared, cost = apply_event(state, ClearToolResults(freed_tokens=42_000), config)
    assert cleared == state
    assert cost.total_cost == 0.0
    assert any("nothing cleared" in n for n in cost.notes)


# -- Stage 3: 20-block walk-back ---------------------------------------------

def test_walk_back_normal_turn_still_hits(config):
    """A turn that adds less than the walk-back window reads the whole cached prefix,
    exactly as Stage 1 (backward compatibility)."""
    assert config.walkback_window_tokens == 20_000
    state = warm_state(config, prefix=50_000)
    _, cost = apply_event(state, Turn(input_tokens=2_000, output_tokens=0), config)
    assert cost.read_tokens == 50_000


def test_walk_back_input_exactly_at_window_still_hits(config):
    """New content exactly equal to the window is still reachable (<= boundary)."""
    state = warm_state(config, prefix=50_000)
    _, cost = apply_event(
        state, Turn(input_tokens=config.walkback_window_tokens, output_tokens=0), config
    )
    assert cost.read_tokens == 50_000


def test_walk_back_huge_single_turn_jump_is_a_full_miss(config):
    """A single turn whose new content exceeds the walk-back window pushes the trailing
    breakpoint out of reach: the lookback misses and the whole prefix is rewritten."""
    state = warm_state(config, prefix=50_000)  # trailing breakpoint at 50K
    big = config.walkback_window_tokens + 5_000  # 25K > 20K window
    new_state, cost = apply_event(state, Turn(input_tokens=big, output_tokens=0), config)

    assert cost.read_tokens == 0
    assert cost.write_tokens == 50_000 + big       # whole prefix + new input rewritten
    assert cost.cache_hit_ratio == 0.0
    assert any("walk-back" in n for n in cost.notes)
    # The turn still establishes a fresh cache entry at the new end.
    assert new_state.cached_tokens == 50_000 + big


def test_walk_back_huge_jump_falls_back_to_anchored_breakpoint(config):
    """In a layered session the trailing breakpoint is missed on a huge jump, but the
    anchored protected-prefix breakpoint is a kept entry that still re-hits -- a partial
    hit, not a full miss."""
    state = layered_warm_state(config, system=8_000, prefix=50_000)
    # One normal turn establishes the anchored + trailing breakpoints.
    state, _ = apply_event(state, Turn(input_tokens=1_000, output_tokens=0), config)
    assert state.breakpoints == (8_000, 51_000)

    big = config.walkback_window_tokens + 5_000  # 25K > 20K window
    _, cost = apply_event(state, Turn(input_tokens=big, output_tokens=0), config)
    assert cost.read_tokens == 8_000              # anchored protected prefix still hits
    assert cost.write_tokens == (51_000 - 8_000) + big
    assert any("walk-back" in n for n in cost.notes)
