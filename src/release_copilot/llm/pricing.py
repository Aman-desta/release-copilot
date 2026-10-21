"""Model prices in USD per 1M tokens, used for cost accounting.

Prices change: check the provider's pricing page before relying on these numbers, and add every model you
configure. Providers return dated model names (``gpt-4.1-mini-2025-04-14``), so lookups match the longest prefix.
An unknown model makes the cost ``None`` (unknown), never 0.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_million: float
    output_per_million: float


PRICES: dict[str, Price] = {
    "gpt-4.1": Price(2.00, 8.00),
    "gpt-4.1-mini": Price(0.40, 1.60),
    "gpt-4.1-nano": Price(0.10, 0.40),
    "gpt-4o": Price(2.50, 10.00),
    "gpt-4o-mini": Price(0.15, 0.60),
    "o4-mini": Price(1.10, 4.40),
    "text-embedding-3-small": Price(0.02, 0.0),
    "text-embedding-3-large": Price(0.13, 0.0),
    "fake": Price(1.00, 2.00),  # fake models: non-zero so tests can check the arithmetic
}


def price_for(model: str) -> Price | None:
    matches = [name for name in PRICES if model == name or model.startswith(name + "-")]
    return PRICES[max(matches, key=len)] if matches else None
