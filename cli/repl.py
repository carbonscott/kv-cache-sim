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


def write_history(path: str, history: list[str]) -> None:
    """Write the session's commands one per line to `path`, in the same grammar the
    batch runner reads. The leading comment is ignored by handle() on replay."""
    with open(path, "w") as f:
        f.write("# cache-sim session\n")
        for command in history:
            f.write(command + "\n")


def read_history(path: str) -> list[str]:
    """Read a saved command file back into a list of lines (newlines stripped)."""
    with open(path) as f:
        return [line.rstrip("\n") for line in f]


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

    def do_save(parts):
        """save [<file>]: write the session's commands to a replayable file."""
        if len(parts) < 2:
            print("usage: save <file>")
            return
        path = parts[1]
        try:
            write_history(path, session.history)
        except OSError as e:
            print(f"could not save to {path}: {e}")
            return
        print(f"saved {len(session.history)} commands to {path}")

    def do_load(parts):
        """load [<file>]: replace the session by replaying a saved file (confirmed)."""
        if len(parts) < 2:
            print("usage: load <file>")
            return
        path = parts[1]
        try:
            lines = read_history(path)
        except OSError as e:
            print(f"could not load {path}: {e}")
            return
        try:
            answer = input("load will discard the current session. proceed? [y/N] ")
        except EOFError:
            print("load cancelled.")
            return
        if answer.strip().lower() not in ("y", "yes"):
            print("load cancelled.")
            return
        session.load_commands(lines)  # discard replay rows for a quiet load
        print_intro()
        print(f"loaded {len(session.history)} commands from {path}")

    print_intro()

    while not session.done:
        try:
            line = input("> ")
        except EOFError:
            print()
            break
        # save/load are REPL-only (they do file I/O and an interactive prompt); the
        # Session stays I/O-free. Intercept them before delegating to handle().
        parts = line.split()
        verb = parts[0].lower() if parts else ""
        if verb == "save":
            do_save(parts)
            continue
        if verb == "load":
            do_load(parts)
            continue
        for out in session.handle(line):
            print(out)
        if session.just_reset:
            print_intro()
            session.just_reset = False
