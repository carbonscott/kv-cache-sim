"""The core engine: a pure function from (state, event) to (new state, cost).

apply_event never mutates its input -- it returns a fresh CacheState via
dataclasses.replace. All printing lives in the CLI/runner layer, never here.
"""

from __future__ import annotations

from dataclasses import replace

from .config import Config, dollars
from .cost import CostBreakdown
from .events import (
    Advance,
    ClearToolResults,
    Compact,
    Event,
    Rewind,
    SwitchEffort,
    SwitchModel,
    Turn,
    Upgrade,
)
from .state import CacheState


def apply_event(
    state: CacheState, event: Event, config: Config
) -> tuple[CacheState, CostBreakdown]:
    """Apply one event, returning the new state and its cost breakdown."""
    if isinstance(event, Turn):
        return _apply_turn(state, event, config)
    if isinstance(event, Advance):
        return _apply_advance(state, event)
    if isinstance(event, SwitchModel):
        return _apply_switch_model(state, event)
    if isinstance(event, SwitchEffort):
        return _apply_switch_effort(state, event)
    if isinstance(event, Upgrade):
        return _apply_upgrade(state, event)
    if isinstance(event, Rewind):
        return _apply_rewind(state, event, config)
    if isinstance(event, Compact):
        return _apply_compact(state, event, config)
    if isinstance(event, ClearToolResults):
        return _apply_clear(state, event, config)
    raise TypeError(f"unknown event type: {type(event).__name__}")


def _apply_advance(state: CacheState, event: Advance) -> tuple[CacheState, CostBreakdown]:
    """Move the simulated clock forward. No cost; TTL is checked on the next Turn."""
    new_state = replace(state, now=state.now + event.seconds)
    return new_state, CostBreakdown.zero()


def _apply_switch_model(
    state: CacheState, event: SwitchModel
) -> tuple[CacheState, CostBreakdown]:
    """Switch model. Different model = different cache key = full rebuild, paid by
    the next Turn. TTL is auth-driven, not model-driven, so it is left unchanged."""
    if event.model == state.model:
        return state, CostBreakdown.zero(["model unchanged; cache kept"])
    new_state = replace(state, model=event.model, cached_tokens=0)
    note = f"switched model to {event.model}; cache invalidated (rebuild next turn)"
    return new_state, CostBreakdown.zero([note])


def _apply_switch_effort(
    state: CacheState, event: SwitchEffort
) -> tuple[CacheState, CostBreakdown]:
    """Switch effort. Like model, effort is part of the cache key, so a real change
    invalidates the whole cache. A no-op change to the current level keeps it."""
    if event.effort == state.effort:
        return state, CostBreakdown.zero(["effort unchanged; cache kept"])
    new_state = replace(state, effort=event.effort, cached_tokens=0)
    note = f"switched effort to {event.effort}; cache invalidated (rebuild next turn)"
    return new_state, CostBreakdown.zero([note])


def _apply_upgrade(
    state: CacheState, event: Upgrade
) -> tuple[CacheState, CostBreakdown]:
    """A Claude Code upgrade changes the system prompt, so the whole history now
    sits behind a *different* prefix: zero the cache regardless of TTL. Unlike the
    TTL cold resume (time-based) or SwitchModel (cache-key change), this is the
    worst case -- no hits at all even on an otherwise-warm session. The system_tokens
    boundary is left unchanged; the next Turn pays the full rebuild."""
    new_state = replace(state, cached_tokens=0, breakpoints=())
    note = "upgrade: system prompt changed; cache invalidated even within TTL"
    return new_state, CostBreakdown.zero([note])


