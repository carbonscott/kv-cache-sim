# Handoff: Claude Code-like prompt-cache session simulator

> Supersedes `handoffs/prompt-cache-simulator.md` (2026-05-25). That earlier doc framed the project as a *pedagogical breakpoint-placement analyzer* and was written in pure research phase with nothing decided. The goal has since evolved (see Goal) and most of its open threads are now resolved. Read this doc as current; read the old one only for the extra Anthropic-doc detail it captured.

## Goal

Build a simulator of a **Claude Code-like interactive session** that models prompt-cache state and cost. The user pastes prompts and issues commands; the simulator **fakes tool calls and produces output tokens to accumulate context** — it does **not** call any real LLM API. The point is to make the *consequences* of cache lifecycle events tangible: TTL expiry invalidates the cache, switching model rebuilds it, switching effort level rebuilds it, resuming a cold session pays a full cache write again, compaction rewrites history, etc. Success = the per-turn cache hit/miss and dollar cost track what real Claude Code would do across these events.

## Current state

**Research complete and written up; no code yet.** This conversation finished the research phase (six files under `research/`, numerically accurate and source-cited — see Pointers) and converged on the design. The build is the next action and has **not** started. Implementation is staged (see Open threads); **Stage 1 = a pure engine + cost model, no UI, driven by a scripted event list** is the immediate next work. The design is converged enough to start Stage 1 without further questions.

## Decisions (with why)

- **Interactive CLI, not a web app or notebook.** The thing being simulated *is* a terminal session; a CLI mirrors the mental model and adds zero presentation layer unrelated to caching. (See Ruled out for the web-app rejection.)
- **Two front-ends — interactive REPL *and* batch/scripted runner — built as thin shells over one shared engine.** Decided "both from the start." REPL gives the live Claude-Code feel; batch gives reproducible experiments and easy testing. Same `apply_event` core underneath so it's not a fork.
- **Engine is a pure, importable module with no `print`/I/O inside it.** It returns structured results; the CLI layer does all printing. Reason: keeps Stage 1 correct/testable and makes a future TUI/web view a drop-in.
- **Real tokenizer now, not a chars/4 heuristic.** Use a tiktoken-style tokenizer (`o200k_base`) so pasted prompts get realistic counts. (Caveat in Load-bearing assumptions — it is *not* Anthropic's true tokenizer.) Make the tokenizer a config-swappable knob.
- **Fake tool-call output size is specified by the user per call** (e.g. a command like `tool read_file 1500` appends 1500 output tokens). Reason: full control for experiments; most realistic.
- **Config file holds all model data** — pricing table, TTLs, per-model minimum-cacheable-token thresholds. The research numbers live there, not baked into code. Reason: easy to update when Anthropic changes numbers (they have before).
- **Python 3.**
- **Simulated, advanceable clock — not wall-clock.** Commands like `advance 6m` jump simulated time so the user can watch the cache expire without real waiting.
- **Cache key = (content prefix, model, effort level).** This is the core invariant: changing model OR effort = full rebuild; changing prefix content = partial rebuild from the first divergence. Confirmed official for Claude Code (see `research/05`).
- **Staged build, one handoff per stage boundary** (not all stages decomposed up front). Reason: Stages 2–3 task shapes depend on Stage 1's API, so decomposing them now would be guessing.

## Ruled out (with why)

- **Web app / notebook front-end** — adds a UI layer with nothing to do with the caching mechanics under study; a CLI matches what Claude Code actually is. Not permanently rejected — engine is kept UI-agnostic so a view can be added later.
- **Cheap chars/4 token heuristic** — rejected in favor of a real tokenizer because the user wants counts that approximate reality, not just relative ratios.
- **Hardcoded pricing/TTL constants** — rejected in favor of a config file (numbers change too often).
- **Decomposing all three stages into granular tasks now** — rejected: shared design frame would duplicate and drift across per-stage docs, and later stages depend on Stage 1's realized API.

## Load-bearing assumptions

The intern must not silently violate these.

- **Target is Claude Code behavior specifically**, not the generic raw Anthropic API. The model/effort-in-cache-key rule and the `/compact`, `/rewind`, cold-resume semantics come from `code.claude.com/docs/en/prompt-caching`. The raw-API equivalent for "effort level" is *not* separately documented — do not assume they match.
- **The tokenizer is an approximation, not ground truth.** `o200k_base` is OpenAI's tokenizer; Anthropic does not publish a public tokenizer for the 4.x models, and Opus 4.7+ reportedly runs ~35% heavier on the same text. Counts are *realistic and consistent*, not true Anthropic token counts. The simulator should be honest about this (and the swappable-tokenizer knob exists for exactly this reason).
- **"Effort level switch is NOT free"** — the user's original prompt said "switching models (effort level is fine)," but the research found that in Claude Code, switching effort level invalidates the *entire* cache, same as a model switch (it's in the cache key; CC even shows a confirmation dialog). The only cheap thinking-related knob is changing the raw-API thinking *budget*, which busts message blocks only. Encode the expensive behavior.
- **Numbers are current as of June 2026 and Anthropic changes them.** Minimums bumped before; workspace-scoping changed Feb 2026. Treat `research/02` + the config file as the source, and re-verify before trusting any constant long-term.
- **User's working preferences** (global CLAUDE.md): deliver exactly what's discussed, add features incrementally, readability over cleverness, and *mention* improvement ideas rather than implementing them unprompted. The conversation has been gated (clarify/approval/no-eager postures) — the user wants to approve scope before action.

