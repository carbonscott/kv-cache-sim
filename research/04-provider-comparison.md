# Cross-Provider Caching Comparison

> Comparative framing so the simulator's core ("prefix + TTL + read-discount") can stay provider-agnostic, with per-provider cost functions layered on top. Anthropic gets its own deep dives (`02`, `05`); summarized here for contrast.
> Confidence: **high** = official docs · **medium** = reputable blog · **low** = unverified. Verified June 2026.

## 1. OpenAI — automatic prefix caching

| Property | Value | Conf. |
|---|---|---|
| Trigger | **Fully automatic**, no breakpoint, no code change | high |
| Minimum prompt | **1,024 tokens**; caches longest prior prefix | high |
| Prefix increment | grows in **128-token** steps above 1,024 | medium |
| Read discount | **Model-dependent.** Launch gpt-4o era: 50% (0.5x). Current GPT-5 family: **90% (0.1x)** | high |
| Write cost | **None — writes are free** | high |
| TTL | active **5–10 min of inactivity**, max ~1 hour | high |
| Extended retention | up to **24 h** (gpt-5.x, gpt-4.1) | high |
| Match | exact prefix only | high |

Source: https://developers.openai.com/api/docs/guides/prompt-caching · https://developers.openai.com/api/docs/pricing

**Flag:** the old "0.5x" discount is stale for current models (now 0.1x). Treat OpenAI's discount as a **per-model parameter**, not a fixed constant.

## 2. Google Gemini — implicit + explicit caching

**Implicit** (automatic, default on 2.5+):

| Property | Value | Conf. |
|---|---|---|
| Trigger | automatic, default on; savings best-effort (not guaranteed) | high |
| Min tokens | 2.5/3.5 Flash: **1,024**; 2.5 Pro / 3 Pro: **4,096** | high |
| Discount | **90%** off cached input | high |
| Storage cost | **none** | high |

**Explicit** (`CachedContent` object referenced by handle):

| Property | Value | Conf. |
|---|---|---|
| Trigger | manual; cost savings **guaranteed** on hit | high |
| Min tokens | ~**32,768** historically; current page defers to model minimums | medium |
| Read price | **10% of base input** (90% off) | high |
| **Storage rent** | **Pro: $4.50 / 1M tok / hour; Flash: $1.00 / 1M tok / hour** — charged continuously while cache lives | high |
| Default TTL | **1 hour** if unset | high |
| Configurable TTL | **Yes — arbitrary**, set via `ttl`/`expire_time`, updatable, no stated min/max | high |

Source: https://ai.google.dev/gemini-api/docs/caching · https://ai.google.dev/gemini-api/docs/pricing

## 3. Comparison table

| Dimension | Anthropic | OpenAI | Gemini |
|---|---|---|---|
| Model | explicit `cache_control` breakpoints | automatic prefix | implicit (auto) + explicit (object) |
| Min tokens | 1,024 or 4,096 (model-dependent) | 1,024 | implicit 1,024–4,096; explicit ~32,768² |
| Default TTL | 5 min (1 h optional) | 5–10 min idle, ~1 h max (24 h ext.) | 1 h (explicit, configurable) |
| Configurable TTL | tiers only (5 min / 1 h) | no (extended-retention flag) | **yes, arbitrary** |
| Cache-write cost | **yes — 1.25x (5m) / 2x (1h)** | **none (free)** | none; **storage rent instead** |
| Read discount | **0.1x** (90% off) | 0.5x legacy → **0.1x** (GPT-5) | **0.1x** (90% off) |
| Ongoing storage cost | no | no | **yes (explicit only)** |

² explicit min is medium-confidence — see open questions.

## 4. Modeling-relevant conceptual differences

- **Cost-of-write differs fundamentally:**
  - **Anthropic** — one-time **write surcharge** (1.25x–2x input). Break-even depends on hit count.
  - **OpenAI** — writes **free**; caching is never a net loss.
  - **Gemini explicit** — no write surcharge but **storage rent per token-hour**; cost is a function of *time held*, not writes. The only model where an **idle, unused cache still costs money**.
- **TTL economics:** Anthropic/OpenAI TTL is ~free to extend; Gemini's configurable TTL is a direct cost lever (`storage_rate × cached_tokens × time_alive`).
- **Determinism:** OpenAI + Gemini-implicit are best-effort (hit not guaranteed); Anthropic-explicit + Gemini-explicit give deterministic hits within TTL.
- **Common core (all three):** prefix-based, order-sensitive, TTL-bounded, ~0.1x read discount on current flagships, ~1k-token floor. → Simulator can model "prefix + TTL + read-discount" uniformly, then layer provider-specific write/storage functions.

## Open questions / couldn't verify
- Gemini explicit-cache current minimum (32,768 is from older docs).
- OpenAI 128-token increment (from cookbook/blog, not core guide).
- Gemini implicit eviction/TTL window (unpublished).