def _apply_rewind(
    state: CacheState, event: Rewind, config: Config
) -> tuple[CacheState, CostBreakdown]:
    """A /rewind truncates the sequence back to an earlier prefix of to_tokens.

    Since the cache is a pure leading prefix, the earlier offset is itself still
    cached, so (within TTL) it re-hits: the next Turn reads the rewound prefix
    rather than rewriting it. last_used is left unchanged -- a rewind sends no
    request, so the TTL clock keeps running from the last real turn."""
    if event.to_tokens >= state.prefix_tokens:
        note = (
            f"rewind target {event.to_tokens} >= current prefix "
            f"{state.prefix_tokens}; nothing to rewind"
        )
        return state, CostBreakdown.zero([note])

    if state.is_expired:
        # The earlier entry has expired along with everything else: cold.
        new_cached = 0
        new_breakpoints: tuple[int, ...] = ()
        note = "cache cold (idle past TTL), full rebuild next turn"
    else:
        new_cached = min(state.cached_tokens, event.to_tokens)
        new_breakpoints = _maintain_breakpoints(
            min(state.system_tokens, new_cached), new_cached, config.max_breakpoints
        )
        note = f"re-hits cached prefix (read {new_cached} tok next turn)"

    new_state = replace(
        state,
        prefix_tokens=event.to_tokens,
        cached_tokens=new_cached,
        breakpoints=new_breakpoints,
    )
    return new_state, CostBreakdown.zero([note])


def _apply_compact(
    state: CacheState, event: Compact, config: Config
) -> tuple[CacheState, CostBreakdown]:
    """A /compact replaces the conversation layer with a short summary, keeping the
    protected leading prefix (system_tokens).

    Unlike a rewind, this is a real request: generating the summary reads the whole
    warm prefix cheaply and emits the summary as output. We bill that exactly like a
    zero-input turn that outputs summary_tokens (so read/write/TTL/sub-minimum math
    lives in one place, _apply_turn), then truncate the sequence: the protected
    prefix survives and re-hits, while the long conversation is gone. The summary is
    fresh output, so -- like any turn's output -- it is cached only on the next turn.
    last_used moves to now because a request was sent."""
    conversation_layer = state.prefix_tokens - state.system_tokens
    if event.summary_tokens >= conversation_layer:
        note = "summary not smaller than conversation layer; nothing to compact"
        return state, CostBreakdown.zero([note])

    # Cost of generating the summary: a zero-input turn that outputs the summary.
    turn_state, breakdown = _apply_turn(
        state, Turn(input_tokens=0, output_tokens=event.summary_tokens), config
    )

    # The surviving valid leading prefix of the new sequence: the protected prefix
    # (or 0 if the summarizing turn established no cache, e.g. sub-minimum).
    post_cached = min(turn_state.cached_tokens, state.system_tokens)
    new_breakpoints = _maintain_breakpoints(
        min(state.system_tokens, post_cached), post_cached, config.max_breakpoints
    )
    note = (
        f"compacted conversation to {event.summary_tokens} tok summary; protected "
        f"prefix ({state.system_tokens} tok) re-hits next turn"
    )
    breakdown = replace(breakdown, notes=breakdown.notes + [note])

    new_state = replace(
        state,
        prefix_tokens=state.system_tokens + event.summary_tokens,
        cached_tokens=post_cached,
        breakpoints=new_breakpoints,
        last_used=state.now,
    )
    return new_state, breakdown


