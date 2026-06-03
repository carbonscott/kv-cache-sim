# Stage 3 State-Model Extension — Design for Approval

> Status: **proposed.** This doc defines the `CacheState` extension Stage 3 needs
> *before* coding the rich cache-lifecycle events, mirroring how Stage 1 began with
> `design/stage1-engine-api.md`. Scope of the *code* landing alongside this doc is
> deliberately small: the data-model fields, the `max_breakpoints` knob, and the two
> events that need little new logic — **sub-minimum no-cache** and **upgrade
> cold-resume**. `/compact`, `/rewind`, context-edit clearing, and the full 20-block
> walk-back are mapped here but built later, each gated separately.
>
> Sources: `handoffs/stage3-events.md`, `research/02`, `research/03`, `research/05`.
> Extends the engine contract in `design/stage1-engine-api.md` (which reserved room
> for a `breakpoints` field for exactly this).

## 1. Motivation — why the flat state is insufficient

Stage 1 models the session as a **flat** pair `(prefix_tokens, cached_tokens)` with a
single advancing breakpoint: the cache is the longest valid leading prefix, and each
turn pushes the breakpoint to the end of that turn's input. This is enough for
steady-state append/expire/switch, and Stages 1–2 verify it.

Most of the remaining Stage 3 events cannot be expressed in that flat state:

- **`/compact`** must keep the protected leading prefix (system + tool defs + project
  context) and replace only the *conversation layer* with a summary. The flat state
  has no notion of *where* that protected boundary sits.
- **`/rewind`** jumps back to an *earlier* cached prefix. The flat state keeps only the
  current breakpoint, so there is nothing to rewind to.
- **Context-edit / tool-result clearing** removes tokens from the *middle* of the
  prefix and invalidates everything downstream — again needs an offset, not just a
  single advancing length.
- **The 20-block walk-back** needs up to two breakpoints (an anchored one and a
  trailing one) so a large single-turn jump can still partially hit.

The handoff's recommended first move is to extend `CacheState` once, get sign-off, then
implement the events one at a time against the stable state. That is what this doc does.

## 2. The layered session model

The session token sequence has always been three layers:

```
[ tools + system + project context ] [ conversation history ] [ this turn ]
 \________ protected prefix ________/ \____ conversation layer ____/
 0                       system_tokens                    prefix_tokens
```

Stage 1 tracked only the right edge (`prefix_tokens`) and the cache's right edge
(`cached_tokens`). Stage 3 adds the **left-of-conversation boundary** (`system_tokens`)
and the **set of live breakpoints** so the events above have the structure they need.
The conversation layer is simply `prefix_tokens - system_tokens`.

## 3. The two new fields

Both are appended to `CacheState` (`sim/state.py`) **with defaults**, so every existing
constructor — `cli/repl.py:default_state`, the test fixtures, `scripts/run_scripted.py`
— keeps working unchanged and all Stage 1/2 cost math is untouched.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `system_tokens` | `int` | `0` | length of the protected leading prefix (system + tool defs + project context). Defines the conversation layer as `prefix_tokens - system_tokens`. |
| `breakpoints` | `tuple[int, ...]` | `()` | sorted token offsets that currently hold live cache entries — typically the anchored one near `system_tokens` and the trailing one from the last turn. |

**`system_tokens`** is consumed later by `/compact` (keep the protected prefix, replace
the conversation layer) and serves as the anchored breakpoint position. It is
invalidated only by an `Upgrade` (the system prompt itself changed) and by a
model/effort switch (different cache key).

**`breakpoints`** generalizes the single advancing breakpoint. It is consumed later by
the ≤2-breakpoint 20-block walk-back and by `/rewind`. `cached_tokens` stays the
engine's *effective hit length* and is kept consistent with the trailing breakpoint, so
Stage 1's cost math is unchanged.

**Invariants:**

- `breakpoints` is sorted ascending and holds at most `max_breakpoints` entries.
- When the cache is warm, `cached_tokens == breakpoints[-1]` (the trailing breakpoint is
  the effective hit length).
- When the cache is cold (`cached_tokens == 0`) `breakpoints == ()`.

## 4. The `max_breakpoints` knob

One config knob is added (`sim/config.py` + `config/models.json` + loader):

- `max_breakpoints: int = 2` — how many breakpoints Claude Code is assumed to keep
  (one anchored after system+tools, one trailing the history).

The exact number CC sets is **not officially documented**; the handoff's working
assumption is 2. Per the handoff, this is made a knob now and revisited only if fidelity
needs it — code reads the knob rather than hard-coding `2`.

## 5. Per-event mapping