## Open threads

- **Stage 1 engine API shape is undesigned.** Need to define `CacheState` fields (≈ `cached_prefix_length`, `model`, `effort`, `ttl`, `last_used`) and the `apply_event(state, event) -> (new_state, cost_breakdown)` signature. This is the first design task and it constrains Stages 2–3.
- **The staged plan** (agreed in principle, not yet written as tasks):
  - *Stage 1* — engine + cost model + fake clock, scripted events, no UI. Prove bookkeeping against a worked example (e.g. the ~7×-cheaper turn-11 case in `research/05`).
  - *Stage 2* — interactive REPL + batch runner over the engine (paste prompt, fake tool call, advance clock, see per-turn hit/miss + running cost).
  - *Stage 3* — long-tail events: `/compact`, `/rewind`, effort switch, context-edit/tool-result clearing, cold resume, sub-minimum no-cache, multi-breakpoint 20-block lookback.
- **How many cache breakpoints Claude Code actually sets, and where** — not officially documented. Working assumption: 2 breakpoints (one after system+tools, one trailing the message history that advances each turn). Tunable; revisit if it matters for fidelity.
- **Within Stage 2, wire REPL or batch first?** Decided "both eventually"; order not fixed. (Claude's lean: REPL first, since the goal is to *feel* the turns — but unconfirmed.)
- **Model rate limits (ITPM)?** Carried over from the May 25 doc, still unresolved. Cache reads still count against ITPM at read-token count — useful to teach but expands scope. Parked.

## Pointers

Read these first, in order:

- **`research/00-summary.md`** — the model-in-one-page: state machine sketch, the per-turn cost formula, the full event→effect table, and the confidence call. Start here.
- **`research/05-claude-code-caching.md`** — the simulator's actual target: session layout, cache-key dimensions, lifecycle events (`/compact`, `/rewind`, cold resume), breakpoint walk-back example. The most important file.
- **`research/02-anthropic-prompt-caching.md`** — exact numbers for the config file: pricing multipliers (read 0.1×, 5m write 1.25×, 1h write 2×), per-model $/MTok table, minimums (1,024 / 4,096 / 2,048), TTLs, 20-block lookback, 4 breakpoints.
- **`research/03-invalidation-scenarios.md`** — the behavioral rules: every state change → cache effect → cost. This is the `apply_event` spec.
- **`research/01-kv-cache-theory.md`** — why caching is prefix-only and why one changed token invalidates everything downstream. Background for getting the prefix logic right.
- **`research/04-provider-comparison.md`** — OpenAI/Gemini contrast; only relevant if the simulator is ever made provider-agnostic (engine core already supports it: prefix + TTL + read-discount, with per-provider write/storage cost layered on).
- **`handoffs/prompt-cache-simulator.md`** — the superseded May 25 handoff. Don't act on its goal; mine it only for extra Anthropic-doc edge-case notes (byte-equality nuances, thinking-block caching detail).
- **No code or repo exists yet.** The project is not a git repo. Don't look for source.
