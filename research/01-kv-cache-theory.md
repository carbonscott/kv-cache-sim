# Transformer KV-Cache Theory

> Foundation for the simulator. Everything about prompt caching as a *product* derives from these mechanics.
> Confidence legend: **high** = official docs / well-known paper · **medium** = reputable blog · **low** = forum/unverified.

## 1. What the KV cache physically is

In a transformer decoder, each self-attention layer projects every token into three vectors — **query (Q)**, **key (K)**, **value (V)**. A token's attention output is a weighted sum of the **value** vectors of all preceding tokens, where weights come from the dot product of that token's **query** with every **key**. During autoregressive generation the K and V of already-processed tokens never change, so they are stored ("cached") instead of recomputed each step. That store is the **KV cache**. (high — https://docs.modular.com/glossary/ai/kv-cache/)

**Why it avoids recomputation:** without a cache, generating token *n* re-derives K/V for all *n−1* prior tokens every step → O(n²) per token. With the cache the model computes Q/K/V only for the new token and reuses stored K/V → O(n) per token. (high — https://huggingface.co/blog/not-lain/kv-caching)

**Memory cost** scales linearly with sequence length:

```
KV bytes ≈ 2 (K and V) × seq_len × num_layers × num_kv_heads × head_dim × bytes_per_element
```

Grows with sequence length and layers; proportional to heads × head_dim. At long context the KV cache — not the weights — dominates GPU memory. Illustrative: a ~4B model adds ~136 KB/token, so a 20-turn chat exceeds 100 MB (medium, single secondary source — treat the constant as illustrative). Grouped/Multi-Query Attention shrinks `num_kv_heads` precisely to cut this. (high scaling / medium constant — https://www.emergentmind.com/topics/transformer-kv-cache)

## 2. Prefill vs. decode

LLM inference has two phases. This distinction is **why caching saves money**:

- **Prefill** — processes the entire prompt in **one parallel forward pass**, building the KV cache for every prompt token. Large matrix–matrix multiplies; cost is **quadratic in prompt length**; **compute-bound**. (medium — https://redis.io/blog/prefill-vs-decode/)
- **Decode** — generates output **one token at a time**. Each step computes Q/K/V for just the new token, then **reads the entire KV cache** to attend. Small matrix–vector multiplies; **memory-bandwidth-bound**. (medium — same)

**Product link:** prefill is the expensive step that builds the cache. If two requests share a leading prompt, prefilling it produces *identical* KV. Prompt caching saves and replays that prefill work, so a cache hit skips re-prefilling the shared prefix — cutting the compute-bound cost and latency. **All prompt-caching savings come from the prefill side.** (high — https://arxiv.org/abs/2309.06180)

## 3. Prefix matching — why caching is prefix-ONLY (the load-bearing property)

Decoders use **causal (masked) attention**: each token attends only to itself and tokens *before* it. So a token's representation — and its K/V — depends only on tokens at positions ≤ its own. (medium — https://vinidlidoo.github.io/blog/kv-cache-invalidation/)

Two consequences that the simulator must model exactly:

1. **Identical prefixes → identical K/V.** Two sequences starting with the same tokens produce bit-identical K/V across the shared region. The cache from one is valid for the other — **but only up to the first point of divergence.** (high — https://docs.vllm.ai/en/v0.9.2/design/automatic_prefix_caching.html)

2. **Changing ONE token invalidates everything from that token onward.** Every *later* token attends back to the changed token, so its K/V enters all subsequent positions' attention. Altering/inserting/deleting a single token changes the K/V of that token **and every token after it**. State *before* the change survives; everything from the change to the end must be recomputed. **You don't lose a little work — you lose all work after the edit.** (high — vLLM + medium — vinidlidoo)

vLLM operationalizes this: a KV block is keyed by "the tokens within the block **and the tokens in the prefix before it**" — sharable only when the entire preceding context matches. (high — vLLM design docs)

## 4. What counts as "the same prefix"

- **Token-level, not character-level.** The key is the token sequence. Strings that look alike but tokenize differently (trailing space, whitespace, Unicode normalization) won't match. (high — https://developers.openai.com/api/docs/guides/prompt-caching)
- **Position matters.** Shared content must occupy the *same positions* starting at position 0 — it must genuinely be a prefix. Inserting one token earlier shifts all positions and breaks the match from there on. (high — OpenAI docs)
- **Append-only is safe; insert/edit/prepend is not.** Anything **appended after** the cached region leaves the prefix reusable (causal attention: later tokens can't affect earlier). Anything **inserted/changed/removed within or before** it invalidates from that point. → The universal guidance: **static content at the front, variable content at the end.** (high — OpenAI + Anthropic docs)

## 5. From hardware cache to billable product

Providers save the prefill-computed KV for a prompt prefix and replay it on later requests sharing that prefix. Two billed events:

- **Cache write** — storing the KV (cost of the prefill that populates it).
- **Cache read** — reusing it (a fraction of the write cost).

**Why a TTL must exist:** the KV cache is large (§1) and lives in scarce GPU memory. It can't be kept forever for every past request, so providers **evict after inactivity**. The TTL is the guarantee for how long a cached prefix stays resident; reuse after expiry pays the full write again. PagedAttention manages KV in paged blocks "like an OS cache," cutting fragmentation waste 60–80% → <4% and enabling block-level sharing. (high — https://arxiv.org/abs/2309.06180)

## Open questions / couldn't verify
- The "136 KB/token" constant is a single secondary source; scaling shape is solid, exact constant is not.
- "prefill = compute-bound, decode = memory-bound" is cross-blog consensus, not one canonical paper.
- Bit-exact K/V across requests in production (float non-determinism) is unconfirmed; providers sidestep it by keying on **token identity**, so prefix reuse correctness is unaffected (high on the keying).
