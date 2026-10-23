"""Model pricing for cost estimates.

We ship no third-party prices: vendor prices change often and a stale table
would silently produce wrong costs. Operators supply a JSON file::

    {"version": "2026-10-01",
     "models": [{"provider": "openai", "model": "gpt-x", "input_per_mtok": 1.0,
                 "output_per_mtok": 4.0}],
     "zero_cost_providers": ["ollama"]}

Calls that can't be priced get ``cost_usd = NULL`` and are counted separately,
so totals are reported as lower bounds instead of being understated silently.
Local providers listed below are free of API charges by definition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

DEFAULT_ZERO_COST_PROVIDERS = frozenset({"ollama"})
_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclass(frozen=True)
class PriceTable:
    models: dict[tuple[str, str], ModelPrice] = field(default_factory=dict)
    zero_cost_providers: frozenset[str] = DEFAULT_ZERO_COST_PROVIDERS
    version: str = "builtin"

    @classmethod
    def load(cls, path: Path | None) -> PriceTable:
        if path is None:
            return cls()
        document = json.loads(path.read_text())
        models = {
            (m["provider"].lower(), m["model"]): ModelPrice(
                Decimal(str(m["input_per_mtok"])), Decimal(str(m["output_per_mtok"])))
            for m in document.get("models", [])
        }
        zero = frozenset(p.lower() for p in document.get(
            "zero_cost_providers", sorted(DEFAULT_ZERO_COST_PROVIDERS)))
        return cls(models, zero, str(document.get("version", "unversioned")))

    def cost(self, provider: str | None, model: str | None, input_tokens: int | None,
             output_tokens: int | None) -> Decimal | None:
        if provider is None:
            return None
        provider = provider.lower()
        if provider in self.zero_cost_providers:
            return Decimal(0)
        if model is None or input_tokens is None or output_tokens is None:
            return None
        price = self.models.get((provider, model))
        if price is None:
            return None
        total = (Decimal(input_tokens) * price.input_per_mtok
                 + Decimal(output_tokens) * price.output_per_mtok) / _MTOK
        return total.quantize(Decimal("0.000001"))
