"""The events that drive the engine.

Stage 1 has a small closed set. Only Turn produces a non-zero cost; the others
change the cache key or the clock and let the next Turn pay any rebuild.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Turn:
    """One request/response. input_tokens is everything appended to context this
    turn (a user prompt and/or a fake tool-result size); output_tokens is
    the assistant's generation.

    input_blocks / output_blocks count the content blocks this turn carries, parallel
    to the token fields (a user message is 1 block, a tool round-trip is 2). They
    default to 0 so a turn that does not track blocks keeps its token-only behavior;
    the engine gates the 20-block walk-back on input_blocks."""

    input_tokens: int
    output_tokens: int
    input_blocks: int = 0
    output_blocks: int = 0


@dataclass(frozen=True)
class Advance:
    """Jump the simulated clock forward. No cost; TTL expiry is detected on the
    next Turn."""

    seconds: int


@dataclass(frozen=True)
class SwitchModel:
    """Change the active model. Invalidates the cache (different cache key)."""

    model: str


@dataclass(frozen=True)
class SwitchEffort:
    """Change the active effort level. Invalidates the cache unless it is a no-op
    change to the level already in effect."""

    effort: str


@dataclass(frozen=True)
class Upgrade:
    """A Claude Code upgrade that changes the system prompt. The whole history now
    sits behind a *different* prefix, so the cache is invalidated even within TTL --
    the worst-case cold resume. No fields; like SwitchModel it has zero immediate
    cost and lets the next Turn pay the full rebuild."""


@dataclass(frozen=True)
class Rewind:
    """A /rewind: truncate the conversation back to an earlier prefix length of
    to_tokens. Because the cache is a pure leading prefix, that earlier offset is
    itself still cached, so it re-hits within TTL -- zero immediate cost and the
    next Turn reads the rewound prefix rather than rewriting it. A no-op if
    to_tokens is not strictly shorter than the current sequence."""

    to_tokens: int


@dataclass(frozen=True)
class Compact:
    """A /compact: replace the conversation layer with a short summary of
    summary_tokens, keeping the protected leading prefix (system_tokens). Unlike
    Rewind, this is a *costed* request: generating the summary reads the whole warm
    prefix cheaply (0.1x) and emits the summary as output. Afterwards the protected
    prefix re-hits and the long conversation read is gone. A no-op if summary_tokens
    is not strictly smaller than the current conversation layer."""

    summary_tokens: int


@dataclass(frozen=True)
class ClearToolResults:
    """Context-edit / tool-result clearing: drop freed_tokens of old tool results
    from the start of the conversation layer (just after the protected prefix).
    Removing content from the middle of the prefix invalidates everything downstream,
    so only the protected prefix stays cached; like Rewind this has zero immediate
    cost and the next Turn rewrites the shifted suffix. Gated by clear_at_least
    (= the model's min_cacheable): a no-op if too little would be freed to be worth
    the rewrite, or if freed_tokens is not a positive amount smaller than the current
    conversation layer."""

    freed_tokens: int


Event = Union[
    Turn, Advance, SwitchModel, SwitchEffort, Upgrade, Rewind, Compact, ClearToolResults
]
