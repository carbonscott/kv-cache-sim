"""The interactive REPL: a thin read-eval-print loop over a Session.

All it does is read lines, hand them to the Session, and print what comes back.
The Session does the parsing and drives the pure engine.
"""

from __future__ import annotations

from sim.config import Config
from sim.state import CacheState

from . import ledger
from .session import Session


def default_state(config: Config) -> CacheState:
    """A fresh, cold session on the first model in the config at 5-minute TTL."""
    first_model = next(iter(config.models))
    return CacheState(
        model=first_model,
        effort="high",
        ttl_seconds=config.ttl_seconds["5m"],
        last_used=0.0,
        now=0.0,
        prefix_tokens=0,
        cached_tokens=0,
    )


def run(state: CacheState, config: Config) -> None:
    """Run the REPL until the user quits or sends EOF."""
    session = Session(state, config)

    print("cache-sim REPL -- type 'help' for commands, 'quit' to leave.")
    print(ledger.status_line(session.state, session.running_total))
    print(ledger.HEADER)
    print(ledger.SEPARATOR)

    while not session.done:
        try:
            line = input("> ")
        except EOFError:
            print()
            break
        for out in session.handle(line):
            print(out)
