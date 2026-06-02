# kv-cache-sim

A simulator of a **Claude Code-like interactive session's prompt-cache state and cost**.
You paste prompts, fake tool calls, advance a simulated clock, and watch the per-turn
cache hit/miss split and dollar cost evolve — **without calling any real LLM API**.

The point is to make the *consequences* of cache lifecycle events tangible: a TTL expiry
invalidates the cache, switching model or effort level rebuilds it, resuming a cold
session pays a full cache write again, `/compact` rewrites history, and so on. It's a
teaching artifact for understanding how prefix caching actually bills.

## Honest caveats (read first)

This is an **educational approximation**, not a billing oracle:

- **The tokenizer is not Anthropic's.** It uses OpenAI's `o200k_base` (via `tiktoken`)
  as a stand-in, because Anthropic publishes no public tokenizer for the 4.x models.
  Counts are *realistic and consistent*, not true Anthropic token counts. The tokenizer
  is a config-swappable knob, and the tool is honest in the UI when it falls back to a
  `chars/4` estimate offline.
- **The numbers are a June-2026 snapshot.** Pricing, TTLs, and per-model minimum-cacheable
  thresholds live in [`config/models.json`](config/models.json) precisely because Anthropic
  changes them. Re-verify before trusting any constant long-term.
- **Some behavior is a modeled assumption, not documented fact** — e.g. exactly how many
  cache breakpoints Claude Code sets (assumed 2) and the 20-block lookback (modeled as a
  token-distance window). These are config knobs, flagged in the design docs.
- **No real API calls happen.** Tool-call output sizes are specified by you; the simulator
  fakes them to accumulate context.

## Quickstart

The project is [uv](https://docs.astral.sh/uv/)-managed. No install step is needed — `uv run`
syncs the environment from the lockfile and runs:

```bash
# Batch runner: feed a command file through one session, print the ledger
uv run scripts/run_batch.py examples/warm-then-cold.txt
uv run scripts/run_batch.py examples/upgrade-cold-resume.txt

# Scripted runner: a hardcoded event list (warm → idle → model/effort switch)
uv run scripts/run_scripted.py

# Interactive REPL: drive a session live
uv run scripts/run_repl.py

# Tests (58 passing)
uv run pytest
```

## What it looks like

Running `examples/warm-then-cold.txt` — a session that warms up, goes idle past the TTL,
and rebuilds:

```
 #  event                           read   write    out  hit%    turn $   total $
---------------------------------------------------------------------------------
 1  Turn(in=50000, out=500)            0   50000    500    0%   0.32500   0.32500
 2  Turn(in=2700, out=600)         50000    3200    600   94%   0.06000   0.38500
 3  Turn(in=1000, out=400)         53200    1600    400   97%   0.04660   0.43160
 4  Advance(600s)                      0       0      0    0%   0.00000   0.43160
 5  Turn(in=1200, out=500)             0   56400    500    0%   0.36500   0.79660
      - cold resume: idle past TTL, full rebuild
 6  Turn(in=800, out=400)          56400    1300    400   98%   0.04632   0.84292
 7  SwitchModel(sonnet-4.6)            0       0      0    0%   0.00000   0.84292
      - switched model to sonnet-4.6; cache invalidated (rebuild next turn)
 8  Turn(in=1000, out=700)             0   59100    700    0%   0.23212   1.07505
```

Turn 1 pays a full write to seed a 50K-token prefix. Turns 2–3 mostly *read* the cached
prefix (cheap — hit ratio climbs to 97%). After a 10-minute idle the cache is cold, so
turn 5 rebuilds at a cost spike. The model switch on turn 7 invalidates the cache again.

## REPL commands

The same grammar drives the REPL and the batch files (one command per line; blank line
commits the pending turn; `#` lines are comments):

| Command | Effect |
|---|---|
| `paste <n \| text>` | add user-input tokens to the pending turn |
| `tool <name> <n>` | add a fake tool-result of `n` tokens |
| `gen <n \| text>` | add assistant-output tokens |
| `send` (or blank line) | commit the pending turn |
| `advance <90s\|6m\|1h>` | jump the simulated clock |
| `model <name>` / `effort <level>` | switch model/effort (invalidates the cache) |
| `compact <summary_tokens>` | replace the conversation layer with a summary |
| `rewind <to_tokens>` | truncate back to an earlier cached prefix (re-hits) |
| `clear-tools <n>` | clear old tool results (invalidates downstream) |
| `upgrade` | simulate a CC upgrade (invalidates even within TTL) |
| `status` / `help` / `quit` | session state / help / leave |

A bare integer is a token count; anything else is tokenized as text.

## Project layout

| Path | What's in it |
|---|---|
| `sim/` | the pure engine: `state.py`, `events.py`, `engine.py` (`apply_event`), `cost.py`, `config.py`, `tokenizer.py`. No I/O — returns structured results. |
| `cli/` | thin front-ends over the engine: `session.py` (shared command core), `repl.py`, `ledger.py` (one output format). |
| `scripts/` | entry points: `run_repl.py`, `run_batch.py`, `run_scripted.py`. |
| `config/models.json` | all model data — pricing, TTLs, minimums, breakpoint knobs. |
| `examples/` | self-documenting batch scripts that double as tests. |
| `tests/` | 58 tests (engine bookkeeping, session grammar, batch runs). |

## How it's built

The engine is a **pure function over state** — `apply_event(state, event, config) ->
(new_state, cost_breakdown)` — with no printing inside it; the CLI layer does all I/O. That
keeps the cost math testable and lets a future TUI/web view drop in.

It was built in three stages, each documented:

- **Stage 1** — the engine + cost model + fake clock (append turn, TTL expiry, model/effort
  switch). See [`design/stage1-engine-api.md`](design/stage1-engine-api.md).
- **Stage 2** — the REPL and batch runner as thin shells over the engine. See
  [`handoffs/stage2-repl-batch.md`](handoffs/stage2-repl-batch.md).
- **Stage 3** — the rich lifecycle events: `/compact`, `/rewind`, tool-result clearing,
  upgrade cold-resume, sub-minimum no-cache, and the multi-breakpoint walk-back. See
  [`design/stage3-state-model.md`](design/stage3-state-model.md) and
  [`handoffs/stage3-events.md`](handoffs/stage3-events.md).

## Going deeper

- **`research/`** — six sourced write-ups on the *why*: KV-cache theory, Anthropic's prompt
  caching numbers, invalidation scenarios, Claude Code's caching model, and a provider
  comparison. Start with [`research/00-summary.md`](research/00-summary.md).
- **`design/`** — the API and state-model design docs (architecture rationale).
- **`handoffs/`** — the development handoff notes. These were written *for an AI coding
  assistant* to carry the project across sessions, and are kept verbatim for transparency
  about how the project was built; the project overview is
  [`handoffs/claude-code-cache-simulator.md`](handoffs/claude-code-cache-simulator.md).