def _apply_clear(
    state: CacheState, event: ClearToolResults, config: Config
) -> tuple[CacheState, CostBreakdown]:
    """A context-edit / tool-result clearing removes freed_tokens of old tool results
    from the start of the conversation layer (just after the protected prefix).

    Because the cache is a leading prefix, punching a hole in the middle invalidates
    everything downstream: only the protected prefix survives as a valid cached prefix
    and the sequence shrinks by freed_tokens. Like a rewind this sends no request, so
    the cost is zero and the next Turn rewrites the shifted suffix.

    Gated by clear_at_least (= the model's min_cacheable): clearing less than that is
    not worth the rewrite, so it is a no-op. Also a no-op if freed_tokens is not a
    positive amount strictly smaller than the current conversation layer."""
    clear_at_least = config.models[state.model].min_cacheable
    conversation_layer = state.prefix_tokens - state.system_tokens

    if event.freed_tokens <= 0 or event.freed_tokens >= conversation_layer:
        note = (
            f"clear of {event.freed_tokens} tok does not fit the conversation layer "
            f"({conversation_layer} tok); nothing cleared"
        )
        return state, CostBreakdown.zero([note])

    if event.freed_tokens < clear_at_least:
        note = (
            f"clear skipped: {event.freed_tokens} < clear_at_least {clear_at_least} "
            f"(not worth the rewrite)"
        )
        return state, CostBreakdown.zero([note])

    # Only the unchanged leading prefix (up to system_tokens) stays cached; the suffix
    # downstream of the hole is invalidated and gets rewritten on the next turn.
    new_cached = min(state.cached_tokens, state.system_tokens)
    new_breakpoints = _maintain_breakpoints(
        min(state.system_tokens, new_cached), new_cached, config.max_breakpoints
    )
    note = (
        f"cleared {event.freed_tokens} tok of tool results; suffix invalidated "
        f"(protected prefix {new_cached} tok re-hits, rewrite next turn)"
    )
    new_state = replace(
        state,
        prefix_tokens=state.prefix_tokens - event.freed_tokens,
        cached_tokens=new_cached,
        breakpoints=new_breakpoints,
    )
    return new_state, CostBreakdown.zero([note])


def _apply_turn(
    state: CacheState, event: Turn, config: Config
) -> tuple[CacheState, CostBreakdown]:
    """The heart of Stage 1: cost one request/response and grow the sequence.

    Bookkeeping (design section 4):
      read  = the valid cached prefix (0.1x)
      write = uncached existing tail + this turn's new input (1.25x / 2x)
      output billed at the model's output rate
    After the turn, the breakpoint sits at the end of this turn's input, so the new
    input becomes cached but this turn's *output* is only cached on the next turn.
    """
    pricing = config.models[state.model]
    notes: list[str] = []

    # 1. Expire the cache if the session has been idle past its TTL.
    cached_tokens = state.cached_tokens
    if state.is_expired:
        cached_tokens = 0
        notes.append("cold resume: idle past TTL, full rebuild")

    output_tokens = event.output_tokens
    prospective_prefix = state.prefix_tokens + event.input_tokens

    if prospective_prefix < pricing.min_cacheable:
        # 2a. Sub-minimum: the cacheable prefix is below the model's minimum, so no
        #     cache entry is established (no error -- it just is not cached). The whole
        #     input is billed at base 1.0x via the full_input bucket and both cache
        #     fields stay 0.
        read_tokens = 0
        write_tokens = 0
        full_input_tokens = prospective_prefix
        new_cached_tokens = 0
        new_breakpoints: tuple[int, ...] = ()
        notes.append(
            f"sub-minimum: prefix {prospective_prefix} < min_cacheable "
            f"{pricing.min_cacheable}; not cached (billed at 1.0x base)"
        )
    else:
        # 2b. Normal turn: split this request's input into read vs write buckets. The
        #     breakpoint sits at the end of this turn's input, so new cached_tokens
        #     covers everything up to there; the output just produced is not yet cached.
        read_tokens = _walk_back_read(
            state.breakpoints, cached_tokens, state.system_tokens,
            prospective_prefix, config.walkback_window_tokens,
        )
        write_tokens = prospective_prefix - read_tokens  # the un-read tail is rewritten
        full_input_tokens = 0  # CC's advancing breakpoint writes new content (no raw tail)
        if read_tokens < cached_tokens:
            notes.append(
                f"walk-back: new content sits {prospective_prefix - cached_tokens} tok "
                f"past the trailing breakpoint (> window {config.walkback_window_tokens}); "
                f"read falls back to {read_tokens} tok"
            )
        new_cached_tokens = prospective_prefix
        new_breakpoints = _maintain_breakpoints(
            state.system_tokens, new_cached_tokens, config.max_breakpoints
        )

    # 3. Price each bucket.
    write_multiplier = config.write_multiplier(state.ttl_seconds)
    read_cost = dollars(read_tokens, pricing.base_input * config.read_multiplier)
    write_cost = dollars(write_tokens, pricing.base_input * write_multiplier)
    full_input_cost = dollars(full_input_tokens, pricing.base_input)
    output_cost = dollars(output_tokens, pricing.output)
    total_cost = read_cost + write_cost + full_input_cost + output_cost

    # 4. Cache-hit ratio over input tokens (guard against an empty first turn).
    input_total = read_tokens + write_tokens + full_input_tokens
    cache_hit_ratio = read_tokens / input_total if input_total else 0.0

    breakdown = CostBreakdown(
        read_tokens=read_tokens,
        write_tokens=write_tokens,
        full_input_tokens=full_input_tokens,
        output_tokens=output_tokens,
        read_cost=read_cost,
        write_cost=write_cost,
        full_input_cost=full_input_cost,
        output_cost=output_cost,
        total_cost=total_cost,
        cache_hit_ratio=cache_hit_ratio,
        notes=notes,
    )

    # 5. Advance the sequence. cached_tokens / breakpoints were chosen above (step 2);
    #    the output just produced is not yet cached (it gets written on the next turn).
    new_prefix_tokens = state.prefix_tokens + event.input_tokens + event.output_tokens
    new_state = replace(
        state,
        prefix_tokens=new_prefix_tokens,
        cached_tokens=new_cached_tokens,
        breakpoints=new_breakpoints,
        last_used=state.now,
    )
    return new_state, breakdown


