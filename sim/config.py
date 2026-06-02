"""Model pricing/TTL configuration, loaded from a JSON file.

All numbers live in config/models.json (source: research/02 section 4) so they
can be updated when Anthropic changes them, rather than being baked into code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """Per-model rates, in dollars per million tokens, plus the cache minimum."""

    base_input: float       # $/MTok for uncached input
    output: float           # $/MTok for generated output
    min_cacheable: int      # prompts shorter than this are not cached (Stage 3)


@dataclass(frozen=True)
class Config:
    """The full configuration: cost multipliers, TTL options, and the model table."""

    read_multiplier: float          # cache read / refresh, e.g. 0.1x base
    write_5m_multiplier: float       # 5-minute cache write, e.g. 1.25x base
    write_1h_multiplier: float       # 1-hour cache write, e.g. 2.0x base
    ttl_seconds: dict[str, int]      # named TTL options, e.g. {"5m": 300, "1h": 3600}
    tokenizer: str                   # recorded for Stage 2; unused in Stage 1
    models: dict[str, ModelPricing]
    max_breakpoints: int = 2         # breakpoints CC keeps (anchored + trailing); Stage 3
    walkback_window_tokens: int = 20_000  # token-distance approx of the 20-block lookback

    def write_multiplier(self, ttl_seconds: int) -> float:
        """Pick the write multiplier that matches the active TTL.

        The 1-hour option costs more to write; anything else uses the 5-minute rate.
        """
        if ttl_seconds == self.ttl_seconds.get("1h"):
            return self.write_1h_multiplier
        return self.write_5m_multiplier


def load_config(path: str) -> Config:
    """Read and parse the JSON config file into a Config."""
    with open(path) as f:
        data = json.load(f)

    multipliers = data["multipliers"]
    models = {
        name: ModelPricing(
            base_input=m["base_input"],
            output=m["output"],
            min_cacheable=m["min_cacheable"],
        )
        for name, m in data["models"].items()
    }

    return Config(
        read_multiplier=multipliers["read"],
        write_5m_multiplier=multipliers["write_5m"],
        write_1h_multiplier=multipliers["write_1h"],
        ttl_seconds=data["ttl_seconds"],
        tokenizer=data["tokenizer"],
        models=models,
        max_breakpoints=data.get("max_breakpoints", 2),
        walkback_window_tokens=data.get("walkback_window_tokens", 20_000),
    )


def dollars(tokens: int, rate_per_mtok: float) -> float:
    """Convert a token count and a $/MTok rate into dollars."""
    return tokens / 1_000_000 * rate_per_mtok
