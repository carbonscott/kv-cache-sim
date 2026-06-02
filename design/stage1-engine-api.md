# Stage 1 Engine API — Design for Approval

> Status: **proposed, not implemented.** This doc defines the Stage 1 engine API
> (`CacheState` + `apply_event`) for sign-off before any code is written. Scope is
> Stage 1 only per the handoff: a pure, importable engine + cost model + fake clock,
> driven by a scripted event list, **no UI**. Stages 2–3 are out of scope here but the
> API is shaped so they drop in without a rewrite.
>
> Sources: `research/00-summary.md`, `research/02`, `research/03`, `research/05`.
> Decisions carried in from `handoffs/claude-code-cache-simulator.md`.

## 1. What the engine models

A session is an ordered token sequence:

```
[tools] + [system + project context] + [growing message history]
```

The **cache** is a *prefix* of that sequence, tagged with `(model, effort)` and a TTL
timer. Each simulated turn pays:

```
turn_cost = read_tokens        × base × 0.1          # unchanged cached prefix
          + write_tokens        × base × (1.25 | 2.0) # newly-appended pre-breakpoint blocks
          + full_input_tokens   × base × 1.0          # tokens after the last breakpoint
          + output_tokens       × output_rate         # assistant generation
```

The engine is a **pure function over state** — no `print`/I/O inside it. It returns a
new state plus a structured cost breakdown; the CLI layer (Stage 2) does all printing.

## 2. `CacheState`

The full mutable state the engine threads through events. Proposed as a dataclass:

| Field | Type | Meaning |
|---|---|---|
| `model` | `str` | active model key, indexes the config pricing table (e.g. `"opus-4.8"`) |
| `effort` | `str` | active effort level (e.g. `"high"`); part of the cache key alongside `model` |
| `ttl_seconds` | `int` | cache lifetime from `last_used` (300 for 5m, 3600 for 1h) |
| `last_used` | `float` | simulated-clock time the cache was last read/written |
| `now` | `float` | current simulated-clock time (advanced by `advance` events) |
| `prefix_tokens` | `int` | total length of the full materialized sequence so far (tools+system+all history) |
| `cached_tokens` | `int` | how many *leading* tokens sit in a valid, non-expired entry for the current `(model, effort)`; `0` when cold |

**Core invariant:** a cache hit covers only the *longest shared leading prefix*.
`cached_tokens ≤ prefix_tokens` always. When `cached_tokens == 0` the next turn pays a
full write of the whole sequence (cold/rebuilt cache).

> Note on breakpoints: the 20-block lookback / multi-breakpoint walk-back
> (`research/05 §2`) is **not** modeled token-exactly in Stage 1. Stage 1 uses the
> single-advancing-breakpoint approximation (breakpoint sits at the end of each turn's
> input), which is sufficient for steady-state per-turn cost. The richer breakpoint
> model is a Stage 3 concern; `CacheState` has room to add a `breakpoints` field later
> without breaking the signature.

## 3. Events

Stage 1 is driven by a **scripted list of events**. Proposed as small tagged dataclasses
(a closed set the engine matches on):

| Event | Fields | Effect |
|---|---|---|
| `Turn` | `input_tokens: int`, `output_tokens: int` | one request/response: reads cached prefix, writes new blocks, generates output, grows the sequence |
| `Advance` | `seconds: int` | jump the simulated clock; no cost. TTL expiry is detected lazily on the next `Turn` |
| `SwitchModel` | `model: str` | set model; invalidate cache (`cached_tokens → 0`). Zero immediate cost; the next `Turn` pays the full rebuild |
| `SwitchEffort` | `effort: str` | set effort; invalidate cache. Same rebuild-on-next-turn cost shape as `SwitchModel` |

`Turn` is the only event that produces a non-zero cost. `input_tokens` is whatever the
turn appends to context (a pasted user prompt **and/or** a fake tool-result size such as
`tool read_file 1500`); the engine does not care which — both are just appended input
tokens. Stage 2's CLI is where `tool read_file 1500` gets parsed into a `Turn`.

> Out of scope for Stage 1 (Stage 3): `/compact`, `/rewind`, context-edit clearing,
> cold-resume-after-upgrade, sub-minimum no-cache, multi-breakpoint walk-back. The
> `Turn`/`SwitchModel`/`SwitchEffort` set is enough to reproduce the worked example in §6.

## 4. `apply_event`

```python
def apply_event(state: CacheState, event: Event, config: Config) -> tuple[CacheState, CostBreakdown]:
    ...
```

- **Pure**: returns a *new* `CacheState` (does not mutate the input) plus the
  `CostBreakdown` for this event. No printing, no global clock.
