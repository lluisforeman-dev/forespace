"""LLM cost estimation and call logging (§10).

Log every call: model, tokens in/out, cost, task, run, entity.
cost-per-entity-per-month is the primary economic viability metric.
"""
from __future__ import annotations
import time
from decimal import Decimal

# Cost per 1K tokens (input_rate, output_rate) in USD.
# Update as model pricing changes — these drive the cost ledger.
_RATES: dict[str, tuple[float, float]] = {
    'meta-llama/llama-3.3-70b-instruct': (0.00059, 0.00079),
    'openai/gpt-4o-mini': (0.00015, 0.0006),
    'openai/gpt-4o': (0.0025, 0.01),
    'openai/gpt-5.6-luna': (0.003, 0.015),
    'anthropic/claude-3-haiku': (0.00025, 0.00125),
    'anthropic/claude-3.5-sonnet': (0.003, 0.015),
    'perplexity/llama-3.1-sonar-small-128k-online': (0.0002, 0.0002),
    'perplexity/llama-3.1-sonar-large-128k-online': (0.001, 0.001),
}
_DEFAULT = (0.001, 0.003)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    in_rate, out_rate = _RATES.get(model, _DEFAULT)
    return Decimal(str(round(
        tokens_in / 1000 * in_rate + tokens_out / 1000 * out_rate,
        6,
    )))


def log_call(
    task: str,
    model: str,
    response,
    run=None,
    entity=None,
    duration_ms: int | None = None,
) -> None:
    """Record an LLM call in the llm_call table. Never raises — must not break the pipeline."""
    try:
        from core.models import LLMCall
        usage = response.usage
        LLMCall.objects.create(
            run=run,
            entity=entity,
            task=task,
            model=model,
            tokens_in=usage.prompt_tokens,
            tokens_out=usage.completion_tokens,
            cost_usd=estimate_cost(model, usage.prompt_tokens, usage.completion_tokens),
            duration_ms=duration_ms,
        )
    except Exception:
        pass
