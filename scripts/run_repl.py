"""Launch the interactive cache-sim REPL.

Run from the project root:  python scripts/run_repl.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli.repl import default_state, run
from sim.config import load_config

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "models.json"
)


def main() -> None:
    config = load_config(CONFIG_PATH)
    state = default_state(config)
    run(state, config)


if __name__ == "__main__":
    main()
