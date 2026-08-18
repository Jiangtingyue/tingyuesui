"""Unified provider-facing context budget allocation.

The cache-stable raw history lane is never shrunk because one turn happens to
contain a large attachment/page.  A deterministic per-model history ceiling is
chosen first; repeated/current auxiliary material receives only the remaining
space.  This keeps the 7.9.9 stable history anchor intact while preventing
independent 36K/28K/30K side budgets from overflowing the model window.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import config
from model_capabilities import get_model_capabilities
from models import estimate_context_tokens_from_lengths


@dataclass(frozen=True)
class MainContextBudget:
    context_window_tokens: int
    output_reserve_tokens: int
    safety_reserve_tokens: int
    fixed_input_tokens: int
    current_message_tokens: int
    history_max_tokens: int
    extra_tokens: int
    explicit_max_chars: int
    web_max_chars: int
    retrieval_max_chars: int
    pinned_index_max_chars: int

    def public(self) -> dict[str, int]:
        return asdict(self)


def _fallback_window(provider: str, protocol: str) -> int:
    p = str(provider or "").lower()
    proto = str(protocol or "").lower()
    if p == "deepseek":
        return 1_000_000
    if proto == "anthropic" or p == "claude_code_p":
        return 200_000
    return 128_000


def _default_output_reserve(protocol: str) -> int:
    # Anthropic's adapter defaults to 16K. Other lanes that omit an explicit
    # output cap keep a smaller conservative reserve instead of pretending the
    # entire provider-advertised maximum is always requested.
    return 16_000 if str(protocol or "").lower() == "anthropic" else 8_192


def _chars_for_tokens(tokens: int) -> int:
    # Two chars/token is intentionally conservative across mixed CJK/source
    # text, while the final diagnostics still use the project's richer token
    # estimator.  The auxiliary budgets are ceilings, not billing estimates.
    return max(0, int(tokens) * 2)


def plan_main_context(
    *,
    provider: str,
    model: str,
    provider_conf: dict[str, Any],
    options: dict[str, Any] | None,
    current_message: str,
    stable_style_chars: int = 0,
    has_explicit: bool = False,
    has_web: bool = False,
    has_retrieval: bool = False,
    has_pinned: bool = False,
) -> MainContextBudget:
    options = options or {}
    profile = get_model_capabilities(provider, model, provider_conf)
    protocol = str(provider_conf.get("protocol") or profile.api_family or "")
    window = int(profile.context_window or _fallback_window(provider, protocol))
    window = max(32_000, window)

    try:
        requested_output = int(options.get("max_output_tokens") or 0)
    except (TypeError, ValueError):
        requested_output = 0
    output = requested_output if requested_output > 0 else _default_output_reserve(protocol)
    if profile.max_output_tokens:
        output = min(output, int(profile.max_output_tokens))
    # Leave some input room even when a UI asks for an extreme output ceiling.
    output = max(1_024, min(output, max(4_096, window // 2)))

    safety = max(2_048, min(8_192, window // 32))
    system_chars = sum(len(str(value or "")) for value in config.SYSTEM_PROMPT_BLOCKS.values())
    fixed_chars = (
        system_chars
        + max(0, int(stable_style_chars or 0))
        + int(config.CONTEXT_CONFIG.get("dynamic_max_chars", 1500) or 1500)
        + 4_000  # conditional persona/rhythm/framing reserve
    )
    # The stable history ceiling must not vary because one turn contains a big
    # attachment/webpage/current message.  Only model/output/fixed prompt shape
    # may determine it; per-turn material consumes the auxiliary remainder.
    fixed_tokens = int(fixed_chars * 1.15) + 8
    current_text = str(current_message or "")
    current_tokens = estimate_context_tokens_from_lengths(
        len(current_text), len(current_text.encode("utf-8", "ignore"))
    )

    configured_history = int(config.CONTEXT_COMPRESSION_CONFIG.get("raw_max_tokens", 158000) or 158000)
    hard_history = max(4_000, window - output - safety - fixed_tokens)
    history_max = min(configured_history, hard_history)

    extra_tokens = max(0, window - output - safety - fixed_tokens - history_max)
    auxiliary_tokens = max(0, extra_tokens - current_tokens)
    remaining_chars = _chars_for_tokens(auxiliary_tokens)

    file_conf = config.FILE_CONTEXT_CONFIG
    explicit_cap = int(file_conf.get("explicit_max_chars", 36000) or 36000)
    web_cap = int(getattr(config, "WEB_CONTEXT_CONFIG", {}).get("current_max_chars", 30000) or 30000)
    retrieval_cap = int(file_conf.get("retrieval_max_chars", 10000) or 10000)
    pinned_cap = int(file_conf.get("pinned_index_max_chars", 6000) or 6000)

    # One-turn material wins; repeated workspace material is reduced first.
    explicit = web = 0
    current_kinds = int(bool(has_explicit)) + int(bool(has_web))
    if current_kinds == 2:
        share = remaining_chars // 2
        explicit = min(explicit_cap, share)
        web = min(web_cap, share)
        leftover = remaining_chars - explicit - web
        if leftover > 0 and explicit < explicit_cap:
            take = min(leftover, explicit_cap - explicit)
            explicit += take
            leftover -= take
        if leftover > 0 and web < web_cap:
            web += min(leftover, web_cap - web)
        remaining_chars = max(0, remaining_chars - explicit - web)
    elif has_explicit:
        explicit = min(explicit_cap, remaining_chars)
        remaining_chars -= explicit
    elif has_web:
        web = min(web_cap, remaining_chars)
        remaining_chars -= web
    retrieval = min(retrieval_cap, remaining_chars) if has_retrieval else 0
    remaining_chars -= retrieval
    pinned = min(pinned_cap, remaining_chars) if has_pinned else 0

    return MainContextBudget(
        context_window_tokens=window,
        output_reserve_tokens=output,
        safety_reserve_tokens=safety,
        fixed_input_tokens=fixed_tokens,
        current_message_tokens=current_tokens,
        history_max_tokens=history_max,
        extra_tokens=extra_tokens,
        explicit_max_chars=max(0, explicit),
        web_max_chars=max(0, web),
        retrieval_max_chars=max(0, retrieval),
        pinned_index_max_chars=max(0, pinned),
    )


PROACTIVE_TOTAL_TOKENS = 20_000
PROACTIVE_RECENT_HISTORY_TOKENS = 9_000
PROACTIVE_RECENT_HISTORY_CHARS = 36_000
PROACTIVE_DYNAMIC_CHARS = 7_000
PROACTIVE_PINNED_INDEX_CHARS = 3_000
