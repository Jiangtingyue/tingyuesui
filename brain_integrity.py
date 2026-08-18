"""脑完整性报告：记录实际发送、实际返回和被省略的能力。

报告只保存参数摘要与数量，不保存 API Key、完整聊天或附件正文。
"""
from __future__ import annotations

import importlib.metadata
import math
from collections import Counter
from typing import Any

from model_capabilities import ModelCapabilities


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except Exception:
        return None


def _content_stats(content: Any) -> tuple[int, Counter]:
    chars = 0
    kinds: Counter = Counter()
    if isinstance(content, str):
        return len(content), kinds
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "unknown")
            kinds[kind] += 1
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                chars += len(text)
        return chars, kinds
    return len(str(content or "")), kinds


def estimate_input(
    system: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    context_window: int | None = None,
) -> dict[str, Any]:
    chars = len(system or "")
    roles: Counter = Counter()
    block_types: Counter = Counter()
    for message in messages:
        roles[str(message.get("role") or "unknown")] += 1
        c, kinds = _content_stats(message.get("content"))
        chars += c
        block_types.update(kinds)
    tool_chars = 0
    for tool in tools or []:
        tool_chars += len(str(tool.get("name") or ""))
        tool_chars += len(str(tool.get("description") or ""))
        tool_chars += len(str(tool.get("input_schema") or ""))
    chars += tool_chars
    # 中英混合的保守近似；它只用于预警，不参与计费。
    estimated_tokens = max(1, math.ceil(chars / 2.7)) if chars else 0
    ratio = None
    if context_window:
        ratio = round(estimated_tokens / context_window, 4)
    return {
        "estimated_input_tokens": estimated_tokens,
        "estimation_method": "chars/2.7",
        "context_window": context_window,
        "context_usage_ratio": ratio,
        "message_count": len(messages),
        "roles": dict(roles),
        "content_blocks": dict(block_types),
        "tool_count": len(tools or []),
    }


def start_report(
    *,
    provider: str,
    model: str,
    profile: ModelCapabilities,
    requested_options: dict[str, Any],
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report = {
        "version": "1",
        "provider": provider,
        "requested_model": model,
        "actual_model": None,
        "api_family": profile.api_family,
        "adapter": None,
        "sdk": {
            "openai": _version("openai"),
            "anthropic": _version("anthropic"),
        },
        "capability_source": profile.source,
        "capabilities": profile.public_dict(),
        "requested_options": {
            k: requested_options.get(k)
            for k in (
                "reasoning_effort", "reasoning_context", "thinking_mode", "thinking_budget",
                "thinking_visibility", "verbosity", "max_output_tokens",
            ) if requested_options.get(k) not in (None, "")
        },
        "sent_options": {},
        "omitted_options": [],
        "input": estimate_input(
            system,
            messages,
            tools=tools,
            context_window=profile.context_window,
        ),
        "stream_events": {},
        "unknown_stream_events": [],
        "tool_calls": 0,
        "stop_reason": None,
        "usage": None,
        "warnings": [],
        "complete": False,
    }
    ratio = report["input"].get("context_usage_ratio")
    if ratio is not None and ratio >= 0.8:
        report["warnings"].append("估算上下文已超过模型窗口的 80%。")
    return report


def sent(report: dict[str, Any], key: str, value: Any) -> None:
    report.setdefault("sent_options", {})[key] = value


def omitted(report: dict[str, Any], key: str, reason: str) -> None:
    report.setdefault("omitted_options", []).append({"option": key, "reason": reason})


def record_event(report: dict[str, Any], event_type: str, *, known: bool = True) -> None:
    name = event_type or "unknown"
    events = report.setdefault("stream_events", {})
    events[name] = int(events.get(name, 0)) + 1
    if not known:
        unknown = report.setdefault("unknown_stream_events", [])
        if name not in unknown:
            unknown.append(name)


def finish(
    report: dict[str, Any],
    *,
    actual_model: str | None = None,
    stop_reason: Any = None,
    usage: dict[str, Any] | None = None,
    tool_calls: int | None = None,
) -> dict[str, Any]:
    if actual_model:
        report["actual_model"] = actual_model
    if stop_reason is not None:
        report["stop_reason"] = stop_reason
    if usage is not None:
        report["usage"] = {
            k: usage.get(k)
            for k in (
                "input_tokens", "output_tokens", "reasoning_tokens",
                "cache_read", "cache_creation", "estimated",
            ) if k in usage
        }
    if tool_calls is not None:
        report["tool_calls"] = int(tool_calls)
    if report.get("unknown_stream_events"):
        report.setdefault("warnings", []).append("收到尚未识别的上游流式事件，已保留事件类型供排查。")
    report["complete"] = True
    return report
