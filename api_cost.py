"""Cost controls for hidden, mechanical model work.

The user's selected conversation model is never changed here.  Optional helper
jobs use one explicitly configured DeepSeek Flash lane; unavailable helpers are
skipped instead of falling back to the companion model.  Identity-bearing work
(emotion, stance, intimacy, relationship voice) is deliberately rejected here.
"""
from __future__ import annotations

import re
from typing import Any

_LONGFORM_CUES = re.compile(
    r"(?:详细|展开|继续|完整|全部|逐条|一步一步|深度|分析|解释|教程|方案|报告|"
    r"代码|编程|debug|调试|重构|写一|写个|生成|总结|翻译|长文|小说|剧本|论文|"
    r"尽量多|说多一点|多说|不要省略|别省略)",
    re.I,
)
_TINY_ACK = re.compile(
    r"^(?:嗯+|哦+|噢+|好+|好的|行|可以|收到|知道了|哈哈+|嘿嘿+|呵呵+|"
    r"哥哥+|宝宝+|亲亲|抱抱|🥹+|🥺+|😭+|😂+|💕+|❤️+|❤+|[~～。！!？?…]+)$",
    re.I,
)


MECHANICAL_AUXILIARY_PURPOSES = frozenset({
    "memory_chunk",
    "memory_tag",
    "memory_dehydrate",
    "memory_rerank",
    "memory_contradiction",
    "memory_merge",
    "voice_translation",
})


def resolve_auxiliary_route() -> tuple[str, str] | None:
    """Return the configured DeepSeek Flash route without touching chat state."""
    import config

    # Helpers can be launched by background workers with no preceding HTTP
    # settings request.  Pick up a newly saved/cleared DeepSeek key here too.
    config.reload_provider_credentials()
    cfg = config.COST_OPTIMIZATION_CONFIG
    if not cfg.get("enabled", True) or not cfg.get("auxiliary_enabled", True):
        return None
    conf = config.PROVIDERS.get("deepseek") or {}
    if not conf.get("api_key"):
        return None
    # Do not honor stale GLM/OpenRouter/provider/model overrides here. Mechanical
    # work has exactly one cheap route and otherwise falls back to local rules.
    return "deepseek", "deepseek-v4-flash"


async def auxiliary_chat(
    *,
    messages: list[dict[str, Any]],
    purpose: str,
    system_prompt: str | None = None,
    session_id: str | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Run allow-listed mechanical work on DeepSeek without a companion persona."""
    import config
    from gateway import gateway

    if str(purpose or "") not in MECHANICAL_AUXILIARY_PURPOSES:
        return None
    route = resolve_auxiliary_route()
    if not route:
        return None
    provider, model = route
    cap = int(
        max_output_tokens
        or config.COST_OPTIMIZATION_CONFIG.get("auxiliary_max_output_tokens", 640)
        or 640
    )
    result = await gateway.chat(
        messages=messages,
        provider=provider,
        model=model,
        stream=False,
        system_prompt=system_prompt,
        memory_context=None,
        options={
            "max_output_tokens": max(64, min(cap, 16384)),
            "thinking_visibility": "hidden",
            "thinking_mode": "disabled",
            "reasoning_effort": "low",
        },
        session_id=session_id,
        include_default_system=False,
        purpose=purpose,
    )
    if not isinstance(result, dict):
        return None
    result["_auxiliary_route"] = {"provider": provider, "model": model}
    return result


def apply_runaway_output_guard(options: dict[str, Any] | None, user_text: str) -> dict[str, Any]:
    """Only cap obvious tiny acknowledgements; requested long work is untouched."""
    import config

    result = dict(options or {})
    cfg = config.COST_OPTIMIZATION_CONFIG
    if not cfg.get("enabled", True) or not cfg.get("runaway_output_guard", True):
        return result
    text = re.sub(r"\s+", "", str(user_text or "").strip())
    if not text or len(text) > 24 or _LONGFORM_CUES.search(text):
        return result
    if _TINY_ACK.fullmatch(text):
        cap = int(cfg.get("tiny_turn_output_cap", 4096) or 4096)
        try:
            current = int(result.get("max_output_tokens") or cap)
        except (TypeError, ValueError):
            current = cap
        result["max_output_tokens"] = min(max(64, current), cap)
    return result


def public_auxiliary_status() -> dict[str, Any]:
    route = resolve_auxiliary_route()
    return {
        "enabled": bool(route),
        "provider": route[0] if route else "",
        "model": route[1] if route else "",
    }
