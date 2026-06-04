# kv-cache-sim

A simulator of a **Claude Code-like interactive session's prompt-cache state and cost**.
You feed in user prompts, fake tool calls, advance a simulated clock, and watch the per-turn
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
  cache breakpoints Claude Code sets (assumed 2) and the 20-block lookback (gated on
  content-block distance via `walkback_window_blocks`, default 20). These are config knobs,
  flagged in the design docs.
- **No real API calls happen.** Tool-call output sizes are specified by you; the simulator
  fakes them to accumulate context.

## How caching bills

Prompt caching is easiest to understand from the agent loop that drives a session:

```python
while True:
    resp = call_llm(messages)   # the one billed API request: cache write/read happens HERE
    messages.append(resp)       # local-only: this list IS the cached prefix, and it only grows
    tools = [b for b in resp if b.tool_use]
    if not tools:
        return                  # turn done
    out = [run_tool(t) for t in tools]
    messages.append(tool_result(out))   # local-only append (free) -- extends the prefix
```

Each `call_llm` is **one API request — the atomic billed unit**. It re-sends the *whole*
`messages` list, and caching is what makes the unchanged leading prefix a cheap *read*
instead of a full *write*. Everything between calls — appending the model's response, running
tools, appending their results — is a free local append that only grows the list, which is why
the cached prefix grows monotonically within a turn. The simulator's grammar mirrors this
exactly: `user` and `tool` are the free appends, and `call` is the one request that bills. One
turn can be several calls — in the ledger below, call 2 requests a tool and call 3 answers
after the result comes back, which is one pass through this loop.

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

# Tests (86 passing)
uv run pytest
```

## What it looks like

Running `examples/warm-then-cold.txt` — a session that warms up, goes idle past the TTL,
and rebuilds:

```
 #  event                           read   write    out  hit%    call $   total $
---------------------------------------------------------------------------------
 1  Call(in=50000, out=500)            0   50000    500    0%   0.32500   0.32500
 2  Call(in=1500, out=50)          50000    2000     50   96%   0.03875   0.36375
 3  Call(in=1200, out=600)         52000    1250    600   98%   0.04881   0.41256
 4  Call(in=1000, out=400)         53250    1600    400   97%   0.04662   0.45919
 5  Advance(600s)                      0       0      0    0%   0.00000   0.45919
 6  Call(in=1200, out=500)             0   56450    500    0%   0.36531   0.82450
      - cold resume: idle past TTL, full rebuild
 7  Call(in=800, out=400)          56450    1300    400   98%   0.04635   0.87085
 8  SwitchModel(sonnet-4.6)            0       0      0    0%   0.00000   0.87085
      - switched model to sonnet-4.6; cache invalidated (rebuild next turn)
 9  Call(in=1000, out=700)             0   59150    700    0%   0.23231   1.10316
```

Each API call is the atomic billed unit. Call 1 pays a full write to seed a 50K-token
prefix. Calls 3–4 mostly *read* the cached prefix (cheap — hit ratio climbs to 97%).
Call 2 is the small tool-requesting generation (`call 50 tu=1`); call 3 is the answer
after the tool result comes back. After a 10-minute idle the cache is cold, so call 6
rebuilds at a cost spike. The model switch on call 8 invalidates the cache again.

## REPL commands

The same engine grammar drives the REPL and the batch files (one command per line; `#` lines
are comments; blank lines are no-ops); only `save`/`load` are REPL-only, since file I/O lives
in the REPL front-end. The API **call** is the atomic billed unit — `user` and `tool` are free
local appends, and `call` is the one event that issues a request:

| Command | Effect |
|---|---|
| `user <n \| text>` | append user-input tokens to the pending call (free; +1 block) |
| `tool <name> <n>` | append a fake tool-result of `n` tokens (free; +1 block) |
| `call <out> [tu=N]` | issue one API request: bill the accumulated input, emit `<out>` output tokens in `max(1, N)` blocks, then reset the pending input |
| `advance <90s\|6m\|1h>` | jump the simulated clock |
| `ttl <5m \| 1h>` | switch the cache TTL (no invalidation; changes the write rate) |
| `system <n>` | set the protected prefix length (cold session only) |
| `rewind <to_tokens>` | truncate back to an earlier cached prefix (re-hits) |
| `compact <summary_tokens>` | replace the conversation layer with a summary (keeps system) |
| `clear-tools <n>` | clear old tool results (invalidates downstream) |
| `model <name>` / `effort <level>` | switch model/effort (invalidates the cache) |
| `upgrade` | simulate a CC upgrade (invalidates even within TTL) |
| `reset` | wipe everything back to a fresh cold session |
| `save <file>` / `load <file>` | write/replay the session's commands (REPL-only) |
| `status` / `help` / `quit` \| `exit` | session state / help / leave |

A bare integer is a token count; anything else is tokenized as text. The protected prefix set
by `system` is a floor: a `call` or `rewind` that would shrink the prefix below it is refused.

## Project layout

| Path | What's in it |
|---|---|
| `sim/` | the pure engine: `state.py`, `events.py`, `engine.py` (`apply_event`), `cost.py`, `config.py`, `tokenizer.py`. No I/O — returns structured results. |
| `cli/` | thin front-ends over the engine: `session.py` (shared command core), `repl.py`, `ledger.py` (one output format). |
| `scripts/` | entry points: `run_repl.py`, `run_batch.py`, `run_scripted.py`. |
| `config/models.json` | all model data — pricing, TTLs, minimums, breakpoint knobs. |
| `examples/` | self-documenting batch scripts that double as tests. |
| `tests/` | 86 tests (engine bookkeeping, session grammar, batch runs). |

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
