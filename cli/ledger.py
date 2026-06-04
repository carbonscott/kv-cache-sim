"""Ledger formatting: turn CostBreakdowns into human-readable lines.

Pure string builders -- no printing. Shared by the REPL, the scripted runner, and
(later) the batch runner so there is one ledger format across the project.
"""

from __future__ import annotations

from sim.cost import CostBreakdown
from sim.events import (
    Advance,
    Call,
    ClearToolResults,
    Compact,
    Rewind,
    SwitchEffort,
    SwitchModel,
    Upgrade,
)
from sim.state import CacheState

HEADER = (
    f"{'#':>2}  {'event':<28} {'read':>7} {'write':>7} {'out':>6} "
    f"{'hit%':>5} {'call $':>9} {'total $':>9}"
)
SEPARATOR = "-" * len(HEADER)


def describe_event(event) -> str:
    """A short label for an event, used in the ledger's `event` column."""
    name = type(event).__name__
    if isinstance(event, Call):
        return f"{name}(in={event.input_tokens}, out={event.output_tokens})"
    if isinstance(event, Advance):
        return f"{name}({event.seconds}s)"
    if isinstance(event, SwitchModel):
        return f"{name}({event.model})"
    if isinstance(event, SwitchEffort):
        return f"{name}({event.effort})"
    if isinstance(event, Upgrade):
        return "Upgrade"
    if isinstance(event, Rewind):
        return f"{name}(to={event.to_tokens})"
    if isinstance(event, Compact):
        return f"{name}(summary={event.summary_tokens})"
    if isinstance(event, ClearToolResults):
        return f"{name}(freed={event.freed_tokens})"
    return name


def row(index: int, label: str, cost: CostBreakdown, running_total: float) -> str:
    """One ledger row for an applied event."""
    return (
        f"{index:>2}  {label:<28} "
        f"{cost.read_tokens:>7} {cost.write_tokens:>7} {cost.output_tokens:>6} "
        f"{cost.cache_hit_ratio * 100:>4.0f}% "
        f"{cost.total_cost:>9.5f} {running_total:>9.5f}"
    )


def note_line(note: str) -> str:
    """An indented note under a ledger row (cold resume, cache invalidation, ...)."""
    return f"      - {note}"


def status_line(state: CacheState, running_total: float) -> str:
    """A one-line snapshot of the current session state."""
    # Report the *effective* cache: an idle-past-TTL session reads as cold, matching
    # what the next turn would actually do, rather than the stale snapshot in cached_tokens.
    cached = 0 if state.is_expired else state.cached_tokens
    # Show the protected prefix only when it is in play, so status output stays
    # byte-identical for every system=0 run (comparability with existing sweeps).
    system = f"sys={state.system_tokens} " if state.system_tokens > 0 else ""
    return (
        f"model={state.model} effort={state.effort} "
        f"ttl={state.ttl_seconds}s now={state.now:.0f}s "
        f"cached={cached}/{state.prefix_tokens} tok "
        f"{system}"
        f"total=${running_total:.5f}"
    )
