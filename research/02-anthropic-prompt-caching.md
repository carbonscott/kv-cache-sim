# Anthropic Prompt Caching — Mechanics & Pricing

> Numerically precise reference for the simulator's Anthropic cost model. Lifecycle/invalidation scenarios are in `03`; Claude-Code-specific behavior is in `05`.
> Confidence: **high** = official Anthropic docs (`platform.claude.com`) · **medium** = reputable secondary.
> Note: Opus 4.7+ uses a new tokenizer that "may use up to 35% more tokens for the same fixed text" — relevant when reasoning about minimums/costs on newest models.

## 1. `cache_control` mechanics

| Aspect | Detail | Conf. |
|---|---|---|
| Marker | `"cache_control": {"type": "ephemeral"}` on a content block — marks the **end** of a reusable prefix | high |
| Max breakpoints | **up to 4** per request | high |
| Placement | in `tools`, `system`, `messages`. Prefixes built in order **`tools → system → messages`** | high |
| Cascade | a breakpoint writes **exactly one** cache entry: a hash of the prefix ending at that block (caches everything from prompt start up to and including it) | high |
| Lookback window | **20 blocks** — on a miss, the system checks at most 20 positions backward per breakpoint (the breakpoint itself counts as #1). Matters for auto-caching: each turn must add < 20 blocks | high |
| Two modes | **automatic** (single top-level `cache_control`, breakpoint auto-advances as conversation grows) and **explicit** (place on individual blocks) | high |
| Pre-warm | `max_tokens: 0` writes the cache without generating output (incurs a write charge if not already cached) | high |
| Usage fields | `cache_creation_input_tokens` (written), `cache_read_input_tokens` (read), `input_tokens` (only tokens **after the last breakpoint**). `total_input = cache_read + cache_creation + input_tokens` | high |

Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

## 2. Minimum cacheable prompt length (per model)

| Model | Minimum tokens | Conf. |
|---|---|---|
| Opus 4.8, Sonnet 4.6, Sonnet 4.5, Opus 4.1, Opus 4 (dep.) | **1,024** | high |
| Opus 4.7, Opus 4.6, Opus 4.5 | **4,096** | high |
| Haiku 4.5 | **4,096** | high |
| Haiku 3.5 (retired exc. Vertex) | **2,048** | high |

- **Below minimum:** "Shorter prompts cannot be cached, even if marked with `cache_control`... processed without caching, and **no error is returned**." Detect via usage: both write & read tokens = 0 ⇒ not cached. (high)
- ⚠️ Common stale guess "1,024 Opus/Sonnet, 2,048 Haiku" is **outdated**: newer Opus 4.5–4.7 and **Haiku 4.5 require 4,096**; 2,048 applies only to old Haiku 3.5.

## 3. TTL options & refresh

| Aspect | Detail | Conf. |
|---|---|---|
| Default TTL | **5 minutes** | high |
| Extended TTL | **1 hour** at higher write cost; `"cache_control": {"type":"ephemeral","ttl":"1h"}` | high |
| Refresh-on-use | "The cache is refreshed for no additional cost each time the cached content is used" → every cache **read resets the TTL timer** (the "sliding window"; the *term* is secondary, the mechanism is official) | high mech. / medium term |
| Mixing TTLs | longer-TTL entries must appear **before** shorter-TTL ones | high |
| Expiry → next request | after TTL of inactivity, evicted; next matching request is a **full write again** (pays 1.25x/2x) | high (by derivation) |
| 1h availability | a paid tier (2x write), GA via the `ttl` field on Claude API/AWS/Bedrock/Vertex (Foundry beta). Historically required an `extended-cache-ttl` beta header — may be version-dependent | high avail. / medium beta-header |

## 4. Pricing multipliers (EXACT, verbatim from docs)

Relative to base input price:
- **5-minute cache write = 1.25× base input**
- **1-hour cache write = 2× base input**
- **Cache read (hit) / refresh = 0.1× base input** (90% discount)

> Break-even (official): caching pays off "after just one cache read for the 5-minute duration, or after two cache reads for the 1-hour duration." Multipliers stack with Batch API and data-residency multipliers.

### Per-model $/MTok (official pricing page, high)

| Model | Base input | 5m write (1.25x) | 1h write (2x) | Read (0.1x) | Output |
|---|---|---|---|---|---|
| **Opus 4.8 / 4.7 / 4.6 / 4.5** | $5 | $6.25 | $10 | $0.50 | $25 |
| **Opus 4.1 / Opus 4 (dep.)** | $15 | $18.75 | $30 | $1.50 | $75 |
| **Sonnet 4.6 / 4.5 / Sonnet 4 (dep.)** | $3 | $3.75 | $6 | $0.30 | $15 |
| **Haiku 4.5** | $1 | $1.25 | $2 | $0.10 | $5 |
| **Haiku 3.5 (retired)** | $0.80 | $1.00 | $1.60 | $0.08 | $4 |

Data-residency `inference_geo:"us"` adds a 1.1x multiplier on all categories (Opus 4.6 / Sonnet 4.6+).

Source: https://platform.claude.com/docs/en/about-claude/pricing

## 5. Isolation
- Caches isolated **between organizations** (never shared even with identical prompts).
- As of **Feb 5, 2026**: also isolated **per workspace** within an org on Claude API / Claude Platform on AWS / Microsoft Foundry (beta). Bedrock & Vertex remain org-level only. (high)
- **ZDR** (Zero Data Retention): cached data not stored after the response returns. (high)

## Per-turn cost shape (the formula the simulator implements)
Steady-state multi-turn: each turn **reads** the unchanged prefix at 0.1x + **writes** only the newly-appended blocks at 1.25x (5m) or 2x (1h). `input_tokens` (post-breakpoint) billed at full base. → cheap read of history + small write of new turn. See `03` and `05` for when this breaks.

## Open questions
- "Sliding window" is not the literal docs phrase (mechanism is official).
- Exact post-TTL eviction timing ("promptly but not immediately") is secondary only.
- Per-user (sub-workspace) isolation not addressed in docs.
