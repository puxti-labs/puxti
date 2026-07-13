"""Shared LLM configuration and response helpers.

Single source of truth for the model ID and its pricing — every module that
calls the Anthropic API imports from here so a model change is a one-line edit.
"""

# Model used for all semantic reasoning — never in the read path
LLM_MODEL = "claude-sonnet-4-6"

# Pricing for LLM_MODEL (USD per million tokens)
INPUT_COST_PER_MTOK = 3.00
OUTPUT_COST_PER_MTOK = 15.00


def strip_markdown_fences(raw: str) -> str:
    """Return raw LLM output with a wrapping markdown code fence removed.

    The LLM occasionally wraps its JSON response in ```json ... ``` fences
    despite instructions not to. Callers parse the returned string with
    json.loads and handle errors according to their own fallback policy.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
