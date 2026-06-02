# KV / Prompt Cache Research — Summary & Confidence Assessment

> Goal: gather enough numerically-accurate, source-cited knowledge to model prompt caching in a simulated Claude Code-like session (fake tool calls, accumulate output tokens, track cache state — no real LLM API calls).
> Status: **Research complete. Confidence: HIGH — the cache model can be built from these notes without further research.**

## Files in this folder
1. **`01-kv-cache-theory.md`** — transformer KV-cache mechanics; why caching is prefix-only; one changed token invalidates everything downstream.
2. **`02-anthropic-prompt-caching.md`** — exact Anthropic mechanics, minimums, TTLs, pricing multipliers, per-model $/MTok.
3. **`03-invalidation-scenarios.md`** — every state change → cache effect → cost (the behavioral rules).
4. **`04-provider-comparison.md`** — OpenAI / Gemini contrast for a provider-agnostic core.
5. **`05-claude-code-caching.md`** — how Claude Code actually structures & re-caches a session (**the simulator's target**).

## The model in one page

A session is an **ordered token sequence** = `[tools] + [system + project context] + [growing message history]`. Cache state is a **prefix** of that sequence, tagged with `(model, effort_level, ttl_seconds, last_used_time)`.

**Core invariant:** a cache hit is valid only for the **longest shared token prefix** up to the first divergence. Everything from the first changed/inserted/deleted token onward must be re-written.

**Per-turn cost** (Anthropic):
```
turn_cost = cache_read_tokens × base×0.1          # unchanged prefix
          + cache_write_tokens × base×(1.25 | 2.0) # newly appended blocks (5m | 1h TTL)
          + post_breakpoint_tokens × base×1.0       # tail after last breakpoint
          + output_tokens × output_rate
```

**Cache key = (prefix content, model, effort_level).** Changing model OR effort → full rebuild (key miss). Changing prefix content → partial rebuild from divergence point.

**TTL:** cache lives `ttl_seconds` from `last_used_time`; every read refreshes it (sliding window). Past TTL → evicted → next request is a full write. CC subscription = 1h TTL; API key = 5m default.

### Events the simulator must handle (all with confirmed rules)
| Event | Effect |
|---|---|
| Append turn / tool result | read prefix (0.1x) + write new blocks (1.25x) |
| Idle past TTL → resume | full write of whole history |
| Switch model (`/model`) | full rebuild |
| Switch effort (`/effort`) | full rebuild (CC confirms first) |
| Edit earlier message / `tools` | invalidate from change; tools-change = entire cache |
| `/compact` | conversation layer rewritten forward; system+tools survive |
| `/rewind` | hits earlier cached prefix (cheap) |
| Context-edit / clear tool results | invalidate downstream + one write |
| CC upgrade then resume | new system prompt → zero hits (worst case) |
| Below min tokens (1,024 / 4,096) | not cached at all |

### Hard numbers locked down
- Read **0.1x**, 5-min write **1.25x**, 1-h write **2x** base input (Anthropic, exact/official).
- Min cacheable: **1,024** (Sonnet 4.x, Opus 4.1/4.8) / **4,096** (Opus 4.5–4.7, Haiku 4.5) / **2,048** (Haiku 3.5).
- Per-model $/MTok table in `02 §4`.
- TTL: **5 min** default, **1 h** optional; lookback window **20 blocks**; up to **4 breakpoints**.
- OpenAI: free writes, 0.1x reads (GPT-5), ~5–10 min idle TTL. Gemini explicit: storage rent $1–4.50/1M tok/hr, configurable TTL.

## Open / unverified items (none block building the model)
1. **Exact breakpoint count/positions Claude Code sets** — inferred from API mechanics, not officially specified. *Modeling choice:* assume 2 breakpoints (one after system+tools, one trailing the message history, advancing each turn). Tunable.
2. **Effort-level keying on the raw API** (outside CC) — authoritative for Claude Code; raw-API equivalence unverified. Simulator targets CC, so use CC's rule.
3. **CC tiered-compaction token thresholds** — secondary-source only; not needed unless modeling auto-compact triggers precisely.
4. **Exact post-TTL eviction timing** ("promptly but not immediately") — secondary; model as hard expiry at TTL.
5. **Unread academic source** — arxiv 2601.06007 "Don't Break the Cache" (long-horizon agentic caching eval); optional rigor, not required.
6. **X/Twitter** — practitioner anecdotes (cost ratios, idle tax) came via web-surfaced blogs rather than direct X browsing; all load-bearing numbers are backed by official docs, so this gap doesn't affect the model.

## Recommended next step (NOT done this turn — research-only per scope)
Design the simulator state machine: a `CacheState` holding `(prefix_tokens, model, effort, ttl, last_used)`, a clock for TTL expiry, a token-counter for fake tool-call output, and an event handler implementing the table above. The numbers and rules to parameterize it are all in files `01`–`05`.
