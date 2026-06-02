# Handoff: Stage 2 — REPL + batch runner (done)

> Follows `handoffs/claude-code-cache-simulator.md` (project overview) and
> `design/stage1-engine-api.md` (engine contract). **Stages 1 and 2 are built+verified.**
> Both front-ends (interactive REPL and batch runner) are done. **Stage 2 is complete →
> next is `handoffs/stage3-events.md`.** This doc records what exists, so the Stage 2
> boundary is honest.

## Goal (recap)

Two thin front-ends over the pure Stage 1 engine so a person can drive a simulated
session and watch cache cost live: an **interactive REPL** (built) and a **batch/scripted
runner** (built). Both are thin shells over the same `apply_event` core — the engine
stays pure and prints nothing; all I/O lives in `cli/`.

## What is built and verified

Decisions taken this stage (with the user): **accumulate-then-send** turn model, **REPL
first**, real `o200k_base` tokenizer wired and config-swappable.

- `sim/tokenizer.py` — `count_tokens(text, encoding="o200k_base")` via tiktoken, with a
  `chars/4` fallback if the encoding can't load offline; `is_approximate()` reports the
  fallback. Honest that it's an approximation, not Anthropic's true tokenizer.
- `cli/ledger.py` — pure string formatters (`HEADER`, `SEPARATOR`, `describe_event`,
  `row`, `note_line`, `status_line`). One ledger format across the whole project.
- `cli/session.py` — **the shared core both front-ends use.** `Session` holds engine
  state + running cost + the pending (uncommitted) turn. `handle(line) -> list[str]`
  parses one command and returns output lines (no I/O of its own). Commands: `paste`,
  `tool <name> <n>`, `gen`, `send` (blank line also commits), `advance <90s|6m|1h>`,
  `model`, `effort`, `status`, `help`, `quit`. `#`-prefixed lines are ignored (comments),
  so both front-ends get annotated/spaced scripts uniformly. `parse_duration` and
  `_tokens_or_text` (bare integer = token count, else tokenize) are here.
- `cli/repl.py` — thin read-eval-print loop over `Session`; `default_state()` starts cold
  on the first config model.
- `scripts/run_repl.py` — REPL entry point.
- `scripts/run_scripted.py` — **refactored** to use `cli/ledger.py` (no duplicated
  formatting; output byte-identical to before).
- `tests/test_session.py` — 16 tests (duration parsing, accumulate/send, blank-line
  commit, empty-send no-op, prose tokenization, cold-resume-through-REPL, model/effort/
  unknown-command/quit handling).

Full suite: **31/31 pass** (9 engine + 16 session + 6 batch).

## The batch runner (built)

`Session.handle()` already speaks the full command grammar, so the batch runner is small:
read a command file line by line, feed each to one `Session`, print the returned lines.

- `scripts/run_batch.py` — entry point taking a command-file path as `sys.argv[1]` (prints
  a usage line and exits non-zero if missing). Builds `Session(default_state(config),
  config)`, prints `ledger.HEADER` + `SEPARATOR`, then prints every line from
  `session.handle(...)` per file line, breaking when `session.done` (a `quit` in the file).
  Does **not** echo raw command lines — the ledger rows already label each turn. All
  printing lives here; the engine stays pure.
- **Input file format = the REPL command grammar, verbatim** — one grammar serves both
  front-ends, and example scripts double as tests. Blank line == `send` (commit). `#`
  lines are comments (a one-line addition to `Session.handle`, so the REPL gets them too);
  use `#` for narration/spacing since a blank line always commits.
- `examples/warm-then-cold.txt` — a self-documenting script: a couple of warm turns →
  `advance` past the TTL → a cold-resume turn → a `model` switch → a rebuilt turn. Parallels
  `run_scripted.py`'s story and doubles as the batch test input.
- `tests/test_batch.py` — 6 tests: comment lines ignored / don't commit, warm turn reads
  the cached prefix, post-`advance` turn emits a "cold resume" note, `quit` sets
  `session.done` (and stops the run), and the shipped example file runs end to end to a
  positive running total.

That completes Stage 2 → next is `handoffs/stage3-events.md`.

## Decisions carried in (do not re-litigate)

- Engine stays pure; front-ends do all I/O.
- Accumulate-then-send turn model; blank line == `send`.
- Real tokenizer, swappable, approximate (be honest in the UI).
- One ledger format (`cli/ledger.py`) shared everywhere.

## Open threads / flagged tweaks (mention, not silently change)

- **Default REPL model** is the first config entry (currently `opus-4.8`); `sonnet-4.6`
  may be a friendlier/cheaper default. One-line change in `cli/repl.py:default_state`.
- **Live session summary** — the REPL prints a per-turn row + `status` on demand; a
  persistent running-total line was discussed but not added. Optional.
- **Sub-minimum warning** — `min_cacheable` is in config but unused; the REPL could warn
  when a prefix is too short to cache. Full handling is Stage 3 (see that handoff).

## Environment (changed this stage)

- **uv-managed** now: `pyproject.toml` declares deps (`tiktoken`, dev `pytest`); `uv.lock`
  pins them; `.gitignore` covers `.venv/`, `__pycache__/`, `.pytest_cache/`.
- Run commands: `uv run pytest`, `uv run scripts/run_repl.py`,
  `uv run scripts/run_scripted.py`. (`uv sync` to set up from scratch.)

## Pointers

- `design/stage1-engine-api.md` — engine contract.
- `cli/session.py` — the grammar the batch runner reuses.
- `scripts/run_scripted.py` — the fold+format pattern to copy for the batch runner.
- `handoffs/stage3-events.md` — the next stage (long-tail cache events).
- `handoffs/claude-code-cache-simulator.md` — project overview + full decision history.
