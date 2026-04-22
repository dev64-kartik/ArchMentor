"""Per-token pricing + model id for the brain client.

The model id and the rate table live in one module so swapping back to
Opus 4.6 (or forward to the next GA) is a one-line change — callers
import `BRAIN_MODEL` and `estimate_cost_usd`, nothing else.

As of 2026-04 (Jan 2026 knowledge cutoff + verified before M2 shipped),
Anthropic's public rate card for Opus 4.x:

    input:                $15.00 / 1M tokens
    output:               $75.00 / 1M tokens
    cache write (5m):     $18.75 / 1M tokens  (1.25x input)
    cache read:           $ 1.50 / 1M tokens  (0.10x input)

Re-verify at implementation time — Anthropic has adjusted rates
historically. If the rate table drifts, only this file needs changing.

Token accounting uses the plan's definition:

    tokens_input  = input_tokens + (cache_creation_input_tokens or 0)
                  + (cache_read_input_tokens or 0)

Cost is computed per-token-type so the delta between a cache-hit call
and a cache-miss call is visible in `brain_snapshots` rows — the whole
reason the plan calls out emitting both `cache_creation_*` and
`cache_read_*` usage counters on every call.
"""

from __future__ import annotations

from dataclasses import dataclass

# Keep the model id as a single module-level constant. Default is the
# Unbound provider-prefixed form (`anthropic/claude-opus-4-7`) because
# this repo ships against an Anthropic-API-compatible gateway (Unbound)
# that uses LiteLLM-style `provider/model` routing. Direct Anthropic
# callers can set `ARCHMENTOR_BRAIN_MODEL=claude-opus-4-7`; both forms
# are registered in `BRAIN_RATES` below.
BRAIN_MODEL = "anthropic/claude-opus-4-7"


@dataclass(frozen=True, slots=True)
class TokenRates:
    """USD per token for a given model. Stored per-token (not per-1M)
    so `estimate_cost_usd` stays a straight multiply-and-sum."""

    input_per_token: float
    output_per_token: float
    cache_write_per_token: float
    cache_read_per_token: float


# Opus 4.7 rates (same as Opus 4.x family; Anthropic has kept the family
# rate card stable across minor revisions). Stored as per-token floats
# to avoid a repeated /1_000_000 at every call site.
OPUS_4_7_RATES = TokenRates(
    input_per_token=15.0 / 1_000_000,
    output_per_token=75.0 / 1_000_000,
    cache_write_per_token=18.75 / 1_000_000,
    cache_read_per_token=1.50 / 1_000_000,
)


# Register both the provider-prefixed and bare Opus 4.7 ids so a snapshot
# captured against one gateway still prices correctly when replayed
# against another. Keep this list explicit rather than stripping the
# prefix at lookup time — a silent prefix strip would also mask a real
# typo in the configured model id.
BRAIN_RATES: dict[str, TokenRates] = {
    "anthropic/claude-opus-4-7": OPUS_4_7_RATES,
    "claude-opus-4-7": OPUS_4_7_RATES,
}


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    """Return the per-call cost in USD.

    Raises `KeyError` if the model id isn't in `BRAIN_RATES` — the
    alternative (silently returning 0) would hide a cost-guard bypass.

    `cache_creation_input_tokens` and `cache_read_input_tokens` are
    counted separately from `input_tokens` per Anthropic's billing
    convention: `input_tokens` is the non-cached portion of the
    request, while the two cache fields cover the cached prefix.
    Summing all three gives the true "tokens in" count; pricing each
    at its own rate gives the true "cost in."
    """
    rates = BRAIN_RATES[model]
    return (
        input_tokens * rates.input_per_token
        + output_tokens * rates.output_per_token
        + cache_creation_input_tokens * rates.cache_write_per_token
        + cache_read_input_tokens * rates.cache_read_per_token
    )
