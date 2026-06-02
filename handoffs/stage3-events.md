# Handoff: Stage 3 — long-tail cache events

> Follows the project overview (`handoffs/claude-code-cache-simulator.md`), the engine
> contract (`design/stage1-engine-api.md`), and Stage 2 (`handoffs/stage2-repl-batch.md`).
> **Stages 1–2 are built and verified** (engine + cost model + clock; REPL front-end;
> batch runner is the last Stage-2 piece). Stage 3 adds the *rich* cache lifecycle events
> that Stages 1–2 deliberately deferred.

## Goal

Model the events that make cache cost interesting beyond steady-state append/expire:
`/compact`, `/rewind`, context-edit / tool-result clearing, cold-resume-after-upgrade,
sub-minimum no-cache, and the multi-breakpoint 20-block walk-back. Success = each event's
hit/miss split and dollar cost tracks the rules in `research/03` + `research/05`.

## What is ALREADY covered (do not re-implement)

These were sometimes loosely filed under "Stage 3" but are done in Stage 1's engine:

- **Append turn / tool result** — `Turn`, with read/write split.
- **Idle past TTL → cold resume** — `_apply_turn` zeroes `cached_tokens` when
  `now - last_used > ttl_seconds`. (This is the *same-content* cold resume; the
  *upgrade* cold resume below is different.)
- **Switch model / switch effort** — `SwitchModel` / `SwitchEffort` invalidate the cache;
  no-op same-value switch keeps it.

## The crux: a data-model extension comes first

Stage 1's `CacheState` models the sequence as a **flat** `(prefix_tokens, cached_tokens)`
pair with a single advancing breakpoint. Most Stage 3 events need more structure:

- `/compact` must keep **system+tools+project context** and replace only the
  **conversation layer** → the state needs to know the *layer boundary* (how many leading
  tokens are protected).
- `/rewind` must jump back to an **earlier cached prefix** → the state needs the history of
  prior prefix lengths (or breakpoint offsets) to rewind to.
- Context-edit / tool-result clearing removes tokens from the **middle** of the prefix →
  needs an offset/length to invalidate downstream from.
- The 20-block walk-back needs **block-level** structure and up to 2 breakpoints, replacing
  the single-breakpoint approximation.

**Recommended first task (mirrors how Stage 1 began): write a short design doc** —
`design/stage3-state-model.md` — proposing the `CacheState` extension before coding the
events. Likely shape: add a `system_tokens` (protected-prefix length) and a `breakpoints`
list / block model, keeping `apply_event`'s signature unchanged. Get sign-off, then
implement events one at a time (the user gates scope; design-before-big-change is the
established rhythm). The design doc anticipated a `breakpoints` field for exactly this.

## The events to add (rule → source)

| Event | Rule | Source |
|---|---|---|
| **`/compact`** | Replace conversation layer with a short summary; system+tools+project context survive (re-hit if unchanged). Summary *generation* reads the existing prefix (cheap); the post-compaction turn writes only the short summary. | `research/05 §4`, `research/03 D` |
| **`/rewind`** | Truncate history back to an already-cached earlier prefix → **re-hits** that entry. Cheaper than compaction. | `research/05 §4`, `research/03 C5` |
| **Context-edit / tool-result clearing** | Remove content from the middle of the prefix → invalidate everything downstream; pay one write to rebuild the suffix. Mitigated by `clear_at_least` (only clear when enough tokens are freed to be worth the rewrite). | `research/05 §4`, `research/03 D` |
| **Cold resume after CC upgrade** | New system prompt → the whole history sits behind a *different* prefix → **no hits at all**, even within TTL. Worst case; "the most expensive request you send." Distinct from the TTL cold resume already modeled. | `research/05 §4`, `research/03 C4` |
| **Sub-minimum no-cache** | If the cacheable prefix < model `min_cacheable`, it is **not cached** (no error): write billed at base 1.0×, no cache established, both cache fields stay 0. `min_cacheable` is already in config, just unused. | `research/02 §2` |
| **Multi-breakpoint 20-block walk-back** | On a miss, walk back block-by-block within a 20-block window per breakpoint; a trailing breakpoint that advances each turn lets large single-turn jumps still hit. Replaces the single-breakpoint approximation. | `research/02 §1`, `research/05 §2` |

New event types will be needed (e.g. `Compact`, `Rewind`, `ClearToolResults`, `Upgrade`);
`SwitchModel`/`SwitchEffort` are the template. Each new event also needs a REPL command in
`cli/session.py` and a ledger label in `cli/ledger.py:describe_event`.

## Open threads (still unresolved — settle as they bite)

- **Exact breakpoint count/positions Claude Code sets** — not officially documented;
  working assumption is 2 (one after system+tools, one trailing the history). Make it a
  config/`CacheState` knob; revisit only if fidelity needs it.
- **`clear_at_least` defaults** and whether CC's internal tool-result clearing uses the
  public context-editing API — secondary sources only; pick a sensible default and note it.
- **CC tiered-compaction token thresholds** (when auto-compact fires) — only needed if you
  model *automatic* compaction triggers; manual `/compact` doesn't need them.
- **Effort keying on the raw API** — authoritative for Claude Code (what we target);
  raw-API equivalence unverified. Stay CC-faithful.

## Validation ideas

- `/compact`: after compaction, a turn re-hits system+tools (high read) but the
  conversation read drops to ~the summary size — assert `cached_tokens` falls to the
  protected prefix + summary, not zero.
- `/rewind` to an earlier turn then a new turn → re-hits the earlier prefix (read > 0
  immediately, unlike a model switch).
- Sub-minimum: a tiny first turn (< `min_cacheable`) → `read==write==0` cache fields, write
  billed at 1.0× base.
- Upgrade cold-resume: even with `now - last_used < ttl`, a `/upgrade` event forces 0 hits.

## Environment

uv-managed: `uv run pytest`, `uv run scripts/run_repl.py`. Deps in `pyproject.toml`
(+`uv.lock`). Add any new dep with `uv add`.

## Pointers

- `design/stage1-engine-api.md` — the engine contract Stage 3 extends (note the
  `breakpoints` field it anticipated).
- `research/03-invalidation-scenarios.md` — the behavioral spec for every event above.
- `research/05-claude-code-caching.md` — `/compact`, `/rewind`, upgrade, walk-back detail.
- `research/02-anthropic-prompt-caching.md` — minimums, 20-block lookback, breakpoint count.
- `sim/engine.py` / `sim/events.py` — where new events and their bookkeeping land.
- `cli/session.py` — where new REPL commands land.