- TTL check happens **inside** `apply_event` at the start of a `Turn`: if
  `state.now - state.last_used > state.ttl_seconds`, set `cached_tokens = 0` before
  costing (a cold resume → full write).
- Non-`Turn` events return a `CostBreakdown` of all zeros.

### `Turn` bookkeeping (the heart of Stage 1)

```
# 1. expire if idle past TTL
if now - last_used > ttl_seconds: cached_tokens = 0

# 2. split this request's input into read vs write
read_tokens  = cached_tokens                                  # valid prefix, 0.1x
write_tokens = (prefix_tokens - cached_tokens) + input_tokens # uncached existing tail
                                                              #   (e.g. last turn's output)
                                                              #   + this turn's new input, 1.25x
full_input_tokens = 0                                          # single-breakpoint model

# 3. output billed at output rate
output_tokens = event.output_tokens

# 4. advance the sequence and re-cache up to the breakpoint
new prefix_tokens = prefix_tokens + input_tokens + output_tokens
new cached_tokens = prefix_tokens + input_tokens   # breakpoint at end of input;
                                                   # this turn's output not yet cached
new last_used     = now
```

The "this turn's output is cached only on the *next* turn" detail is what makes the
multi-turn numbers track Anthropic billing: turn N+1's `write_tokens` includes turn N's
generated output.

## 5. `CostBreakdown` and `Config`

**`CostBreakdown`** (returned per event, consumed by the CLI/report layer):

| Field | Meaning |
|---|---|
| `read_tokens`, `write_tokens`, `full_input_tokens`, `output_tokens` | token counts in each billing bucket |
| `read_cost`, `write_cost`, `full_input_cost`, `output_cost` | dollars per bucket |
| `total_cost` | sum |
| `cache_hit_ratio` | `read_tokens / (read + write + full_input)`; the "is caching working" signal from `research/05 §8` |
| `notes` | free-text flags (e.g. cold-resume, model-switch rebuild) for the CLI to surface |

**`Config`** — loaded from a file, holds *all* model data so numbers aren't baked into
code (`research/02 §4` is the source):

```jsonc
{
  "multipliers": { "read": 0.1, "write_5m": 1.25, "write_1h": 2.0 },
  "ttl_seconds": { "5m": 300, "1h": 3600 },
  "tokenizer":   "o200k_base",          // swappable knob; NOT Anthropic's true tokenizer
  "models": {
    "opus-4.8":   { "base_input": 5.0, "output": 25.0, "min_cacheable": 1024 },
    "sonnet-4.6": { "base_input": 3.0, "output": 15.0, "min_cacheable": 1024 },
    "haiku-4.5":  { "base_input": 1.0, "output":  5.0, "min_cacheable": 4096 }
    // ...rest of research/02 §4 table
  }
}
```

Dollars = `tokens / 1_000_000 × rate`. Write multiplier is chosen by the active TTL
(`write_5m` vs `write_1h`).

## 6. Validation target

Stage 1 is "done" when it reproduces the `research/05 §2` worked example: a Sonnet turn
with **50K cached + 2K new** input should land at roughly **~7× cheaper** than the
no-cache cost (no-cache 51K×$3/MTok ≈ $0.153; cached 50K read + 2K write ≈ $0.02). A
scripted event list plus an assertion on `total_cost` is the Stage 1 test.

## 7. Points that need your decision

These are the choices I'd like signed off before coding — flagged rather than assumed:

1. **Write-multiplier vs blog simplification.** The `research/05` blog billed the new 2K
   at *full base* ($3); faithful Anthropic billing writes pre-breakpoint blocks at
   *1.25×* ($3.75). I propose the engine uses the faithful **1.25× write** and treats the
   blog's number as an approximation. The validation assertion in §6 would use a
   tolerance band so both round to "~7× cheaper." — **OK to use 1.25× write?**
2. **Event representation.** Tagged dataclasses (`Turn`, `Advance`, …) as above, vs a
   single dict like `{"type": "turn", ...}`. I lean dataclasses for readability/typo
   safety. — **dataclasses OK?**
3. **Config format.** JSON (shown) vs TOML/YAML. JSON needs no dependency. — **JSON OK?**
4. **Single-breakpoint approximation for Stage 1**, deferring the 20-block walk-back to
   Stage 3 (§2 note). — **OK to defer?**
5. **Tokenizer in Stage 1?** The scripted runner can take raw token *counts* directly,
   so the real `o200k_base` tokenizer isn't strictly needed until Stage 2 parses pasted
   prose. I propose **deferring the tokenizer to Stage 2** and having Stage 1 events carry
   integer token counts. — **OK to defer the tokenizer?**

Nothing here is built yet. On your sign-off (and any changes to the §7 points) I'll
implement Stage 1 against this doc.
