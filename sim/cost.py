"""The per-event cost breakdown returned by the engine.

Splits an event's billing into the four buckets Anthropic charges separately:
cache read, cache write, post-breakpoint full input, and output generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CostBreakdown:
    """Token counts and dollar cost for a single event.

    cache_hit_ratio is read_tokens / (read + write + full_input) -- the
    "is caching working" signal from research/05 section 8. notes carries
    free-text flags (cold resume, model-switch rebuild) for the CLI to surface.
    """

    read_tokens: int = 0
    write_tokens: int = 0
    full_input_tokens: int = 0
    output_tokens: int = 0

    read_cost: float = 0.0
    write_cost: float = 0.0
    full_input_cost: float = 0.0
    output_cost: float = 0.0

    total_cost: float = 0.0
    cache_hit_ratio: float = 0.0
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def zero(notes: list[str] | None = None) -> "CostBreakdown":
        """A no-cost breakdown for events that don't bill anything (Advance,
        SwitchModel, SwitchEffort)."""
        return CostBreakdown(notes=notes or [])
