"""The simulator's cache state.

A session is an ordered token sequence: [tools] + [system + project context] +
[growing message history]. The cache is a *prefix* of that sequence, tagged with
(model, effort) and a TTL timer. CacheState holds exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CacheState:
    """A snapshot of the session's cache.

    Invariant: cached_tokens <= prefix_tokens. A cache hit covers only the longest
    valid leading prefix; cached_tokens == 0 means the cache is cold and the next
    turn pays a full write of the whole sequence.

    Stage 3 adds two fields with defaults (see design/stage3-state-model.md):
    system_tokens marks the protected leading prefix (system + tool defs + project
    context), so the conversation layer is prefix_tokens - system_tokens; breakpoints
    holds the sorted token offsets that currently carry live cache entries. Both are
    invariants the engine maintains: breakpoints is sorted ascending and, when the
    cache is warm, its trailing entry equals cached_tokens (cold -> breakpoints == ()).

    prefix_blocks / cached_blocks run parallel to prefix_tokens / cached_tokens but count
    content blocks (the unit Anthropic's 20-block lookback walks back over) instead of
    tokens. They default to 0, so every path that does not carry blocks keeps its current
    token-only behavior; only turns that actually exceed the 20-block window can miss.
    For a fully-warm state cached_blocks == prefix_blocks.
    """

    model: str               # active model key, indexes Config.models
    effort: str              # active effort level; part of the cache key with model
    ttl_seconds: int         # cache lifetime measured from last_used
    last_used: float         # simulated-clock time the cache was last read/written
    now: float               # current simulated-clock time
    prefix_tokens: int       # total length of the materialized sequence so far
    cached_tokens: int       # leading tokens held in a valid, non-expired entry
    system_tokens: int = 0   # length of the protected leading prefix (system+tools+ctx)
    breakpoints: tuple[int, ...] = ()  # sorted offsets holding live cache entries
    prefix_blocks: int = 0   # running content-block count of the materialized sequence
    cached_blocks: int = 0   # block offset of the trailing cached breakpoint

    @property
    def is_expired(self) -> bool:
        """True when the session has been idle past its TTL: the cache entry has
        lapsed and the next request will cold-resume. Single source of truth for the
        idle-past-TTL test the engine applies at request time."""
        return self.now - self.last_used > self.ttl_seconds