All rows are now built. The state shape defined here proved sufficient for every event,
as intended: each was implemented one at a time against the stable model, with no further
state changes.

| Event | State transition | Status |
|---|---|---|
| Sub-minimum no-cache | When the prefix that would be cached (`prefix_tokens + input_tokens`) `< min_cacheable`, establish **no** entry: the whole amount is billed at base 1.0×, `read = write = 0`, `cached_tokens = 0`, `breakpoints = ()`. | **built** |
| Upgrade cold-resume | Zero the cache even within TTL (new system prompt ⇒ a *different* prefix ⇒ no hits). `system_tokens` boundary unchanged; zero immediate cost (the next Turn pays the full rebuild). | **built** |
| `/compact` | Keep `system_tokens`; shrink the conversation layer to a summary. **Costed request** (Q2): the summary generation bills a cheap read of the warm prefix plus the summary as output, then re-anchors to the protected prefix. | **built** |
| `/rewind` | Jump `prefix_tokens` / `cached_tokens` back to an earlier breakpoint, re-hitting it. Zero immediate cost (re-hits within TTL, matching the `/rewind` precedent). | **built** |
| Context-edit / clearing | Invalidate downstream of a mid-prefix offset (only the protected prefix survives); gated by `clear_at_least` = the model's `min_cacheable`. Zero immediate cost; the next Turn rewrites the suffix. | **built** |
| 20-block walk-back | Hit length = the highest reachable live breakpoint: the trailing one if the new content is within the block-distance window (`prospective_blocks - cached_blocks <= walkback_window_blocks`), else the anchored protected prefix (partial hit), else 0 (full miss). | **built** |

## 6. Walk-back fidelity — recommendation

When the walk-back event is built, the 20-block window can be modeled two ways:

1. **Token-distance approximation (initially recommended).** Treat the window as a token
   distance (≈ the size of 20 cache blocks) and check whether a miss falls within that
   distance of a live breakpoint. Stays consistent with Stage 1's token-based spirit and the
   "keep it simple" guideline — no new block bookkeeping in the state.
2. **Discrete-block tracking (built).** Track cache blocks explicitly and walk back
   block-by-block. Higher fidelity to `research/02 §1`; it adds a block layer to the state,
   but that layer is what makes the cliff provably block-driven rather than a token proxy.

**Finalized: option 2 (discrete-block tracking) is built**, superseding the original
token-distance recommendation. The state carries `prefix_blocks`/`cached_blocks` alongside the
token counts (see `state.py`), and the window is a `walkback_window_blocks` config knob
(default 20; tunable in `config/models.json`). The hit length is the highest live breakpoint
the new request can reach, where the two breakpoints reach differently:

- the **trailing** breakpoint (at `cached_tokens`) is reached only if the new content
  since it is within the window (`prospective_blocks - cached_blocks ≤ walkback_window_blocks`)
  — the auto-advancing breakpoint walked back to;
- the **anchored** breakpoint (at `system_tokens`) is a *kept* `cache_control` entry over
  system + tools, hit directly whenever that prefix is unchanged and warm — **independent
  of the window**.

So an ordinary turn reaches the trailing breakpoint and reads the whole cached prefix
(Stage 1 behavior). A large single-turn jump pushes the trailing breakpoint out of the
window; the read then falls back to the anchored protected prefix — a *partial* hit — or,
in a flat session with no anchored breakpoint, to 0 (a full miss, matching the
single-breakpoint agent-loop example in `research/05 §2`).

## 7. Open threads

Carried from `handoffs/stage3-events.md`, to settle as they bite:

- **Exact breakpoint count/positions CC sets** — undocumented; working assumption 2,
  now expressed as the `max_breakpoints` knob. Revisit only if fidelity demands it.
- **`clear_at_least` default** — *resolved*: set to the model's `min_cacheable`. Clearing
  fewer tokens than the cache minimum can never re-establish a cacheable suffix, so it is
  never worth the rewrite; the clearing event no-ops below that threshold.
- **CC tiered-compaction thresholds** — only needed if *automatic* compaction triggers
  are modeled; manual `/compact` does not need them.

## 8. What lands now

1. `system_tokens` and `breakpoints` on `CacheState` (defaults preserve every caller).
2. `max_breakpoints` on `Config` + `config/models.json` + the loader.
3. `Upgrade` event (frozen dataclass, no fields) wired through the engine, session, and
   ledger.
4. The **sub-minimum rule** and breakpoint maintenance in `_apply_turn`, and
   `_apply_upgrade` (zero the cache regardless of TTL).

Everything in §5 marked "follow-up" is out of scope until separately gated, by design:
the state shape and the knob land now so each complex event can be built one at a time
without further state changes.
