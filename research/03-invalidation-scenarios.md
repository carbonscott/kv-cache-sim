# Invalidation & Lifecycle Scenarios

> The behavioral rules the simulator enforces. Each row = a state change → what it does to the cache → the cost consequence.
> Confidence: **high** = official docs · **medium** = reputable secondary. Claude-Code-specific framing in `05`.

## A. The one rule everything reduces to
Cache hits require **100% identical prompt segments**, including all text and images, **up to and including the block marked with cache control**. Any change to the cached prefix invalidates **from the change point onward** (the prefix before it survives). This is exact **token-level prefix matching** (see `01 §3–4`). Hierarchy of invalidation cascade: **`tools → system → messages`** — a change at one level invalidates that level and all later ones. (high)

## B. Official invalidation table (Anthropic API)

| Change | What it invalidates | Conf. |
|---|---|---|
| **Tool definitions changed** | **entire cache** (tools + system + messages) | high |
| `web_search` toggled | system + messages | high |
| Citations toggled | system + messages | high |
| **Speed setting** (`speed:"fast"`) | system + messages | high |
| `tool_choice` changed | messages only | high |
| Images added/removed | messages only | high |
| **Thinking *parameters* changed** (budget/enable) | messages only | high |

Source: https://platform.claude.com/docs/en/build-with-claude/prompt-caching

## C. The named scenarios (user's list)

### C1. TTL expiry → cache invalid
After the TTL of **inactivity** (default 5 min; 1 h if opted in), the entry is **evicted**. The next matching request recomputes the full input and **pays a full cache write again** (1.25x or 2x). Each cache **read refreshes the timer for free**, so an active session stays warm; a gap longer than the TTL goes cold. (high)

### C2. Model switching → cache invalid (CONFIRMED OFFICIAL)
> "Each model has its own cache. Switching models recomputes the entire request even when the content is identical." (high — https://code.claude.com/docs/en/prompt-caching)

Model is part of the **cache key**, not the prompt text — different model = different KV matrices = nothing reusable. **Full rebuild** of tools + system + messages. (Claude Code's `opusplan` makes every plan-mode toggle a model switch → fresh cache.)

### C3. Effort / reasoning-level switching → cache invalid (CONFIRMED OFFICIAL)
> "Each effort level has its own cache for the same model. Changing it mid-session recomputes the entire request, and Claude Code asks you to confirm before applying the change." (high — code.claude.com/docs/en/prompt-caching)

**Effort level is part of the cache key, like model** → toggling `/effort` mid-session = **full request rebuild** (strictly worse than the thinking-*parameter* row in §B, which only busts message blocks). Claude Code gates it behind a confirmation dialog because it's expensive. A change that resolves to the level already in effect keeps the cache.

> ⚠️ Note on the user's phrasing "switching models (effort level is fine)": in **Claude Code**, switching effort is **NOT** free — it invalidates the full cache, same as a model switch. The thing that *doesn't* fully invalidate is changing only the extended-thinking **budget** via the raw API (messages-only bust, §B).

### C4. Session resume → cost of writing new cache (CONFIRMED OFFICIAL)
> "Cached prefixes expire after a period of inactivity... After a long enough gap, the next request recomputes the full input and re-establishes the cache, which is why the first turn back after stepping away can be noticeably slower." (high — code.claude.com/docs/en/prompt-caching)

- Cold-resume cost depends on auth/TTL: API key defaults to **5-min** idle window; Claude Code subscription auto-requests **1-hour** TTL (env: `ENABLE_PROMPT_CACHING_1H=1`, `FORCE_PROMPT_CACHING_5M=1`).
- **Worst case — resume after a Claude Code upgrade**: the system prompt changed, so the whole history sits behind a different prefix → **no cache hits at all**, "the first turn back into a long session can be the most expensive request you send."

### C5. Prompt edits / context growth
- **Append (normal growth):** safe. New blocks at the end don't touch the cached prefix; prior prefix still hits (as long as < 20 new blocks/turn, within the lookback window). Pay 0.1x read of history + 1.25x write of new blocks.
- **Edit/insert earlier in history:** invalidates from the edit point onward; everything before survives. `/rewind`-style truncation back to an already-cached prefix → re-**hits** the earlier entry (cheap).

### C6. Tool use across turns
- Appended `tool_result` blocks are normal message content; they extend the prefix and only the new blocks are written. (high)
- **Changing the `tools` array busts the entire cache** (§B). `tool_choice` change busts messages only.
- On older Opus/Sonnet and **all Haiku**: interleaving non-tool-result user content can **strip cached thinking blocks** and drop following messages from cache. On **Opus 4.5+ / Sonnet 4.6+** thinking blocks are preserved by default → cache stays valid. (high)

## D. Extra scenarios (beyond the named list, relevant to a CC simulator)
- **Compaction / `/compact`** — replaces history with a summary → invalidates the **conversation layer** going forward; system+tools survive. (Detail in `05`.) (high)
- **Context editing / tool-result clearing** — removes content from the middle of the prefix → invalidates downstream; pay one write to re-establish. Mitigated by `clear_at_least`. (Detail in `05`.) (high)

## Open questions
- Effort-level keying on the **raw API** (outside Claude Code) is not separately documented — authoritative *for Claude Code*; raw-API equivalence unverified.
- Exact eviction timing post-TTL (secondary only).