def _walk_back_read(
    breakpoints: tuple[int, ...],
    cached_tokens: int,
    system_tokens: int,
    prospective_prefix: int,
    window: int,
) -> int:
    """The cache hit length for a turn, modeling the 20-block lookback as a token
    distance (design section 6, option 1).

    The new request re-places its breakpoints; the hit length is the highest live
    breakpoint the request can still reach. A live breakpoint at offset b
    (0 < b <= cached_tokens) is reachable when either:

      - it is the anchored breakpoint at the protected-prefix boundary
        (b == system_tokens) -- a kept cache_control entry over system + tools that is
        hit directly while that prefix is unchanged and warm, independent of the window; or
      - the new content since it is within the lookback window
        (prospective_prefix - b <= window) -- the auto-advancing trailing breakpoint can
        still be walked back to.

    So an ordinary turn reaches the trailing breakpoint and reads the whole cached prefix
    (Stage 1 behavior). A large single-turn jump pushes the trailing breakpoint out of the
    window; the read then falls back to the anchored protected prefix (a partial hit) or,
    with no anchored breakpoint, to 0 (full miss). A cold cache (cached_tokens == 0) is
    always a full miss."""
    if cached_tokens <= 0:
        return 0
    reachable = [
        b
        for b in set(breakpoints) | {cached_tokens}
        if 0 < b <= cached_tokens
        and (b == system_tokens or prospective_prefix - b <= window)
    ]
    return max(reachable, default=0)


def _maintain_breakpoints(
    system_tokens: int, trailing: int, max_breakpoints: int
) -> tuple[int, ...]:
    """The breakpoints a warm turn leaves behind: the anchored one at the protected
    prefix boundary (if any) and the trailing one at the end of this turn's input,
    sorted ascending and capped at max_breakpoints (keeping the most recent offsets).

    cached_tokens stays equal to the trailing breakpoint, so the cost math is
    unchanged from Stage 1; the extra anchored breakpoint is carried for the
    follow-up walk-back / rewind events that will consume it."""
    offsets = sorted({offset for offset in (system_tokens, trailing) if offset > 0})
    if len(offsets) > max_breakpoints:
        offsets = offsets[-max_breakpoints:]
    return tuple(offsets)
