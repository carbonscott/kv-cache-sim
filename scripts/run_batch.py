"""Run a command file through a single Session and print the ledger.

A non-interactive front-end over the same Session the REPL uses: read a file of
commands (the REPL command grammar, with `#` comments), feed each line to one
Session, and print what comes back. Reproducible experiments and an easy
end-to-end test harness. All printing lives here; the engine stays pure.

Run from the project root:  python scripts/run_batch.py examples/warm-then-cold.txt
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import ledger
from cli.repl import default_state
from cli.session import Session
from sim.config import load_config

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.json"
)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: run_batch.py <commands.txt>")
        sys.exit(1)

    path = sys.argv[1]
    config = load_config(CONFIG_PATH)
    session = Session(default_state(config), config)

    print(ledger.HEADER)
    print(ledger.SEPARATOR)

    with open(path) as f:
        for line in f:
            for out in session.handle(line.rstrip("\n")):
                print(out)
            if session.done:
                break


if __name__ == "__main__":
    main()
