# How Claude Code Uses Prompt Caching (Simulator Target)

> This is the most important file for the simulator — it describes the exact session a "Claude Code-like" simulator must reproduce.
> Primary source: the dedicated official page **https://code.claude.com/docs/en/prompt-caching** (answers model-switch, effort, compaction, resume directly). Confidence **high** unless noted.

## 1. Session layout as a cached prompt

Claude Code orders each request so rarely-changing content comes first:

| Layer | Content | Changes when |
|---|---|---|
| **System prompt** | core instructions, **tool definitions**, output style | tool set changes, or Claude Code upgraded |
| **Project context** | CLAUDE.md, auto memory, unscoped rules | session start, or after `/clear` / `/compact` |
| **Conversation** | your messages, Claude's responses, tool results | every turn |

Claude Code puts tool definitions **inside the system-prompt layer** (not a separate top block in its conceptual model). API-level hierarchy is still `tools → system → messages`.

New content is appended at the end; "most of each request is identical to the one before it." The breakpoint slides forward each turn to include the latest assistant response.

## 2. Per-turn cost mechanics (the simulator's core loop)

Each turn:
- **reads** the unchanged prefix → `cache_read_input_tokens` billed at **0.1x** base
- **writes** only newly-appended blocks → `cache_creation_input_tokens` at **1.25x** (5m) / **2x** (1h)
- post-breakpoint tokens → `input_tokens` at full base

**Breakpoint walk-back example** (official): a `cache_control` block writes one entry = hash of prefix ending there. On a miss the system walks backward block-by-block within a **20-block lookback window**:
- Turn 1: 10 blocks, bp@10 → writes entry@10
- Turn 2: 15 blocks, bp@15 → miss@15, walk back, **hit@10**; blocks 11–15 fresh + written
- Turn 3: 35 blocks → window (35→16) finds nothing; entry@15 is one slot outside the window → **full miss** unless a 2nd breakpoint kept @15

→ This is why agent loops keep a trailing breakpoint that advances each turn. (Exact number/positions of breakpoints CC sets is **not** officially stated — inferred from API mechanics; see open questions.)

**Cost intuition (medium, practitioner blog):** Sonnet turn-11 example, 50K cached + 2K new: no cache = 51K×$3 = $0.153; with cache = 50K×$0.30 + 2K×$3 = $0.021 → **~7× cheaper**.

## 3. Cache key = (content prefix, **model**, **effort level**)

Two dimensions sit in the cache key, **not** the prompt text — changing either is a **full request rebuild**:

- **Model** — "Switching models recomputes the entire request even when the content is identical." `/model` → next request reads entire history with **no cache hits**. `opusplan` makes each plan-mode toggle a model switch.
- **Effort level** — "Each effort level has its own cache for the same model. Changing it mid-session recomputes the entire request." `/effort` → full miss; CC shows a **confirmation dialog** before applying because it's expensive. A no-op change (same level) keeps the cache.

(Contrast with API "thinking parameters" / "speed" rows in `03 §B`, which only bust message or system+message blocks — CC's effort knob is strictly worse: full rebuild.)

## 4. Session lifecycle events

| Event | Cache effect | Cost |
|---|---|---|
| **Cold resume** after idle > TTL | prefix evicted → recompute full input, re-establish cache | full write of whole history; "first turn back can be noticeably slower" |
| **Resume after CC upgrade** | new system prompt → history sits behind a different prefix → **no hits at all** | worst case; "most expensive request you send" |
| **`/compact`** (or auto-compact) | replaces history with a summary → invalidates **conversation layer** going forward; system+tools survive, project context re-hits only if CLAUDE.md/memory unchanged | summary *generation* shares the existing prefix (cheap read); post-compaction turn writes only the short summary — "the post-compaction turn is not the slow part" |
| **`/rewind`** | truncates back to an already-cached prefix → **hits** earlier entry | cheaper than compaction |
| **`/recap`** | appends output, prefix intact | cheap |
| **Edit CLAUDE.md / output style mid-session** | does **not** invalidate, but also does **not apply** until `/clear` / `/compact` / restart | none until applied |
| **Context editing / tool-result clearing** | removes content from middle of prefix → invalidates downstream; mitigated by `clear_at_least` (only clear when enough tokens removed to be worth a rewrite) | one cache write to rebuild the suffix |

## 5. TTL by auth mode
- **Claude Code subscription** → auto-requests **1-hour** TTL (free under plan). On overflow usage credits → drops to **5-min**.
- **API key / Bedrock / Vertex** → default **5-min**; opt into 1h via `ENABLE_PROMPT_CACHING_1H=1`; force 5m via `FORCE_PROMPT_CACHING_5M=1`.
- **Subagents** always 5-min TTL even on subscription; build their **own separate cache** (own system/tools, cold first call). A **fork** inherits the parent prefix and hits the parent's cache.

## 6. Cache scope gotchas (for a faithful simulator)
- Cache effectively scoped to **one machine + one directory** — system prompt embeds working dir, platform, shell, OS version, git branch/recent-commits, memory paths.
- Two sessions in **different dirs** (including **git worktrees of the same repo**) miss each other's cache; **same-dir** parallel sessions share it.

## 7. MCP / tools invalidation subtlety
- On modern models, MCP tools are **deferred** via tool search → a server connecting/disconnecting only **appends**, does **not** break cache.
- Tools break cache only when **loaded into the prefix** (tool search disabled/unavailable — **Haiku, Vertex, custom `ANTHROPIC_BASE_URL`**, `alwaysLoad`/threshold-loaded servers).
- Adding a **bare-name deny rule** (`Bash`, `WebFetch`, `Bash(*)`) removes a built-in tool from the system layer → invalidates cache. Scoped deny (`Bash(rm *)`) and all allow/ask rules do **not**.

## 8. Monitoring
"A high read-to-creation ratio means caching is working well. If creation stays high turn after turn, something is changing in your prefix." Track via statusline `current_usage`, `ccusage`, or the OpenTelemetry exporter. Anthropic's design narrative: "Prompt caching is everything" — plan mode, deferred tool loading, and compaction are all designed to preserve cache hits. (medium-high — https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything)

## Open questions
- How many breakpoints CC sets and exact positions (inferred, not specified).
- Whether CC keeps a trailing breakpoint to survive large single-turn jumps (e.g. huge tool result) across the 20-block window.
- Precise token thresholds for CC's tiered compaction (secondary source-code analyses only).
- Exact `clear_at_least` defaults; whether CC's internal tool-result clearing uses the public context-editing API.
- Academic source flagged but unread: "Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks" (arxiv 2601.06007).
