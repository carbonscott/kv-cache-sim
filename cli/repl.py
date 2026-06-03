"""The interactive REPL: a thin read-eval-print loop over a Session.

All it does is read lines, hand them to the Session, and print what comes back.
The Session does the parsing and drives the pure engine.
"""

from __future__ import annotations

try:
    import readline  # enables arrow/Ctrl-P history and Tab completion for input()
except ImportError:
    readline = None  # not in the Windows stdlib; the REPL still works without it

from sim.config import Config
from sim.state import CacheState

from . import ledger
from .session import COMMAND_NAMES, Session, default_state  # re-exported for callers


def command_completions(text: str, line: str) -> list[str]:
    """Command-name matches for `text`, but only while still typing the first
    word of `line`. Returns [] once an argument is being typed."""
    if " " in line.lstrip():
        return []  # past the command word: don't complete arguments
    return [c for c in COMMAND_NAMES if c.startswith(text)]


def _make_completer():
    """Build a readline completer closure following its (text, state) protocol."""
    def completer(text, state):
        line = readline.get_line_buffer()[: readline.get_endidx()]
        matches = command_completions(text, line)
        return matches[state] if state < len(matches) else None
    return completer


def run(state: CacheState, config: Config) -> None:
    """Run the REPL until the user quits or sends EOF."""
    session = Session(state, config)

    if readline is not None:
        # readline's default delimiters include '-', which would break
        # "clear-tools"; restrict them to whitespace so commands stay whole.
        readline.set_completer_delims(" \t\n")
        readline.set_completer(_make_completer())
        readline.parse_and_bind("tab: complete")

    def print_intro():
        """The opening banner + ledger header; shown at startup and after a reset."""
        print("cache-sim REPL -- type 'help' for commands, 'quit' to leave.")
        print(ledger.status_line(session.state, session.running_total))
        print(ledger.HEADER)
        print(ledger.SEPARATOR)

    print_intro()

    while not session.done:
        try:
            line = input("> ")
        except EOFError:
            print()
            break
        for out in session.handle(line):
            print(out)
        if session.just_reset:
            print_intro()
            session.just_reset = False
