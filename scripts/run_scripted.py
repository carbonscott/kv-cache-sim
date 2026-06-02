"""Run a hardcoded event list through the engine and print a per-turn ledger.

A non-interactive scripted experiment so we can eyeball the cache read/write split
and the running cost. Uses the same ledger formatter as the REPL (cli/ledger.py);
all printing lives here, the engine itself is pure.

Run from the project root:  python scripts/run_scripted.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import ledger
from sim.config import load_config
from sim.engine import apply_event
from sim.events import Advance, SwitchEffort, SwitchModel, Turn
from sim.state import CacheState

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.json"
)


def build_initial_state(config) -> CacheState:
    """Start a warm sonnet-4.6 session with a ~50K-token prefix already cached."""
    return CacheState(
        model="sonnet-4.6",
        effort="high",
        ttl_seconds=config.ttl_seconds["5m"],
        last_used=0.0,
        now=0.0,
        prefix_tokens=50_000,
        cached_tokens=50_000,
    )


def build_events() -> list:
    """A small story: a few warm turns, an idle gap, a model switch, an effort switch."""
    return [
        Turn(input_tokens=2_000, output_tokens=500),    # warm turn
        Turn(input_tokens=1_500, output_tokens=800),    # warm turn, ratio climbs
        Advance(seconds=600),                            # idle 10 min (> 5 min TTL)
        Turn(input_tokens=1_000, output_tokens=400),    # cold resume: full rebuild
        Turn(input_tokens=1_200, output_tokens=600),    # warm again
        SwitchModel(model="opus-4.8"),                   # invalidates cache
        Turn(input_tokens=1_000, output_tokens=900),    # rebuild on opus
        SwitchEffort(effort="medium"),                   # invalidates cache
        Turn(input_tokens=800, output_tokens=700),      # rebuild on new effort
    ]


def main() -> None:
    config = load_config(CONFIG_PATH)
    state = build_initial_state(config)
    events = build_events()

    print(ledger.HEADER)
    print(ledger.SEPARATOR)

    running_total = 0.0
    for i, event in enumerate(events, start=1):
        state, cost = apply_event(state, event, config)
        running_total += cost.total_cost
        print(ledger.row(i, ledger.describe_event(event), cost, running_total))
        for note in cost.notes:
            print(ledger.note_line(note))


if __name__ == "__main__":
    main()
