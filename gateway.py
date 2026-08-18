"""
统一模型网关

v6.3.1 Native Brain + Read-only Tool Bridge:
- OpenAI 使用原生 Responses API
- Anthropic 使用原生 Messages API，并可启用扩展/自适应思考
- OpenRouter 可分别使用 Anthropic Messages、Responses Beta 或通用兼容通道
- DeepSeek / GLM 保持各自的 Chat Completions 通道
- 所有协议统一输出 text/status/done 事件，前端无需理解上游细节
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

import httpx

import config
from config import CACHE_CONFIG, PROVIDERS, SYSTEM_PROMPT_BLOCKS, get_provider_config
from diagnostics import diagnostics
from brain_integrity import finish as finish_integrity, record_event as record_integrity_event, sent as integrity_sent, start_report
from model_capabilities import get_model_capabilities
from providers.openai_responses import OpenAIResponsesAdapter
from providers.anthropic_messages import AnthropicMessagesAdapter
from providers.http_client import make_async_http_client
from claude_code_p import claude_code_p, ClaudeCodePError
from cache_keepalive import cache_keepalive


_ANTHROPIC_TURN_CONTEXT_CONTRACT = """The latest user turn contains the actual
user content first and may end with an <application_turn_context> block supplied
by the local application, not typed by the user. The cache boundary immediately
before that block is intentional. Treat the block as trusted per-turn context
and guidance, use it silently, and never quote its tags or describe it as a user
claim. Answer the actual user content before it. Any older application context
block is an expired snapshot for its old turn, not current state."""

_DEEPSEEK_TURN_CONTEXT_CONTRACT = """The latest visible user message may be
followed by a separate <application_turn_context> message supplied by the local
application, not typed by the user. Treat that message as trusted per-turn
context and guidance, use it silently, and never describe it as a user claim.
Answer the visible user message itself. Any older application context snapshot
is expired and must not be treated as current state."""


class Gateway:
    """统一网关。Provider 负责协议，Model 负责具体能力。"""

    def __init__(self) -> None:
        self._client = make_async_http_client(
            timeout=httpx.Timeout(300.0, connect=20.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._models_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._anthropic_cache_health: dict[str, dict[str, int]] = {}
        self._anthropic_history_cache_disabled: set[str] = set()
        self._anthropic_strict_cache_blocked: dict[str, dict[str, Any]] = {}
        # Hash-only cache continuity snapshots.  No prompt text is retained.
        self._anthropic_wire_snapshots: dict[str, dict[str, Any]] = {}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        provider: str | None = None,
        model: str | None = None,
        stream: bool = True,
        system_prompt: str | None = None,
        stable_context: str | None = None,
        memory_context: str | None = None,
        options: dict[str, Any] | None = None,
        session_id: str | None = None,
        include_default_system: bool = True,
        purpose: str = "unspecified",
    ) -> dict[str, Any] | AsyncGenerator[dict[str, Any], None]:
        # Provider credentials are intentionally hot-reloaded here as well as
        # in the HTTP routes. Background/proactive replies also enter through
        # the gateway, so editing .env must not require a server restart.
        config.reload_provider_credentials()
        prov = provider or config.ACTIVE_PROVIDER
        conf = get_provider_config(prov)
        protocol = conf["protocol"]
        if protocol != "claude_code_p" and not conf.get("api_key"):
            raise RuntimeError(f"{prov} API Key 未配置")
        if protocol == "claude_code_p" and not claude_code_p.available():
            raise ClaudeCodePError(
                "未找到 Claude Code CLI。请先安装 Claude Code 并运行 claude auth login。",
                code="claude_code_cli_missing",
            )

        mdl = config.cache_stable_model(prov, model or config.get_active_model(prov))
        request_options = self._merge_options(conf, options)

        if protocol == "claude_code_p":
            stable_system = self._build_system_prompt(
                (
                    system_prompt
                    if not include_default_system
                    else stable_context
                ),
                None,
                include_defaults=include_default_system,
            )
            try:
                response = await claude_code_p.request(
                    messages=messages,
                    model=mdl,
                    stable_system_prompt=stable_system,
                    memory_context=memory_context,
                    turn_instructions=(system_prompt if include_default_system else None),
                    options=request_options,
                    local_session_id=session_id,
                    stream=stream,
                    purpose=purpose,
                )
            except ClaudeCodePError as exc:
                if exc.usage:
                    self._record_gateway_usage(
                        session_id=session_id,
                        provider=prov,
                        model=mdl,
                        purpose=purpose,
                        usage=exc.usage,
                    )
                raise
            return self._track_gateway_response(
                response, session_id=session_id, provider=prov, model=mdl, purpose=purpose
            )

        if protocol == "anthropic":
            transport_session_id = self._anthropic_transport_session_id(
                session_id, purpose
            )
            if include_default_system and CACHE_CONFIG.get("enable_turn_context_tail", True):
                full_system = self._build_system_prompt(
                    stable_context, include_defaults=True
                )
                messages = self._attach_anthropic_turn_context(
                    messages,
                    memory_context=memory_context,
                    turn_instructions=system_prompt,
                )
                tail_context = True
                history_cache_allowed = True
            else:
                full_system = self._build_system_prompt(
                    "\n\n".join(
                        part for part in (stable_context, system_prompt) if part
                    ) or None,
                    memory_context,
                    include_defaults=include_default_system,
                )
                tail_context = False
                history_cache_allowed = include_default_system
            response = await self._anthropic_request(
                conf, mdl, full_system, messages, stream, request_options,
                tail_context=tail_context,
                history_cache_allowed=history_cache_allowed,
                session_id=transport_session_id,
                purpose=purpose,
            )
            return self._track_gateway_response(
                response, session_id=session_id, provider=prov, model=mdl, purpose=purpose
            )
        if (
            protocol == "deepseek_chat"
            and include_default_system
            and CACHE_CONFIG.get("enable_deepseek_prefix_cache", True)
        ):
            # DeepSeek's disk cache is automatic and matches the longest common
            # prefix from token 0.  Keep the reusable system/history byte-stable
            # and move volatile memory/state into a separate final app-context
            # message.  Claude's explicit cache_control path above is untouched.
            full_system = self._build_deepseek_cache_system(stable_context)
            messages = self._attach_deepseek_turn_context(
                messages,
                memory_context=memory_context,
                turn_instructions=system_prompt,
            )
        else:
            full_system = self._build_system_prompt(
                "\n\n".join(
                    part for part in (stable_context, system_prompt) if part
                ) or None,
                memory_context,
                include_defaults=include_default_system,
            )
        if protocol == "openai_responses":
            response = await self._openai_responses_request(
                conf, mdl, full_system, messages, stream, request_options
            )
        else:
            response = await self._openai_compat_request(
                conf, mdl, full_system, messages, stream, request_options
            )
        return self._track_gateway_response(
            response, session_id=session_id, provider=prov, model=mdl, purpose=purpose
        )

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[Any]],
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        stable_context: str | None = None,
        memory_context: str | None = None,
        options: dict[str, Any] | None = None,
        session_id: str | None = None,
        purpose: str = "tool_loop",
        max_rounds: int = 4,
        tool_observer: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """执行只读工具循环，并返回最终文本。

        只在用户明确请求诊断/代码检查时调用，因此这里优先可靠性，
        使用非流式多轮工具协议。普通聊天仍走原来的流式路径。
        """
        config.reload_provider_credentials()
        prov = provider or config.ACTIVE_PROVIDER
        conf = get_provider_config(prov)
        protocol = conf["protocol"]
        if protocol == "claude_code_p":
            raise RuntimeError("Claude Code P 模式不使用 Gateway API tool loop；请走 P 模式自己的工具/MCP或本机诊断预取")
        if not conf.get("api_key"):
            raise RuntimeError(f"{prov} API Key 未配置")
        mdl = config.cache_stable_model(prov, model or config.get_active_model(prov))
        request_options = self._merge_options(conf, options)

        if protocol == "anthropic":
            transport_session_id = self._anthropic_transport_session_id(
                session_id, purpose
            )
            if CACHE_CONFIG.get("enable_turn_context_tail", True):
                full_system = self._build_system_prompt(
                    stable_context, include_defaults=True
                )
                messages = self._attach_anthropic_turn_context(
                    messages,
                    memory_context=memory_context,
                    turn_instructions=system_prompt,
                )
                tail_context = True
            else:
                full_system = self._build_system_prompt(
                    "\n\n".join(
                        part for part in (stable_context, system_prompt) if part
                    ) or None,
                    memory_context,
                )
                tail_context = False
            result = await self._anthropic_tool_loop(
                conf, mdl, full_system, messages, request_options,
                tools, tool_executor, max_rounds, tool_observer,
                tail_context=tail_context,
                session_id=transport_session_id,
                purpose=purpose,
            )
            self._record_gateway_usage(
                session_id=session_id, provider=prov, model=mdl, purpose=purpose,
                usage=result.get("usage") if isinstance(result, dict) else None,
            )
            return result

        if (
            protocol == "deepseek_chat"
            and CACHE_CONFIG.get("enable_deepseek_prefix_cache", True)
        ):
            full_system = self._build_deepseek_cache_system(stable_context)
            messages = self._attach_deepseek_turn_context(
                messages,
                memory_context=memory_context,
                turn_instructions=system_prompt,
            )
        else:
            full_system = self._build_system_prompt(
                "\n\n".join(
                    part for part in (stable_context, system_prompt) if part
                ) or None,
                memory_context,
            )
        if protocol == "openai_responses":
            result = await self._openai_responses_tool_loop(
                conf, mdl, full_system, messages, request_options,
                tools, tool_executor, max_rounds, tool_observer,
            )
        else:
            result = await self._openai_compat_tool_loop(
                conf, mdl, full_system, messages, request_options,
                tools, tool_executor, max_rounds, tool_observer,
            )
        self._record_gateway_usage(
            session_id=session_id, provider=prov, model=mdl, purpose=purpose,
            usage=result.get("usage") if isinstance(result, dict) else None,
        )
        return result

    def _record_gateway_usage(
        self,
        *,
        session_id: str | None,
        provider: str,
        model: str,
        purpose: str,
        usage: dict[str, Any] | None,
    ) -> None:
        if not isinstance(usage, dict):
            return
        try:
            from models import record_api_call
            record_api_call(
                session_id=session_id, provider=provider, model=model,
                purpose=purpose, usage=usage,
            )
        except Exception as exc:
            diagnostics.record_error(
                "api_call_usage_audit", exc,
                metadata={"provider": provider, "model": model, "purpose": purpose},
            )

    async def _audit_gateway_stream(
        self,
        stream: AsyncGenerator[dict[str, Any], None],
        *,
        session_id: str | None,
        provider: str,
        model: str,
        purpose: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        recorded = False
        try:
            async for chunk in stream:
                if (not recorded and isinstance(chunk, dict)
                        and chunk.get("type") == "done"
                        and isinstance(chunk.get("usage"), dict)):
                    self._record_gateway_usage(
                        session_id=session_id, provider=provider, model=model,
                        purpose=purpose, usage=chunk.get("usage"),
                    )
                    recorded = True
                yield chunk
        except ClaudeCodePError as exc:
            if not recorded and exc.usage:
                self._record_gateway_usage(
                    session_id=session_id,
                    provider=provider,
                    model=model,
                    purpose=purpose,
                    usage=exc.usage,
                )
            raise

    def _track_gateway_response(
        self,
        response: dict[str, Any] | AsyncGenerator[dict[str, Any], None],
        *,
        session_id: str | None,
        provider: str,
        model: str,
        purpose: str,
    ) -> dict[str, Any] | AsyncGenerator[dict[str, Any], None]:
        if hasattr(response, "__aiter__"):
            return self._audit_gateway_stream(
                response, session_id=session_id, provider=provider, model=model,
                purpose=purpose,
            )
        if isinstance(response, dict):
            self._record_gateway_usage(
                session_id=session_id,
                provider=str(response.get("provider") or provider),
                model=str(response.get("model") or model),
                purpose=purpose, usage=response.get("usage"),
            )
        return response

    async def _notify_tool_observer(
        self,
        observer: Callable[[dict[str, Any]], Any] | None,
        event: dict[str, Any],
    ) -> None:
        if observer is None:
            return
        value = observer(event)
        if hasattr(value, "__await__"):
            await value

    @staticmethod
    def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            parsed = json.loads(str(raw))
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {"_raw": str(raw)}

    @staticmethod
    def _json_tool_output(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _add_usage(total: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read", "cache_creation", "cache_creation_1h",
            "cache_creation_5m",
        ):
            total[key] = int(total.get(key, 0) or 0) + int(item.get(key, 0) or 0)
        total["upstream_requests"] = (
            int(total.get("upstream_requests", 0) or 0)
            + max(1, int(item.get("upstream_requests", 1) or 1))
        )
        total["cost"] = round(float(total.get("cost", 0) or 0) + float(item.get("cost", 0) or 0), 6)
        total["saved"] = round(float(total.get("saved", 0) or 0) + float(item.get("saved", 0) or 0), 6)
        if item.get("price_snapshot"):
            total["price_snapshot"] = item["price_snapshot"]
        item_source = str(item.get("cost_source") or "unavailable")
        total_source = str(total.get("cost_source") or "")
        total["cost_source"] = (
            item_source
            if not total_source or total_source == item_source
            else "mixed"
        )
        # For multi-round tool calls, keep the newest route/generation while
        # preserving the stable cache-prefix diagnostics from each round.
        for key in (
            "generation_id", "actual_provider", "actual_model",
            "router_region", "router_strategy", "cache_prefix_hash",
            "cache_parent_hash", "cache_shape_hash", "cache_ttl", "cache_breakpoint",
            "history_cache_guard", "cache_guard_status", "cache_strategy",
        ):
            if item.get(key) not in (None, ""):
                total[key] = item[key]
        for key in (
            "cache_prefix_chars", "cache_prefix_tokens_estimate",
            "cache_min_tokens",
        ):
            if item.get(key) is not None:
                total[key] = int(item.get(key) or 0)
        return total

    @staticmethod
    def _resolve_usage_cost(
        usage: dict[str, Any],
        pricing: dict[str, Any],
        calculated_cost: float,
    ) -> tuple[float, str]:
        """Prefer provider-accounted spend and label every fallback honestly.

        OpenRouter exposes ``usage.cost`` even when the local per-model price
        table is intentionally empty.  First-party APIs generally expose only
        tokens, so their configured snapshot remains a clearly labelled local
        estimate.  A numeric upstream zero is meaningful (for example a free
        route) and must not be mistaken for missing data.
        """
        raw_cost = usage.get("cost")
        try:
            upstream_cost = float(raw_cost)
        except (TypeError, ValueError):
            upstream_cost = math.nan
        if math.isfinite(upstream_cost) and upstream_cost >= 0:
            return round(upstream_cost, 8), "upstream_exact"

        pricing_known = False
        for key in ("input", "output", "cache_read", "cache_write"):
            try:
                pricing_known = pricing_known or float(pricing.get(key, 0) or 0) > 0
            except (TypeError, ValueError):
                continue
        if pricing_known:
            return round(max(0.0, float(calculated_cost or 0)), 8), "local_estimate"
        return 0.0, "unavailable"

    def _openai_tool_defs(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            }
            for tool in tools
        ]

    def _compat_tool_defs(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for tool in tools
        ]

    async def _openai_responses_tool_loop(
        self,
        conf: dict[str, Any],
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[Any]],
        max_rounds: int,
        observer: Callable[[dict[str, Any]], Any] | None,
    ) -> dict[str, Any]:
        provider_name = self._provider_name_from_conf(conf)
        profile = get_model_capabilities(provider_name, model, conf)
        report = start_report(
            provider=provider_name, model=model, profile=profile,
            requested_options=options, system=system, messages=messages, tools=tools,
        )
        adapter = OpenAIResponsesAdapter(conf)
        kwargs = adapter.build_kwargs(
            model=model,
            instructions=system,
            input_items=self._build_openai_responses_input(messages),
            options=options,
            profile=profile,
            report=report,
            tools=self._openai_tool_defs(tools),
        )

        async def notify(item: dict[str, Any]) -> None:
            await self._notify_tool_observer(observer, item)

        return await adapter.tool_loop(
            kwargs=kwargs,
            report=report,
            tool_executor=tool_executor,
            max_rounds=max_rounds,
            observer=notify,
            usage_parser=lambda usage: self._calc_cost_openai_responses(usage, conf),
            add_usage=self._add_usage,
        )

    async def _anthropic_tool_loop(
        self,
        conf: dict[str, Any],
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[Any]],
        max_rounds: int,
        observer: Callable[[dict[str, Any]], Any] | None,
        *,
        tail_context: bool = False,
        session_id: str | None = None,
        purpose: str = "tool_loop",
    ) -> dict[str, Any]:
        provider_name = self._provider_name_from_conf(conf)
        cache_key = self._anthropic_cache_key(provider_name, model, session_id)
        # Keep an unmarked canonical transcript inside the loop. Cache markers
        # are request metadata and must be recomputed after every appended
        # tool_result rather than accumulating or staying stuck on round one.
        normalized_messages = self._normalize_anthropic_messages(messages)
        # Tool declaration order is part of the Anthropic prompt prefix.  Keep
        # it deterministic so an equivalent tool set never creates a new cache
        # key merely because registry enumeration order changed.
        tools = self._apply_stable_tool_schema_cache(tools)
        profile = get_model_capabilities(provider_name, model, conf)
        report = start_report(
            provider=provider_name, model=model, profile=profile,
            requested_options=options, system=system, messages=normalized_messages, tools=tools,
        )
        adapter = AnthropicMessagesAdapter(conf)
        kwargs = adapter.build_kwargs(
            model=model,
            system=self._build_anthropic_system_with_cache(
                system, tail_context=tail_context
            ),
            messages=normalized_messages,
            options=options,
            profile=profile,
            report=report,
            tools=tools,
        )
        self._attach_openrouter_session_id(
            kwargs, provider_name, session_id, report
        )
        self._reserve_anthropic_history_marker_budget(
            kwargs, reserve_history_markers=2, max_markers=4
        )

        def prepare_round_messages(
            round_messages: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            prepared = self._apply_history_cache(
                round_messages, cache_key=cache_key
            )
            prepared = self._strip_internal_message_fields(prepared)
            diagnostic_kwargs = {**kwargs, "messages": prepared}
            report["cache_diagnostics"] = self._anthropic_cache_diagnostics(
                diagnostic_kwargs, model, cache_key=cache_key
            )
            return prepared

        async def notify(item: dict[str, Any]) -> None:
            await self._notify_tool_observer(observer, item)

        return await adapter.tool_loop(
            kwargs=kwargs,
            report=report,
            tool_executor=tool_executor,
            max_rounds=max_rounds,
            observer=notify,
            usage_parser=lambda usage: self._finalize_anthropic_usage(
                usage, conf=conf, model=model, session_id=session_id, purpose=purpose,
                report=report, cache_key=cache_key, kwargs=kwargs
            ),
            add_usage=self._add_usage,
            message_preparer=prepare_round_messages,
        )

    def _apply_stable_tool_schema_cache(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cache a deterministic stable tool schema on the dedicated tool lane.

        Mirrors Anthropic's documented last-tool breakpoint pattern. Forced-tool
        follow-ups/media requests never call this helper.
        """
        if not tools:
            return []
        stable = sorted((copy.deepcopy(t) for t in tools), key=lambda x: str(x.get("name") or ""))
        stable[-1]["cache_control"] = self._anthropic_cache_control()
        return stable

    @staticmethod
    def _marker_count_in_blocks(value: Any) -> int:
        if isinstance(value, dict):
            return (1 if isinstance(value.get("cache_control"), dict) else 0) + sum(
                Gateway._marker_count_in_blocks(v) for k, v in value.items() if k != "cache_control"
            )
        if isinstance(value, list):
            return sum(Gateway._marker_count_in_blocks(v) for v in value)
        return 0

    def _reserve_anthropic_history_marker_budget(
        self, kwargs: dict[str, Any], *, reserve_history_markers: int = 2, max_markers: int = 4
    ) -> dict[str, Any]:
        """Keep Anthropic explicit cache markers within the normal four-marker budget.

        Priority is: fixed system marker + stable tool-schema marker + two rolling
        history markers.  A redundant second system marker is removed first because
        a later history breakpoint already caches the entire preceding system prefix.
        """
        static_budget = max(0, int(max_markers) - max(0, int(reserve_history_markers)))
        while self._marker_count_in_blocks(kwargs.get("system")) + self._marker_count_in_blocks(kwargs.get("tools")) > static_budget:
            system = kwargs.get("system")
            removed = False
            if isinstance(system, list):
                marked = [i for i, block in enumerate(system) if isinstance(block, dict) and isinstance(block.get("cache_control"), dict)]
                # Preserve the first/fixed system marker; later system markers are redundant.
                if len(marked) > 1:
                    system[marked[-1]].pop("cache_control", None)
                    removed = True
            if removed:
                continue
            # If a pathological configuration still exceeds the budget, drop the
            # tool marker before sacrificing the fixed system checkpoint.
            tools = kwargs.get("tools")
            if isinstance(tools, list):
                for tool in reversed(tools):
                    if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
                        tool.pop("cache_control", None)
                        removed = True
                        break
            if not removed:
                break
        return kwargs

    @staticmethod
    def _deepseek_cacheable_prefix(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return provider-visible messages before the volatile app tail."""
        result: list[dict[str, Any]] = []
        for message in messages:
            content = str(message.get("content") or "")
            # The stable system contract mentions the tag by name.  Only the
            # dedicated volatile tail *starts* with the tag; a substring match
            # would therefore collapse every diagnostic prefix to an empty list.
            if content.lstrip().startswith("<application_turn_context>"):
                break
            result.append(copy.deepcopy(message))
        return result

    def _compat_messages(
        self,
        system: str,
        messages: list[dict[str, Any]],
        provider_name: str,
    ) -> list[dict[str, Any]]:
        """把可见历史转换成 Chat Completions 历史。

        DeepSeek 只有在发生过工具调用时才需要跨轮重放隐藏推理；这类
        原生回合保存在 native_envelope 中，并且只会回给同一型号。
        """
        result: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg.get("role")
            if role not in {"user", "assistant", "tool"}:
                continue
            envelope = msg.get("native_envelope")
            if (
                provider_name == "deepseek"
                and role == "assistant"
                and isinstance(envelope, dict)
                and envelope.get("api_family") == "deepseek_chat_completions"
                and isinstance(envelope.get("messages"), list)
            ):
                result.extend(copy.deepcopy(envelope["messages"]))
                continue
            content = msg.get("content")
            result.append({
                "role": role,
                "content": content if isinstance(content, str) else self._flatten_content(content),
            })
        return result

    def _apply_compat_options(
        self,
        body: dict[str, Any],
        *,
        conf: dict[str, Any],
        model: str,
        options: dict[str, Any],
        profile: Any,
        report: dict[str, Any],
        stream: bool,
    ) -> None:
        max_output = self._safe_int(options.get("max_output_tokens"), 0)
        if max_output > 0:
            if profile.max_output_tokens:
                max_output = min(max_output, int(profile.max_output_tokens))
            param = conf.get("max_tokens_param", "max_tokens")
            body[param] = max_output
            integrity_sent(report, param, max_output)

        provider_name = profile.provider
        if provider_name == "openrouter":
            # OpenRouter normalizes provider-specific reasoning fields.  This
            # flag controls only whether an available trace is returned; it
            # does not manufacture reasoning for models that expose none.
            visibility = str(options.get("thinking_visibility") or "full").strip().lower()
            body["reasoning"] = {"exclude": visibility != "full"}
            integrity_sent(report, "reasoning.exclude", visibility != "full")
        if provider_name == "deepseek" and profile.reasoning_modes:
            requested_mode = str(
                options.get("thinking_mode") or profile.default_reasoning_mode or "enabled"
            ).strip().lower()
            mode_aliases = {"auto": "enabled", "on": "enabled", "off": "disabled"}
            requested_mode = mode_aliases.get(requested_mode, requested_mode)
            if requested_mode in profile.reasoning_modes:
                body["thinking"] = {"type": requested_mode}
                integrity_sent(report, "thinking", body["thinking"])
            else:
                from brain_integrity import omitted as integrity_omitted
                integrity_omitted(report, "thinking_mode", f"当前 DeepSeek 型号不支持 {requested_mode}。")

            requested_effort = str(
                options.get("reasoning_effort") or profile.default_reasoning_effort or "high"
            ).strip().lower()
            if requested_mode != "disabled":
                if requested_effort in profile.reasoning_efforts:
                    body["reasoning_effort"] = requested_effort
                    integrity_sent(report, "reasoning_effort", requested_effort)
                else:
                    from brain_integrity import omitted as integrity_omitted
                    integrity_omitted(report, "reasoning_effort", f"当前 DeepSeek 型号不支持 {requested_effort}。")

        if stream and (conf.get("include_stream_usage") or provider_name == "deepseek"):
            body["stream_options"] = {"include_usage": True}
            integrity_sent(report, "stream_options.include_usage", True)

    async def _openai_compat_tool_loop(
        self,
        conf: dict[str, Any],
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[Any]],
        max_rounds: int,
        observer: Callable[[dict[str, Any]], Any] | None,
    ) -> dict[str, Any]:
        url = self._join_url(conf["base_url"], conf.get("chat_path", "/v1/chat/completions"))
        headers = {"Authorization": f"Bearer {conf['api_key']}", "Content-Type": "application/json"}
        headers.update(conf.get("extra_headers", {}))
        provider_name = conf.get("provider_name") or self._provider_name_from_conf(conf)
        chat_messages = self._compat_messages(system, messages, provider_name)
        profile = get_model_capabilities(provider_name, model, conf)
        report = start_report(
            provider=provider_name,
            model=model,
            profile=profile,
            requested_options=options,
            system=system,
            messages=messages,
            tools=tools,
        )
        report["adapter"] = (
            "httpx/deepseek-chat-completions"
            if provider_name == "deepseek"
            else "httpx/chat-completions-compatible"
        )
        if provider_name == "deepseek":
            report["cache_strategy"] = "automatic_longest_common_prefix+ephemeral_turn_context+tools"
            report["cache_prefix_hash"] = self._hash_json({
                "model": model,
                "messages": self._deepseek_cacheable_prefix(chat_messages),
                "tools": self._compat_tool_defs(tools),
            })
        total_usage: dict[str, Any] = {}
        tool_log: list[dict[str, Any]] = []
        final_text = ""
        reasoning_parts: list[str] = []
        native_transcript: list[dict[str, Any]] = []
        used_tool_call = False
        show_thinking = str(options.get("thinking_visibility") or "full").strip().lower() == "full"
        actual_model = model
        stop_reason: Any = None

        for _ in range(max(1, min(max_rounds, 8))):
            body: dict[str, Any] = {
                "model": model,
                "messages": chat_messages,
                "tools": self._compat_tool_defs(tools),
                "tool_choice": "auto",
                "stream": False,
            }
            self._apply_compat_options(
                body,
                conf=conf,
                model=model,
                options=options,
                profile=profile,
                report=report,
                stream=False,
            )
            integrity_sent(report, "tools", len(tools))
            integrity_sent(report, "tool_choice", "auto")
            resp = await self._client.post(url, headers=headers, json=body)
            self._raise_upstream_error(resp, "兼容接口工具循环")
            data = resp.json()
            actual_model = data.get("model") or actual_model
            self._add_usage(total_usage, self._calc_cost_openai_compat(data.get("usage", {}), conf, actual_model))
            choices = data.get("choices", [])
            choice = choices[0] if choices else {}
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            stop_reason = choice.get("finish_reason") or stop_reason
            final_text = message.get("content") or ""
            reasoning_text = self._compat_reasoning_text(message)
            if show_thinking and reasoning_text:
                reasoning_parts.append(reasoning_text)
            calls = message.get("tool_calls") or []
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": final_text or None,
            }
            if message.get("reasoning_content") is not None:
                assistant_message["reasoning_content"] = message.get("reasoning_content")
            if message.get("reasoning") is not None:
                assistant_message["reasoning"] = message.get("reasoning")
            if message.get("reasoning_details") is not None:
                assistant_message["reasoning_details"] = copy.deepcopy(message.get("reasoning_details"))
            if calls:
                assistant_message["tool_calls"] = calls
            chat_messages.append(copy.deepcopy(assistant_message))
            native_transcript.append(copy.deepcopy(assistant_message))
            if not calls:
                total_usage["native_protocol"] = (
                    "deepseek_chat_completions+tools"
                    if provider_name == "deepseek"
                    else "chat_completions+tools"
                )
                total_usage["integrity"] = finish_integrity(
                    report,
                    actual_model=actual_model,
                    stop_reason=stop_reason or "completed",
                    usage=total_usage,
                    tool_calls=len(tool_log),
                )
                envelope = None
                if provider_name == "deepseek" and used_tool_call:
                    envelope = {
                        "api_family": "deepseek_chat_completions",
                        "response_id": data.get("id") or "",
                        "messages": native_transcript,
                    }
                return {
                    "content": final_text,
                    "reasoning_content": "\n\n".join(reasoning_parts),
                    "model": actual_model,
                    "provider": provider_name,
                    "usage": total_usage,
                    "tool_calls": tool_log,
                    "native_envelope": envelope,
                }
            used_tool_call = True
            for call in calls:
                fn = call.get("function", {}) or {}
                name = fn.get("name", "")
                args = self._parse_tool_arguments(fn.get("arguments"))
                started = time.perf_counter()
                ok = True
                try:
                    result = await tool_executor(name, args)
                except Exception as exc:
                    ok = False
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                log_item = {"name": name, "arguments": args, "result": result, "duration_ms": duration_ms, "ok": ok}
                tool_log.append(log_item)
                await self._notify_tool_observer(observer, log_item)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": self._json_tool_output(result),
                }
                chat_messages.append(copy.deepcopy(tool_message))
                native_transcript.append(copy.deepcopy(tool_message))

        total_usage["native_protocol"] = (
            "deepseek_chat_completions+tools"
            if provider_name == "deepseek"
            else "chat_completions+tools"
        )
        total_usage["integrity"] = finish_integrity(
            report,
            actual_model=actual_model,
            stop_reason=stop_reason or "max_tool_rounds",
            usage=total_usage,
            tool_calls=len(tool_log),
        )
        return {
            "content": final_text or "工具调用达到上限，我已经停止继续调用。",
            "reasoning_content": "\n\n".join(reasoning_parts),
            "model": model,
            "provider": provider_name,
            "usage": total_usage,
            "tool_calls": tool_log,
            "incomplete": True,
            "native_envelope": (
                {
                    "api_family": "deepseek_chat_completions",
                    "response_id": "",
                    "messages": native_transcript,
                }
                if provider_name == "deepseek" and used_tool_call else None
            ),
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # System prompt / request options
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_system_prompt(
        self,
        custom_prompt: str | None = None,
        memory_context: str | None = None,
        *,
        include_defaults: bool = True,
    ) -> str:
        parts: list[str] = []
        if include_defaults:
            for key, block in SYSTEM_PROMPT_BLOCKS.items():
                if block.strip():
                    parts.append(f"<{key}>\n{block}\n</{key}>")
        if memory_context:
            parts.append(f"<memory_context>\n{memory_context}\n</memory_context>")
        if custom_prompt:
            parts.append(custom_prompt)
        return "\n\n".join(parts)

    @staticmethod
    def _anthropic_turn_context_text(
        *,
        memory_context: str | None,
        turn_instructions: str | None,
    ) -> str:
        parts: list[str] = []
        if memory_context and memory_context.strip():
            parts.append(
                "<runtime_context>\n"
                f"{memory_context.strip()}\n"
                "</runtime_context>"
            )
        if turn_instructions and turn_instructions.strip():
            parts.append(
                "<turn_instructions>\n"
                f"{turn_instructions.strip()}\n"
                "</turn_instructions>"
            )
        if not parts:
            return ""
        return (
            "<application_turn_context>\n"
            + "\n\n".join(parts)
            + "\n</application_turn_context>"
        )

    @staticmethod
    def _deepseek_turn_context_text(
        *,
        memory_context: str | None,
        turn_instructions: str | None,
    ) -> str:
        # Keep the payload format identical to the Anthropic runtime tail, but
        # transport it as its own final message so the preceding visible user
        # message remains a byte-stable automatic-cache prefix.
        return Gateway._anthropic_turn_context_text(
            memory_context=memory_context,
            turn_instructions=turn_instructions,
        )

    def _build_deepseek_cache_system(self, stable_context: str | None) -> str:
        stable_tail = "\n\n".join(
            part for part in (
                stable_context,
                "<turn_context_contract>\n"
                + _DEEPSEEK_TURN_CONTEXT_CONTRACT
                + "\n</turn_context_contract>",
            )
            if part
        )
        return self._build_system_prompt(
            stable_tail or None,
            None,
            include_defaults=True,
        )

    def _attach_deepseek_turn_context(
        self,
        messages: list[dict[str, Any]],
        *,
        memory_context: str | None,
        turn_instructions: str | None,
    ) -> list[dict[str, Any]]:
        """Append volatile app context after the canonical visible user turn.

        DeepSeek caches matching prefixes automatically in fixed token blocks;
        it has no Anthropic-style explicit breakpoint.  A separate ephemeral
        final message therefore preserves the exact system/history/current-user
        prefix while keeping volatile memory out of the next turn's history.
        """
        context_text = self._deepseek_turn_context_text(
            memory_context=memory_context,
            turn_instructions=turn_instructions,
        )
        if not context_text:
            return messages
        result = copy.deepcopy(messages)
        result.append({
            "role": "user",
            "content": context_text,
            "_application_turn_context": True,
            "_cache_replay_stable": False,
        })
        return result

    def _attach_anthropic_turn_context(
        self,
        messages: list[dict[str, Any]],
        *,
        memory_context: str | None,
        turn_instructions: str | None,
    ) -> list[dict[str, Any]]:
        """Put volatile app context after the newest user's cacheable content.

        The visible user content becomes a stable checkpoint.  On the next turn
        that same user message is replayed without the old runtime tail, yet the
        prefix through the checkpoint is byte-identical.  Prepending the runtime
        block (the old design) made every previous user/assistant prefix differ
        and reduced a 100K+ conversation to little more than a system-cache hit.
        """
        context_text = self._anthropic_turn_context_text(
            memory_context=memory_context,
            turn_instructions=turn_instructions,
        )
        if not context_text:
            return messages
        result = copy.deepcopy(messages)
        user_index = next(
            (index for index in range(len(result) - 1, -1, -1)
             if result[index].get("role") == "user"),
            -1,
        )
        if user_index < 0:
            return messages
        message = result[user_index]
        context_block = {"type": "text", "text": context_text}
        content = message.get("content", "")
        if isinstance(content, str):
            message["content"] = [
                {"type": "text", "text": content},
                context_block,
            ]
        elif isinstance(content, list):
            message["content"] = [*content, context_block]
        else:
            message["content"] = [
                {"type": "text", "text": str(content or "")},
                context_block,
            ]
        return result

    @staticmethod
    def _merge_options(conf: dict[str, Any], options: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(conf.get("default_options", {}))
        if isinstance(options, dict):
            for key, value in options.items():
                if value is not None and value != "":
                    merged[key] = value
        return merged

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # OpenAI 原生 Responses API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _openai_responses_request(
        self,
        conf: dict[str, Any],
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        stream: bool,
        options: dict[str, Any],
    ) -> dict[str, Any] | AsyncGenerator[dict[str, Any], None]:
        """通过官方 OpenAI SDK 调用原生 Responses API。"""
        provider_name = self._provider_name_from_conf(conf)
        profile = get_model_capabilities(provider_name, model, conf)
        report = start_report(
            provider=provider_name,
            model=model,
            profile=profile,
            requested_options=options,
            system=system,
            messages=messages,
        )
        adapter = OpenAIResponsesAdapter(conf)
        kwargs = adapter.build_kwargs(
            model=model,
            instructions=system,
            input_items=self._build_openai_responses_input(messages),
            options=options,
            profile=profile,
            report=report,
        )
        if stream:
            return adapter.stream(
                kwargs=kwargs,
                report=report,
                usage_parser=lambda usage: self._calc_cost_openai_responses(usage, conf),
                fallback_usage=lambda text: self._fallback_usage(text, conf),
            )
        return await adapter.request(
            kwargs=kwargs,
            report=report,
            usage_parser=lambda usage: self._calc_cost_openai_responses(usage, conf),
        )

    def _build_openai_responses_input(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant", "developer", "system"}:
                continue
            envelope = message.get("native_envelope")
            if (role == "assistant" and isinstance(envelope, dict)
                    and envelope.get("api_family") == "responses"
                    and isinstance(envelope.get("output"), list)):
                native_items = [copy.deepcopy(item) for item in envelope["output"] if isinstance(item, dict)]
                if native_items:
                    result.extend(native_items)
                    continue
            content = message.get("content", "")
            # 字符串是最稳定的 EasyInputMessage 形式；用户多模态块则保留。
            if isinstance(content, str):
                normalized: Any = content
            elif role == "user":
                normalized = self._normalize_openai_user_content(content)
            else:
                normalized = self._flatten_content(content)
            result.append({"role": role, "content": normalized})
        return result

    def _normalize_openai_user_content(self, content: Any) -> str | list[dict[str, Any]]:
        if not isinstance(content, list):
            return str(content or "")
        blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "input_image", "input_file"}:
                blocks.append(dict(block))
            elif block_type == "text":
                blocks.append({"type": "input_text", "text": block.get("text", "")})
            elif block_type in {"image", "image_url"}:
                source = block.get("image_url") or block.get("url") or block.get("source")
                if isinstance(source, dict):
                    source = source.get("url") or source.get("data")
                if source:
                    blocks.append({"type": "input_image", "image_url": source})
            elif block_type in {"file", "document"}:
                file_id = block.get("file_id")
                file_url = block.get("file_url") or block.get("url")
                file_data = block.get("file_data")
                item: dict[str, Any] = {"type": "input_file"}
                if file_id:
                    item["file_id"] = file_id
                elif file_url:
                    item["file_url"] = file_url
                elif file_data:
                    item["file_data"] = file_data
                else:
                    continue
                if block.get("filename"):
                    item["filename"] = block["filename"]
                blocks.append(item)
        return blocks or self._flatten_content(content)

    async def _openai_responses_stream(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        conf: dict[str, Any],
        model: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        full_text = ""
        final_usage: dict[str, Any] | None = None
        yield {"type": "status", "stage": "connecting", "label": "连接 OpenAI 原生接口…"}

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                raw = await resp.aread()
                self._raise_upstream_error_bytes(resp.status_code, raw, "OpenAI Responses API")

            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")
                if event_type == "response.created":
                    yield {"type": "status", "stage": "reasoning", "label": "GPT 正在思考…"}
                elif event_type == "response.output_text.delta":
                    text = event.get("delta", "")
                    if text:
                        full_text += text
                        yield {"type": "text", "text": text}
                elif event_type == "response.completed":
                    response = event.get("response", {}) or {}
                    final_usage = self._calc_cost_openai_responses(response.get("usage", {}), conf)
                elif event_type in {"response.failed", "response.incomplete"}:
                    response = event.get("response", {}) or {}
                    error = response.get("error") or response.get("incomplete_details") or event
                    raise RuntimeError(f"OpenAI 响应未完成: {error}")
                elif event_type == "error":
                    error = event.get("error") or event
                    message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    raise RuntimeError(f"OpenAI 流式错误: {message}")

        if final_usage is None:
            final_usage = self._fallback_usage(full_text, conf)
        final_usage["native_protocol"] = "responses"
        yield {"type": "done", "usage": final_usage}

    def _parse_openai_responses_response(
        self, data: dict[str, Any], conf: dict[str, Any], model: str
    ) -> dict[str, Any]:
        text_parts: list[str] = []
        for item in data.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for block in item.get("content", []):
                if isinstance(block, dict) and block.get("type") == "output_text":
                    text_parts.append(block.get("text", ""))
        usage = self._calc_cost_openai_responses(data.get("usage", {}), conf)
        usage["native_protocol"] = "responses"
        return {
            "content": "".join(text_parts),
            "model": data.get("model") or model,
            "provider": "openai",
            "usage": usage,
        }

    def _calc_cost_openai_responses(self, usage: dict[str, Any], conf: dict[str, Any]) -> dict[str, Any]:
        pricing = conf["pricing"]
        input_t = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        output_t = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        input_details = (
            usage.get("input_tokens_details")
            or usage.get("prompt_tokens_details")
            or {}
        )
        output_details = (
            usage.get("output_tokens_details")
            or usage.get("completion_tokens_details")
            or {}
        )
        cache_read = input_details.get("cached_tokens", 0) or 0
        cache_creation = input_details.get("cache_write_tokens", 0) or 0
        reasoning_tokens = output_details.get("reasoning_tokens", 0) or 0
        cached_price = pricing.get("cache_read", pricing.get("input", 0))
        billable_input = max(0, input_t - cache_read)
        calculated_cost = (
            billable_input * pricing.get("input", 0) / 1_000_000
            + cache_read * cached_price / 1_000_000
            + output_t * pricing.get("output", 0) / 1_000_000
        )
        cost, cost_source = self._resolve_usage_cost(usage, pricing, calculated_cost)
        saved = cache_read * max(0, pricing.get("input", 0) - cached_price) / 1_000_000
        return {
            "input_tokens": input_t,
            "output_tokens": output_t,
            "reasoning_tokens": reasoning_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "cost": cost,
            "cost_source": cost_source,
            "saved": round(saved, 6),
            "price_snapshot": pricing,
        }

    @staticmethod
    def _openai_model_supports_reasoning(model: str) -> bool:
        m = model.lower().split("/")[-1]
        return m.startswith(("gpt-5", "o1", "o3", "o4"))

    @staticmethod
    def _openai_model_supports_verbosity(model: str) -> bool:
        return model.lower().split("/")[-1].startswith("gpt-5")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Anthropic 原生 Messages API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _anthropic_request(
        self,
        conf: dict[str, Any],
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        stream: bool,
        options: dict[str, Any],
        *,
        tail_context: bool = False,
        history_cache_allowed: bool = True,
        session_id: str | None = None,
        purpose: str = "unspecified",
    ) -> dict[str, Any] | AsyncGenerator[dict[str, Any], None]:
        """通过官方 Anthropic SDK 调用原生 Messages API。"""
        provider_name = self._provider_name_from_conf(conf)
        cache_key = self._anthropic_cache_key(provider_name, model, session_id)
        normalized_messages = self._normalize_anthropic_messages(messages)
        if history_cache_allowed:
            normalized_messages = self._apply_history_cache(
                normalized_messages, cache_key=cache_key
            )
        normalized_messages = self._strip_internal_message_fields(
            normalized_messages
        )
        profile = get_model_capabilities(provider_name, model, conf)
        report = start_report(
            provider=provider_name,
            model=model,
            profile=profile,
            requested_options=options,
            system=system,
            messages=normalized_messages,
        )
        adapter = AnthropicMessagesAdapter(conf)
        kwargs = adapter.build_kwargs(
            model=model,
            system=self._build_anthropic_system_with_cache(
                system, tail_context=tail_context
            ),
            messages=normalized_messages,
            options=options,
            profile=profile,
            report=report,
        )
        self._attach_openrouter_session_id(
            kwargs, provider_name, session_id, report
        )
        self._reserve_anthropic_history_marker_budget(
            kwargs, reserve_history_markers=2, max_markers=4
        )
        report["cache_diagnostics"] = self._anthropic_cache_diagnostics(
            kwargs, model, cache_key=cache_key
        )
        cache_diag = report.get("cache_diagnostics") or {}
        if purpose == "main_chat":
            report["cache_wire_snapshot"] = copy.deepcopy(kwargs)
        strict_main = bool(
            str(purpose or "") == "main_chat"
            and CACHE_CONFIG.get("strict_mode", True)
        )
        if strict_main:
            lifetime = cache_keepalive.cache_lifetime_status(cache_key)
            naturally_expired = bool(lifetime.get("expired"))
            if naturally_expired:
                # A provider cache past its TTL is a normal cold start, not a
                # continuity failure. Drop stale process-local baselines so a
                # new cache can be established cleanly after restart/idle.
                self._anthropic_strict_cache_blocked.pop(cache_key, None)
                self._anthropic_cache_health.pop(cache_key, None)
                cache_diag["cache_natural_expiry"] = True
                cache_diag["cache_cold_start_reason"] = "ttl_expired"
            blocked = self._anthropic_strict_cache_blocked.get(cache_key)
            if isinstance(blocked, dict) and not naturally_expired:
                try:
                    ttl_seconds = int(CACHE_CONFIG.get("cache_ttl_seconds", 3600) or 3600)
                except (TypeError, ValueError):
                    ttl_seconds = 3600
                if time.time() - float(blocked.get("created_at") or 0) <= max(60, ttl_seconds):
                    raise RuntimeError(
                        "Claude 缓存连续性保护仍处于红灯：" +
                        str(blocked.get("reason") or "upstream_cache_read_below_expected") +
                        "。本轮长上下文未发送上游；等待 TTL 或重启服务后重新建立冷缓存。"
                    )
                self._anthropic_strict_cache_blocked.pop(cache_key, None)
            health = self._anthropic_cache_health.get(cache_key) or {}
            controlled_rebase = bool(cache_diag.get("cache_window_rebased"))
            cache_diag["cache_expected_read_floor"] = 0 if (naturally_expired or controlled_rebase) else max(
                0, int(health.get("last_reusable_prefix") or 0)
            )
            cache_diag["cache_strict_main_chat"] = True
        if strict_main and cache_diag.get("cache_guard_violation"):
            reason = str(cache_diag.get("cache_guard_reason") or "cache_continuity_broken")
            raise RuntimeError(
                "Claude 缓存连续性保护已拦截本轮长上下文：" + reason +
                "。请求尚未发送上游，请查看缓存诊断。"
            )
        if stream:
            return adapter.stream(
                kwargs=kwargs,
                report=report,
                usage_parser=lambda usage: self._finalize_anthropic_usage(
                    usage, conf=conf, model=model, session_id=session_id, purpose=purpose,
                    report=report, cache_key=cache_key, kwargs=kwargs
                ),
                fallback_usage=lambda text: self._fallback_usage(text, conf),
            )
        return await adapter.request(
            kwargs=kwargs,
            report=report,
            usage_parser=lambda usage: self._finalize_anthropic_usage(
                usage, conf=conf, model=model, session_id=session_id, purpose=purpose,
                report=report, cache_key=cache_key, kwargs=kwargs
            ),
        )

    def _finalize_anthropic_usage(
        self, raw_usage: dict[str, Any], *, conf: dict[str, Any], model: str,
        session_id: str | None, purpose: str, report: dict[str, Any],
        cache_key: str, kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        parsed = self._parse_anthropic_usage(raw_usage, conf, model, session_id=session_id)
        parsed = self._attach_cache_diagnostics_to_usage(
            parsed, report, cache_key=cache_key, purpose=purpose
        )
        if purpose == "main_chat" and str(session_id or ""):
            try:
                keepalive_meta = cache_keepalive.record_main_result(
                    cache_key=cache_key, provider=self._provider_name_from_conf(conf),
                    model=model, session_id=str(session_id),
                    diagnostics=report.get("cache_diagnostics") or {},
                    kwargs=report.get("cache_wire_snapshot") or kwargs, usage=parsed,
                )
                if isinstance(keepalive_meta, dict):
                    parsed.update(keepalive_meta)
            except Exception as exc:
                print(f"[Cache] keepalive state update skipped: {type(exc).__name__}")
        return parsed

    async def send_cache_keepalive(self, state: dict[str, Any]) -> dict[str, Any]:
        """Warm only the exact provider prefix through the deepest cache marker.

        Runtime tail material is deliberately absent.  A tiny post-breakpoint ping
        is added solely to force a real provider turn; output/thinking are capped so
        the warmer can never inherit a 32K/high-thinking main-chat budget.
        """
        provider = str(state.get("provider") or "")
        model = str(state.get("model") or "")
        conf = get_provider_config(provider)
        kwargs = cache_keepalive.trim_snapshot_to_deepest_marker(
            copy.deepcopy(state.get("snapshot") or {})
        )
        if not kwargs or not model:
            raise RuntimeError("cache keepalive prefix snapshot missing")
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            messages = []

        ping_text = "<cache_keepalive>Reply only: ok</cache_keepalive>"
        if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "user":
            # Consecutive user messages may be normalized/merged by providers.
            # Keep the ping after the marker inside the already-trimmed user turn.
            content = messages[-1].get("content")
            if isinstance(content, list):
                content.append({"type": "text", "text": ping_text})
            elif isinstance(content, str):
                messages[-1]["content"] = [{"type": "text", "text": content}, {"type": "text", "text": ping_text}]
            else:
                raise RuntimeError("cache keepalive unsupported user content")
        else:
            messages.append({"role": "user", "content": [{"type": "text", "text": ping_text}]})
        kwargs["messages"] = messages

        # Minimal inference budget. ``max_tokens`` is NOT part of the cache key,
        # so capping it is free. ``thinking`` / ``output_config`` are: toggling
        # extended thinking invalidates the whole messages-cache tier (tools and
        # system survive). A warmer that changed them could never read the prefix
        # it was sent to refresh -- it silently re-wrote the entire history at the
        # 1h (2x input) write price and let the real entry expire. Keep the wire
        # shape byte-identical to the main-chat request.
        kwargs["max_tokens"] = 256
        profile = get_model_capabilities(provider, model, conf)

        # OpenRouter response caching must remain disabled: a response-cache HIT
        # would bypass Anthropic and therefore fail to refresh prompt-cache TTL.
        if provider.startswith("openrouter"):
            extra_headers = dict(kwargs.get("extra_headers") or {})
            extra_headers["X-OpenRouter-Cache"] = "false"
            kwargs["extra_headers"] = extra_headers

        report = start_report(
            provider=provider, model=model, profile=profile, requested_options={
                "max_output_tokens": 256, "thinking_mode": "off", "reasoning_effort": "low"
            },
            system=kwargs.get("system"), messages=messages, tools=kwargs.get("tools"),
        )
        cache_key = str(state.get("cache_key") or "")
        # Continuity snapshots are keyed per wire shape. The warmer deliberately
        # sends a prefix trimmed to the deepest marker, so writing its snapshot
        # under the main-chat key made the next real turn compare against the
        # warmer and trip cache_guard_violation under strict_mode. Give the
        # warmer its own diagnostic lane; the billing/keepalive bookkeeping below
        # still uses the real cache_key.
        diagnostic_cache_key = (
            f"{cache_key}::lane:cache_keepalive" if cache_key else ""
        )
        report["cache_diagnostics"] = self._anthropic_cache_diagnostics(
            kwargs, model, cache_key=diagnostic_cache_key
        )
        adapter = AnthropicMessagesAdapter(conf)
        result = await adapter.request(
            kwargs=kwargs, report=report,
            usage_parser=lambda usage: self._finalize_anthropic_usage(
                usage, conf=conf, model=model, session_id=state.get("session_id"),
                purpose="cache_keepalive", report=report, cache_key=cache_key, kwargs=kwargs
            ),
        )
        if isinstance(result, dict):
            self._record_gateway_usage(
                session_id=str(state.get("session_id") or "") or None,
                provider=provider, model=model, purpose="cache_keepalive",
                usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
            )
            return result
        return {"usage": {}}

    def _apply_anthropic_thinking(
        self,
        body: dict[str, Any],
        model: str,
        options: dict[str, Any],
        max_tokens: int,
    ) -> None:
        mode = str(options.get("thinking_mode", "auto")).strip().lower()
        effort = str(options.get("reasoning_effort", "high")).strip().lower()
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            effort = "high"

        adaptive = self._anthropic_model_supports_adaptive(model)
        manual = self._anthropic_model_supports_manual_thinking(model)

        if mode == "off":
            if self._anthropic_model_defaults_thinking(model):
                body["thinking"] = {"type": "disabled"}
            return

        if mode == "adaptive" or (mode == "auto" and adaptive):
            # 不向 UI 暴露推理文本；保留完整推理能力并降低首字延迟。
            body["thinking"] = {"type": "adaptive", "display": "omitted"}
            body["output_config"] = {"effort": effort}
            return

        if mode == "manual" or (mode == "auto" and manual):
            if max_tokens <= 1536:
                return
            budget = self._safe_int(options.get("thinking_budget"), 8000)
            budget = max(1024, min(budget, max_tokens - 512))
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
                "display": "omitted",
            }

    @staticmethod
    def _anthropic_model_supports_adaptive(model: str) -> bool:
        m = model.lower()
        markers = (
            "claude-sonnet-5", "claude-opus-4-6", "claude-opus-4-7",
            "claude-opus-4-8", "claude-sonnet-4-6", "claude-fable-5",
            "claude-mythos",
        )
        return any(marker in m for marker in markers)

    @staticmethod
    def _anthropic_model_defaults_thinking(model: str) -> bool:
        m = model.lower()
        return any(x in m for x in ("claude-sonnet-5", "claude-fable-5", "claude-mythos"))

    @staticmethod
    def _anthropic_model_supports_manual_thinking(model: str) -> bool:
        m = model.lower()
        if any(x in m for x in ("claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8", "claude-fable-5")):
            return False
        return any(x in m for x in ("claude-sonnet-4", "claude-opus-4", "claude-haiku-4"))

    def _build_anthropic_system_with_cache(
        self, system: str, *, tail_context: bool = False
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        static_parts: list[str] = []
        for key, block in SYSTEM_PROMPT_BLOCKS.items():
            if block.strip():
                static_parts.append(f"<{key}>\n{block}\n</{key}>")
        static_text = "\n\n".join(static_parts)
        has_default_system = bool(static_text and system.startswith(static_text))
        if has_default_system:
            cached_text = static_text
            if tail_context:
                cached_text = (
                    f"{cached_text}\n\n<turn_context_contract>\n"
                    f"{_ANTHROPIC_TURN_CONTEXT_CONTRACT}\n"
                    "</turn_context_contract>"
                )
            block = {"type": "text", "text": cached_text}
            if CACHE_CONFIG.get("enable_system_cache", True):
                block["cache_control"] = self._anthropic_cache_control()
            blocks.append(block)
        dynamic_text = (
            system[len(static_text):].strip()
            if has_default_system else system.strip()
        )
        if dynamic_text:
            dynamic_block = {"type": "text", "text": dynamic_text}
            # With turn-tail mode, everything remaining in ``system`` was
            # explicitly classified by the caller as session-stable context.
            # Cache it as a second segment; actual per-turn instructions live
            # after the visible-user checkpoint instead.
            if tail_context and CACHE_CONFIG.get("enable_system_cache", True):
                dynamic_block["cache_control"] = self._anthropic_cache_control()
            blocks.append(dynamic_block)
        return blocks

    def _normalize_anthropic_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            envelope = message.get("native_envelope")
            if (role == "assistant" and isinstance(envelope, dict)
                    and envelope.get("api_family") == "messages"
                    and isinstance(envelope.get("content"), list)):
                content = copy.deepcopy(envelope["content"])
            else:
                content = message.get("content", "")
            if isinstance(content, str):
                # Canonical Anthropic wire shape: text is always a content
                # block array, even when this message has no cache marker.
                # Otherwise a rolling marker turns "text" -> [text block]
                # for one request and back to "text" on the next, mutating
                # the historical prefix despite identical visible content.
                normalized: Any = ([{"type": "text", "text": content}] if content else [])
            elif isinstance(content, list):
                normalized = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in {
                        "text", "image", "document", "tool_use", "tool_result",
                        "thinking", "redacted_thinking",
                    }:
                        normalized.append(copy.deepcopy(block))
                if not normalized:
                    normalized = self._flatten_content(content)
            else:
                normalized = str(content or "")
            normalized_message = {"role": role, "content": normalized}
            if message.get("_cache_replay_stable") is False:
                normalized_message["_cache_replay_stable"] = False
            result.append(normalized_message)
        return result

    @staticmethod
    def _strip_internal_message_fields(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove local cache-planning metadata before provider transport."""
        return [
            {
                key: copy.deepcopy(value)
                for key, value in message.items()
                if not str(key).startswith("_")
            }
            for message in messages
        ]

    @staticmethod
    def _stable_session_fingerprint(session_id: str | None) -> str:
        raw = str(session_id or "").strip()
        if not raw:
            return ""
        return hashlib.sha256(f"daxigua-cache:{raw}".encode("utf-8")).hexdigest()

    @staticmethod
    def _anthropic_transport_session_id(
        session_id: str | None, purpose: str
    ) -> str | None:
        """Isolate incompatible Anthropic wire shapes without changing ledgers."""
        raw = str(session_id or "").strip()
        if not raw:
            return session_id
        lane = str(purpose or "unspecified").strip().lower() or "unspecified"
        if lane == "main_chat":
            return raw
        lane = re.sub(r"[^a-z0-9_.:-]+", "_", lane)[:48]
        return f"{raw}::lane:{lane}"

    @classmethod
    def _anthropic_cache_key(
        cls, provider_name: str, model: str, session_id: str | None = None
    ) -> str:
        key = f"{provider_name}:{model}".lower()
        fingerprint = cls._stable_session_fingerprint(session_id)
        return f"{key}:{fingerprint[:16]}" if fingerprint else key

    @classmethod
    def _openrouter_sticky_session_id(
        cls, provider_name: str, session_id: str | None
    ) -> str:
        if not str(provider_name or "").lower().startswith("openrouter"):
            return ""
        fingerprint = cls._stable_session_fingerprint(session_id)
        return f"daxigua-{fingerprint[:48]}" if fingerprint else ""

    def _attach_openrouter_session_id(
        self,
        kwargs: dict[str, Any],
        provider_name: str,
        session_id: str | None,
        report: dict[str, Any] | None = None,
    ) -> None:
        sticky_id = self._openrouter_sticky_session_id(provider_name, session_id)
        if not sticky_id:
            return
        extra_body = kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
            kwargs["extra_body"] = extra_body
        extra_body["session_id"] = sticky_id
        if report is not None:
            integrity_sent(report, "openrouter.session_id", "stable_hashed")

    @staticmethod
    def _anthropic_cache_control() -> dict[str, str]:
        try:
            ttl_seconds = int(CACHE_CONFIG.get("cache_ttl_seconds", 3600) or 3600)
        except (TypeError, ValueError):
            ttl_seconds = 3600
        # Production companion chats use the explicit one-hour tier.  Five
        # minutes remains available as an intentional operator override.
        ttl = "1h" if ttl_seconds >= 3600 else "5m"
        return {"type": "ephemeral", "ttl": ttl}

    @staticmethod
    def _anthropic_min_cache_tokens(model: str) -> int:
        """Published minimum cacheable prefix size for current Claude families."""
        value = str(model or "").lower().replace("_", "-")
        if any(name in value for name in ("claude-opus-5", "claude-fable-5", "claude-mythos-5")):
            return 512
        if any(name in value for name in ("claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4.6", "claude-sonnet-4-5", "claude-sonnet-4.5", "claude-opus-4-8", "claude-opus-4.8")):
            return 1024
        if any(name in value for name in ("claude-opus-4-7", "claude-opus-4.7")):
            return 2048
        if any(name in value for name in ("claude-opus-4-6", "claude-opus-4.6", "claude-opus-4-5", "claude-opus-4.5", "claude-haiku-4-5", "claude-haiku-4.5")):
            return 4096
        return 1024

    @staticmethod
    def _cache_control_from_content(content: Any) -> dict[str, Any]:
        if not isinstance(content, list):
            return {}
        for block in reversed(content):
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                return dict(block["cache_control"])
        return {}

    @staticmethod
    def _content_through_cache_control(content: Any) -> Any:
        """Return only provider content covered by its deepest breakpoint."""
        if not isinstance(content, list):
            return copy.deepcopy(content)
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                return copy.deepcopy(content[: index + 1])
        return copy.deepcopy(content)

    def _messages_through_cache_breakpoint(
        self, messages: list[dict[str, Any]], index: int
    ) -> list[dict[str, Any]]:
        if index < 0:
            return []
        prefix = copy.deepcopy(messages[: index + 1])
        if prefix:
            prefix[-1]["content"] = self._content_through_cache_control(
                prefix[-1].get("content")
            )
        return prefix

    @staticmethod
    def _without_cache_control(value: Any) -> Any:
        """Return a canonical copy of provider content without cache marker metadata.

        Anthropic cache_control chooses checkpoints; it is not conversational
        content.  Diagnostics therefore hash both the stable provider content
        and checkpoint placement separately instead of confusing marker motion
        with an actual history rewrite.
        """
        if isinstance(value, dict):
            return {
                key: Gateway._without_cache_control(item)
                for key, item in value.items()
                if key != "cache_control"
            }
        if isinstance(value, list):
            return [Gateway._without_cache_control(item) for item in value]
        return value

    @staticmethod
    def _hash_json(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _anthropic_cache_diagnostics(
        self, kwargs: dict[str, Any], model: str, *, cache_key: str = ""
    ) -> dict[str, Any]:
        """Hash cacheable prefixes and verify cross-turn checkpoint continuity.

        ``cache_prefix_hash`` hashes provider-visible content through the deepest
        history breakpoint while ignoring cache_control metadata.  The matching
        ``cache_parent_hash`` hashes the prefix through the older overlapping
        user checkpoint.  That point is the previous request's deepest
        checkpoint, so the two hashes line up across consecutive turns.

        ``cache_checkpoint_overlap`` additionally verifies that the previous
        deepest explicit history checkpoint is still explicitly marked on the
        current request.  This catches the failure mode where a single rolling
        marker jumps forward every turn and Anthropic can only reuse the system
        prefix.  Only hashes/indexes are kept; prompt text is never retained.
        """
        system = kwargs.get("system") or []
        messages = kwargs.get("messages") or []
        message_breaks: list[int] = []
        ttl = ""
        if isinstance(system, list):
            control = self._cache_control_from_content(system)
            if control:
                ttl = str(control.get("ttl") or "")
        if isinstance(messages, list):
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                control = self._cache_control_from_content(message.get("content"))
                if control:
                    message_breaks.append(index)
                    ttl = str(control.get("ttl") or ttl)

        last_message_break = message_breaks[-1] if message_breaks else -1
        user_indices = [
            i for i, item in enumerate(messages if isinstance(messages, list) else [])
            if isinstance(item, dict) and item.get("role") == "user"
        ]
        assistant_indices = [
            i for i, item in enumerate(messages if isinstance(messages, list) else [])
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        prefix_messages = (
            self._messages_through_cache_breakpoint(messages, last_message_break)
            if isinstance(messages, list) and last_message_break >= 0
            else []
        )
        prefix_payload = self._without_cache_control({
            "tools": kwargs.get("tools") or [],
            "system": system,
            "messages": prefix_messages,
        })

        parent_payload: dict[str, Any] | None = None
        parent_idx = message_breaks[-2] if len(message_breaks) >= 2 else -1
        if parent_idx >= 0:
            parent_payload = self._without_cache_control({
                "tools": kwargs.get("tools") or [],
                "system": system,
                "messages": self._messages_through_cache_breakpoint(messages, parent_idx),
            })

        shape_payload = self._without_cache_control({
            "model": model,
            "tools": kwargs.get("tools") or [],
            "system": system,
            "thinking": kwargs.get("thinking"),
            "output_config": kwargs.get("output_config"),
            "tool_choice": kwargs.get("tool_choice"),
        })
        prefix_hash = self._hash_json(prefix_payload)
        parent_hash = self._hash_json(parent_payload) if parent_payload is not None else ""
        shape_hash = self._hash_json(shape_payload)
        prefix_chars = len(json.dumps(
            prefix_payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ))

        labels: list[str] = ["system"]
        for idx in message_breaks:
            if idx in assistant_indices:
                rank = assistant_indices.index(idx) - len(assistant_indices)
                labels.append(f"assistant[{rank}]")
            elif idx in user_indices:
                rank = user_indices.index(idx) - len(user_indices)
                labels.append(f"user[{rank}]")
            else:
                labels.append(f"message[{idx}]")

        result: dict[str, Any] = {
            "cache_prefix_hash": prefix_hash,
            "cache_parent_hash": parent_hash,
            "cache_shape_hash": shape_hash,
            "cache_ttl": ttl or str(self._anthropic_cache_control().get("ttl") or ""),
            "cache_breakpoint": "+".join(labels),
            "cache_prefix_chars": prefix_chars,
            "cache_prefix_tokens_estimate": max(1, math.ceil(prefix_chars / 2.4)),
            "cache_min_tokens": self._anthropic_min_cache_tokens(model),
            "cache_strategy": "system+visible_user_overlap+post_checkpoint_runtime_tail",
            "cache_control_count": (
                (1 if self._cache_control_from_content(system) else 0)
                + len(message_breaks)
                + sum(1 for tool in (kwargs.get("tools") or [])
                      if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict))
            ),
            "cache_tools_hash": self._hash_json(self._without_cache_control(kwargs.get("tools") or [])),
            "cache_segment_hashes": json.dumps({
                "tools": self._hash_json(self._without_cache_control(kwargs.get("tools") or []))[:16],
                "system": self._hash_json(self._without_cache_control(system))[:16],
                "checkpoints": [
                    self._hash_json(self._without_cache_control({
                        **messages[i],
                        "content": self._content_through_cache_control(messages[i].get("content")),
                    }))[:16]
                    for i in message_breaks if 0 <= i < len(messages)
                ],
            }, separators=(",", ":")),
        }

        if cache_key:
            snapshots = getattr(self, "_anthropic_wire_snapshots", None)
            if not isinstance(snapshots, dict):
                snapshots = {}
                self._anthropic_wire_snapshots = snapshots
            previous = snapshots.get(cache_key)
            content_hashes = [
                self._hash_json(self._without_cache_control({
                    **message,
                    "content": self._content_through_cache_control(
                        message.get("content")
                    ),
                }))
                for message in (messages if isinstance(messages, list) else [])
            ]
            checkpoint_hashes = {
                idx: self._hash_json(self._without_cache_control({
                    **messages[idx],
                    "content": self._content_through_cache_control(
                        messages[idx].get("content")
                    ),
                }))
                for idx in message_breaks
                if 0 <= idx < len(messages)
            }
            overlap = "first_request"
            first_divergence = -1
            shape_changed = False
            previous_fresh = False
            if isinstance(previous, dict):
                try:
                    ttl_seconds = int(CACHE_CONFIG.get("cache_ttl_seconds", 3600) or 3600)
                except (TypeError, ValueError):
                    ttl_seconds = 3600
                previous_age = max(0.0, time.time() - float(previous.get("created_at") or 0.0))
                previous_fresh = previous_age <= max(60, ttl_seconds)
                if previous_fresh:
                    previous_deepest = int(previous.get("deepest_index", -1))
                    previous_checkpoint_hash = str(previous.get("deepest_checkpoint_hash") or "")
                    checkpoint_reindexed = False
                    if previous_deepest >= 0 and previous_checkpoint_hash:
                        current_same_checkpoint = checkpoint_hashes.get(previous_deepest, "")
                        if current_same_checkpoint == previous_checkpoint_hash:
                            overlap = "ok"
                        else:
                            # A stable-history window rotation can drop old
                            # messages and shift every surviving message index.
                            # Match the canonical checkpoint by content hash
                            # before declaring continuity broken.
                            reindexed_at = next(
                                (
                                    idx for idx, digest in checkpoint_hashes.items()
                                    if digest == previous_checkpoint_hash
                                ),
                                -1,
                            )
                            if reindexed_at >= 0:
                                overlap = "ok"
                                checkpoint_reindexed = reindexed_at != previous_deepest
                            else:
                                overlap = "missing_or_changed"
                    old_hashes = previous.get("content_hashes") or []
                    compare_limit = min(len(old_hashes), len(content_hashes))
                    for idx in range(compare_limit):
                        if old_hashes[idx] != content_hashes[idx]:
                            first_divergence = idx
                            break
                    shape_changed = bool(
                        previous.get("shape_hash")
                        and str(previous.get("shape_hash")) != shape_hash
                    )
            previous_prefix_hash = str((previous or {}).get("prefix_hash") or "") if isinstance(previous, dict) else ""
            parent_continuity = bool(parent_hash and previous_prefix_hash and parent_hash == previous_prefix_hash)
            window_rebased = bool(
                previous_fresh
                and locals().get("checkpoint_reindexed", False)
                and overlap == "ok"
                and not parent_continuity
            )
            if window_rebased:
                # The same deepest historical checkpoint survived, but older
                # history was intentionally rotated out. This is a controlled
                # cold rebase, not corruption.
                result["cache_window_rebased"] = True
                result["cache_cold_start_reason"] = "history_window_rebased"
            result["cache_breakpoint"] += f";overlap={overlap}"
            if first_divergence >= 0:
                result["cache_breakpoint"] += f";first_diff=message[{first_divergence}]"
            if shape_changed:
                result["cache_breakpoint"] += ";shape=changed"
            guard_reasons = []
            if previous_fresh and overlap not in {"ok", "first_request"}:
                guard_reasons.append(f"overlap={overlap}")
            if previous_fresh and shape_changed:
                guard_reasons.append("shape_hash_changed")
            if previous_fresh and previous_prefix_hash and not parent_continuity and not window_rebased:
                guard_reasons.append("parent_prefix_hash_mismatch")
            result["cache_overlap"] = overlap
            result["cache_parent_continuity"] = parent_continuity
            result["cache_first_diff_index"] = first_divergence
            result["cache_shape_changed"] = shape_changed
            result["cache_guard_violation"] = bool(guard_reasons)
            result["cache_guard_reason"] = ",".join(guard_reasons)

            deepest_hash = checkpoint_hashes.get(last_message_break, "")
            snapshots[cache_key] = {
                "deepest_index": last_message_break,
                "deepest_checkpoint_hash": deepest_hash,
                "content_hashes": content_hashes[: last_message_break + 1] if last_message_break >= 0 else [],
                "shape_hash": shape_hash,
                "prefix_hash": prefix_hash,
                "prefix_tokens_estimate": result["cache_prefix_tokens_estimate"],
                "created_at": time.time(),
            }
            # Bound diagnostics memory without retaining any prompt content.
            while len(snapshots) > 512:
                snapshots.pop(next(iter(snapshots)))

        return result

    def _attach_cache_diagnostics_to_usage(
        self, usage: dict[str, Any], report: dict[str, Any] | None, *,
        cache_key: str = "", purpose: str = "unspecified"
    ) -> dict[str, Any]:
        diagnostics_payload = (report or {}).get("cache_diagnostics") or {}
        if isinstance(diagnostics_payload, dict):
            expected = max(0, int(diagnostics_payload.get("cache_expected_read_floor") or 0))
            actual = max(0, int(usage.get("cache_read") or 0))
            strict_main = bool(
                str(purpose or "") == "main_chat"
                and CACHE_CONFIG.get("strict_mode", True)
                and diagnostics_payload.get("cache_strict_main_chat")
            )
            lifetime = cache_keepalive.cache_lifetime_status(cache_key) if cache_key else {}
            naturally_expired = bool(
                diagnostics_payload.get("cache_natural_expiry") or lifetime.get("expired")
            )
            if naturally_expired:
                diagnostics_payload["cache_natural_expiry"] = True
                diagnostics_payload.setdefault("cache_cold_start_reason", "ttl_expired")
            if (strict_main and not naturally_expired
                    and expected >= self._anthropic_min_cache_tokens(str((report or {}).get("model") or ""))):
                # A hot request should at least recover the immediately prior
                # reusable prefix. Natural TTL expiry is explicitly excluded.
                floor = max(1, int(expected * 0.90))
                if actual < floor:
                    reason = f"cache_read_below_previous_reusable_prefix:{actual}<{expected}"
                    diagnostics_payload["cache_guard_violation_actual"] = True
                    diagnostics_payload["cache_guard_reason_actual"] = reason
                    if cache_key:
                        self._anthropic_strict_cache_blocked[cache_key] = {
                            "created_at": time.time(),
                            "reason": reason,
                        }
            for key, value in diagnostics_payload.items():
                usage[key] = value
        return usage

    @staticmethod
    def _cacheable_block_index(content: Any) -> int:
        if not isinstance(content, list):
            return -1
        cacheable_types = {"text", "image", "document", "tool_use", "tool_result"}
        for index in range(len(content) - 1, -1, -1):
            block = content[index]
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and str(block.get("text") or "").lstrip().startswith(
                    "<application_turn_context>"
                )
            ):
                continue
            if isinstance(block, dict) and block.get("type") in cacheable_types:
                return index
        return -1

    def _history_cache_breakpoints(self, messages: list[dict[str, Any]]) -> list[int]:
        """Return the last two stable assistant checkpoints.

        This is the direct rolling-breakpoint layout used by the OpenRouter
        caching guide: system is the fixed BP, history rolls on the response
        immediately before the newest user turn, and the newest user/runtime
        material remains uncached. Two overlapping assistant checkpoints keep
        the previous deepest BP explicit on the next request.
        """
        indices = []
        for i, message in enumerate(messages):
            if message.get("role") != "assistant" or message.get("_cache_replay_stable") is False:
                continue
            content = message.get("content", "")
            if (isinstance(content, str) and content) or self._cacheable_block_index(content) >= 0:
                indices.append(i)
        if not indices:
            return []
        try:
            overlap_count = int(CACHE_CONFIG.get("history_overlap_breakpoints", 2) or 2)
        except (TypeError, ValueError):
            overlap_count = 2
        overlap_count = max(1, min(overlap_count, 2))
        return indices[-overlap_count:]

    def _apply_history_cache(
        self, messages: list[dict[str, Any]], *, cache_key: str = ""
    ) -> list[dict[str, Any]]:
        if (
            not CACHE_CONFIG.get("enable_history_cache")
            or (cache_key and cache_key in self._anthropic_history_cache_disabled)
            or not messages
        ):
            return messages
        result = copy.deepcopy(messages)
        breakpoint_indices = self._history_cache_breakpoints(result)
        if not breakpoint_indices:
            return result
        cache_control = self._anthropic_cache_control()
        for breakpoint_idx in breakpoint_indices:
            msg = result[breakpoint_idx]
            content = msg.get("content", "")
            if isinstance(content, str):
                msg["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": copy.deepcopy(cache_control),
                }]
            elif isinstance(content, list) and content:
                block_idx = self._cacheable_block_index(content)
                if block_idx >= 0:
                    content[block_idx]["cache_control"] = copy.deepcopy(cache_control)
        return result

    def _find_breakpoint(self, messages: list[dict[str, Any]]) -> int:
        """Compatibility helper: return the deepest rolling history checkpoint."""
        points = self._history_cache_breakpoints(messages)
        return points[-1] if points else -1

    def _parse_anthropic_usage(
        self,
        usage: dict[str, Any],
        conf: dict[str, Any],
        model: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        token_info = self._calc_cost_anthropic(usage, conf)
        provider_name = self._provider_name_from_conf(conf)
        cache_key = self._anthropic_cache_key(provider_name, model, session_id)
        self._observe_anthropic_cache(cache_key, token_info)
        health = self._anthropic_cache_health.get(cache_key) or {}
        if cache_key in self._anthropic_history_cache_disabled:
            status = "disabled_for_process"
        elif health.get("warning"):
            status = "warning_high_write_low_read"
        else:
            status = "active"
        token_info["history_cache_guard"] = status
        token_info["cache_guard_status"] = status
        return token_info

    def _observe_anthropic_cache(
        self, cache_key: str, token_info: dict[str, Any]
    ) -> None:
        if not CACHE_CONFIG.get("enable_history_cache"):
            return
        cache_read = max(0, int(token_info.get("cache_read") or 0))
        cache_creation = max(0, int(token_info.get("cache_creation") or 0))
        if not cache_read and not cache_creation:
            return
        health = self._anthropic_cache_health.setdefault(
            cache_key, {"samples": 0, "read": 0, "creation": 0, "warning": 0}
        )
        health["samples"] += 1
        health["read"] += cache_read
        health["creation"] += cache_creation
        health["last_cache_read"] = cache_read
        health["last_cache_creation"] = cache_creation
        # After this response, both tokens read from the prior cache and newly
        # created cache tokens form the prefix that the next hot request should
        # be able to reuse.  Comparing only last_cache_creation would miss a
        # catastrophic 140K -> 3K collapse when the prior turn wrote only 2K.
        health["last_reusable_prefix"] = cache_read + cache_creation
        min_samples = max(2, int(CACHE_CONFIG.get("health_min_samples", 4)))
        min_creation = max(
            1024, int(CACHE_CONFIG.get("health_min_creation_tokens", 32768))
        )
        max_ratio = max(
            1.0, float(CACHE_CONFIG.get("health_max_creation_read_ratio", 2.0))
        )
        unhealthy = (
            health["samples"] >= min_samples
            and health["creation"] >= min_creation
            and health["creation"] > max(1, health["read"]) * max_ratio
        )
        if not unhealthy:
            return
        health["warning"] = 1
        action = str(CACHE_CONFIG.get("health_action") or "warn").strip().lower()
        if action == "disable":
            self._anthropic_history_cache_disabled.add(cache_key)
            print(
                "[Cache] Anthropic rolling history cache disabled by explicit "
                f"health policy: {cache_key} (write={health['creation']}, read={health['read']})"
            )
        elif health.get("warning_printed") != 1:
            health["warning_printed"] = 1
            print(
                "[Cache] Anthropic cache warning: repeated writes are much larger "
                f"than reads for {cache_key} (write={health['creation']}, read={health['read']}). "
                "Cache remains enabled so routing/prefix diagnostics stay observable."
            )


    async def _anthropic_stream(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        conf: dict[str, Any],
        model: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        full_text = ""
        usage: dict[str, Any] = {}
        reasoning_announced = False
        yield {"type": "status", "stage": "connecting", "label": "连接 Claude 原生接口…"}

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                raw = await resp.aread()
                self._raise_upstream_error_bytes(resp.status_code, raw, "Anthropic Messages API")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")
                if event_type == "message_start":
                    usage.update((event.get("message") or {}).get("usage", {}) or {})
                    yield {"type": "status", "stage": "reasoning", "label": "Claude 正在思考…"}
                elif event_type == "content_block_start":
                    block = event.get("content_block", {}) or {}
                    if block.get("type") in {"thinking", "redacted_thinking"} and not reasoning_announced:
                        reasoning_announced = True
                        yield {"type": "status", "stage": "reasoning", "label": "Claude 正在深度思考…"}
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {}) or {}
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            full_text += text
                            yield {"type": "text", "text": text}
                    elif delta_type == "thinking_delta" and not reasoning_announced:
                        reasoning_announced = True
                        yield {"type": "status", "stage": "reasoning", "label": "Claude 正在深度思考…"}
                elif event_type == "message_delta":
                    usage.update(event.get("usage", {}) or {})
                elif event_type == "error":
                    err = event.get("error", {}) or {}
                    raise RuntimeError(f"Anthropic 流式错误: {err.get('message') or err}")

        token_info = self._calc_cost_anthropic(usage, conf)
        token_info["native_protocol"] = "messages"
        yield {"type": "done", "usage": token_info}

    def _parse_anthropic_response(
        self, data: dict[str, Any], conf: dict[str, Any], model: str
    ) -> dict[str, Any]:
        content = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        token_info = self._calc_cost_anthropic(data.get("usage", {}), conf)
        token_info["native_protocol"] = "messages"
        return {
            "content": content,
            "model": data.get("model") or model,
            "provider": "anthropic",
            "usage": token_info,
        }

    def _calc_cost_anthropic(self, usage: dict[str, Any], conf: dict[str, Any]) -> dict[str, Any]:
        pricing = conf["pricing"]
        prompt_details = usage.get("prompt_tokens_details", {}) or {}
        output_details = (
            usage.get("output_tokens_details")
            or usage.get("completion_tokens_details")
            or {}
        )
        input_t = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
        output_t = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
        cache_read = int(
            usage.get("cache_read_input_tokens")
            or prompt_details.get("cached_tokens")
            or 0
        )
        cache_creation = int(
            usage.get("cache_creation_input_tokens")
            or prompt_details.get("cache_write_tokens")
            or 0
        )
        creation_detail = usage.get("cache_creation") or {}
        if not isinstance(creation_detail, dict):
            creation_detail = {}
        cache_creation_1h = int(creation_detail.get("ephemeral_1h_input_tokens") or 0)
        cache_creation_5m = int(creation_detail.get("ephemeral_5m_input_tokens") or 0)
        classified_creation = cache_creation_1h + cache_creation_5m
        # Some transports expose only the aggregate creation count. Preserve
        # the total and classify the otherwise-unknown bucket according to the
        # explicit TTL we sent so local direct-Anthropic pricing remains sane.
        unclassified_creation = max(0, cache_creation - classified_creation)
        try:
            configured_ttl = int(CACHE_CONFIG.get("cache_ttl_seconds", 3600) or 3600)
        except (TypeError, ValueError):
            configured_ttl = 3600
        if unclassified_creation:
            if configured_ttl >= 3600:
                cache_creation_1h += unclassified_creation
            else:
                cache_creation_5m += unclassified_creation

        thinking_tokens = int(
            output_details.get("thinking_tokens")
            or output_details.get("reasoning_tokens")
            or 0
        )
        billable_input = input_t
        write_5m_price = float(pricing.get("cache_write", 0) or 0)
        write_1h_price = float(
            pricing.get("cache_write_1h", pricing.get("input", 0) * 2) or 0
        )
        calculated_cost = (
            billable_input * float(pricing.get("input", 0) or 0) / 1_000_000
            + output_t * float(pricing.get("output", 0) or 0) / 1_000_000
            + cache_read * float(pricing.get("cache_read", 0) or 0) / 1_000_000
            + cache_creation_5m * write_5m_price / 1_000_000
            + cache_creation_1h * write_1h_price / 1_000_000
        )
        cost, cost_source = self._resolve_usage_cost(usage, pricing, calculated_cost)
        saved = cache_read * max(
            0, float(pricing.get("input", 0) or 0) - float(pricing.get("cache_read", 0) or 0)
        ) / 1_000_000
        return {
            "input_tokens": input_t,
            "output_tokens": output_t,
            "reasoning_tokens": thinking_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "cache_creation_1h": cache_creation_1h,
            "cache_creation_5m": cache_creation_5m,
            "cost": cost,
            "cost_source": cost_source,
            "saved": round(saved, 6),
            "price_snapshot": pricing,
        }


    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # OpenAI-compatible Chat Completions
    # DeepSeek / GLM / OpenRouter
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _openai_compat_request(
        self,
        conf: dict[str, Any],
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        stream: bool,
        options: dict[str, Any],
    ) -> dict[str, Any] | AsyncGenerator[dict[str, Any], None]:
        url = self._join_url(conf["base_url"], conf.get("chat_path", "/v1/chat/completions"))
        headers = {
            "Authorization": f"Bearer {conf['api_key']}",
            "Content-Type": "application/json",
        }
        headers.update(conf.get("extra_headers", {}))

        provider_name = conf.get("provider_name") or self._provider_name_from_conf(conf)
        oai_messages = self._compat_messages(system, messages, provider_name)

        profile = get_model_capabilities(provider_name, model, conf)
        report = start_report(
            provider=profile.provider,
            model=model,
            profile=profile,
            requested_options=options,
            system=system,
            messages=messages,
        )
        report["adapter"] = (
            "httpx/deepseek-chat-completions"
            if provider_name == "deepseek"
            else "httpx/chat-completions-compatible"
        )
        if provider_name == "deepseek":
            report["cache_strategy"] = "automatic_longest_common_prefix+ephemeral_turn_context"
            report["cache_prefix_hash"] = self._hash_json({
                "model": model,
                "messages": self._deepseek_cacheable_prefix(oai_messages),
            })
        body: dict[str, Any] = {"model": model, "messages": oai_messages, "stream": stream}
        integrity_sent(report, "stream", stream)
        self._apply_compat_options(
            body,
            conf=conf,
            model=model,
            options=options,
            profile=profile,
            report=report,
            stream=stream,
        )

        if stream:
            return self._openai_compat_stream(
                url, headers, body, conf, model, report, provider_name
            )
        resp = await self._client.post(url, headers=headers, json=body)
        self._raise_upstream_error(resp, "OpenAI兼容接口")
        parsed = self._parse_openai_compat_response(
            resp.json(), conf, model, provider_name
        )
        if str(options.get("thinking_visibility") or "full").lower() != "full":
            parsed.pop("reasoning_content", None)
        raw_data = resp.json()
        choices = raw_data.get("choices", [])
        stop_reason = choices[0].get("finish_reason") if choices else "completed"
        parsed["usage"]["integrity"] = finish_integrity(
            report,
            actual_model=parsed.get("model") or model,
            stop_reason=stop_reason or "completed",
            usage=parsed["usage"],
        )
        return parsed

    async def _openai_compat_stream(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        conf: dict[str, Any],
        model: str,
        report: dict[str, Any],
        provider_name: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        full_text = ""
        reasoning_text = ""
        final_usage: dict[str, Any] | None = None
        stop_reason: Any = None
        actual_model = model
        reasoning_announced = False
        thinking_visibility = str(
            (report.get("requested_options") or {}).get("thinking_visibility") or "hidden"
        ).lower()
        show_thinking = thinking_visibility == "full"
        label = (
            "连接 DeepSeek 原生思考接口…"
            if provider_name == "deepseek"
            else "连接 OpenRouter 可见推理接口…"
            if provider_name == "openrouter"
            else "连接兼容接口…"
        )
        yield {"type": "status", "stage": "connecting", "label": label}

        async with self._client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                raw = await resp.aread()
                self._raise_upstream_error_bytes(resp.status_code, raw, "OpenAI兼容接口")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    err = event["error"]
                    message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"上游 API 错误: {message}")
                record_integrity_event(report, "chat.completion.chunk", known=True)
                actual_model = event.get("model") or actual_model
                choices = event.get("choices", [])
                if choices:
                    choice = choices[0]
                    delta = choice.get("delta", {}) or {}
                    reasoning_piece = self._compat_reasoning_text(delta)
                    if reasoning_piece and not reasoning_announced:
                        reasoning_announced = True
                        reasoning_label = (
                            "DeepSeek 正在展开模型草稿…"
                            if provider_name == "deepseek"
                            else "OpenRouter 正在返回可见推理…"
                            if provider_name == "openrouter"
                            else "模型正在返回可见推理…"
                        )
                        yield {"type": "status", "stage": "reasoning", "label": reasoning_label}
                    if reasoning_piece:
                        reasoning_text += reasoning_piece
                        if show_thinking:
                            yield {"type": "thinking", "text": reasoning_piece}
                    text = delta.get("content") or ""
                    if text:
                        full_text += text
                        yield {"type": "text", "text": text}
                    stop_reason = choice.get("finish_reason") or stop_reason
                if event.get("usage"):
                    final_usage = self._calc_cost_openai_compat(event["usage"], conf, actual_model)

        if final_usage is None:
            final_usage = self._fallback_usage(full_text, conf)
        final_usage["native_protocol"] = (
            "deepseek_chat_completions"
            if provider_name == "deepseek"
            else "chat_completions_compatible"
        )
        final_usage["integrity"] = finish_integrity(
            report,
            actual_model=actual_model,
            stop_reason=stop_reason or "completed",
            usage=final_usage,
        )
        done = {"type": "done", "usage": final_usage}
        if show_thinking and reasoning_text:
            done["reasoning_content"] = reasoning_text
        yield done

    def _parse_openai_compat_response(
        self,
        data: dict[str, Any],
        conf: dict[str, Any],
        model: str,
        provider_name: str | None = None,
    ) -> dict[str, Any]:
        choices = data.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "")
        reasoning_content = self._compat_reasoning_text(message)
        actual_model = data.get("model") or model
        provider_name = provider_name or conf.get("provider_name", "compatible")
        usage = self._calc_cost_openai_compat(data.get("usage", {}), conf, actual_model)
        usage["native_protocol"] = (
            "deepseek_chat_completions"
            if provider_name == "deepseek"
            else "chat_completions_compatible"
        )
        return {
            "content": content,
            "reasoning_content": reasoning_content,
            "model": actual_model,
            "provider": provider_name,
            "usage": usage,
        }

    @staticmethod
    def _reasoning_details_text(details: Any) -> str:
        if not isinstance(details, list):
            return ""
        parts: list[str] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "reasoning.text":
                text = item.get("text") or ""
            elif item_type == "reasoning.summary":
                text = item.get("summary") or ""
            else:
                # reasoning.encrypted is preserved by native continuity when
                # required, but ciphertext is never presented as readable text.
                continue
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return "\n\n".join(parts)

    @classmethod
    def _compat_reasoning_text(cls, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        for key in ("reasoning", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                text = value.get("text") or value.get("summary") or ""
                if isinstance(text, str) and text.strip():
                    return text
        return cls._reasoning_details_text(message.get("reasoning_details"))

    def _calc_cost_openai_compat(
        self,
        usage: dict[str, Any],
        conf: dict[str, Any],
        model: str | None = None,
    ) -> dict[str, Any]:
        pricing = (
            (conf.get("model_pricing") or {}).get(model)
            or conf["pricing"]
        )
        input_t = usage.get("prompt_tokens", 0) or 0
        output_t = usage.get("completion_tokens", 0) or 0
        prompt_details = usage.get("prompt_tokens_details", {}) or {}
        cache_read = (
            usage.get("prompt_cache_hit_tokens")
            or prompt_details.get("cached_tokens")
            or 0
        )
        cache_creation = (
            usage.get("prompt_cache_miss_tokens")
            or prompt_details.get("cache_write_tokens")
            or 0
        )
        completion_details = usage.get("completion_tokens_details", {}) or {}
        reasoning_tokens = completion_details.get("reasoning_tokens", 0) or 0
        billable_input = max(0, input_t - cache_read)
        cached_price = pricing.get("cache_read", pricing.get("input", 0))
        calculated_cost = (
            billable_input * pricing.get("input", 0) / 1_000_000
            + output_t * pricing.get("output", 0) / 1_000_000
            + cache_read * cached_price / 1_000_000
        )
        cost, cost_source = self._resolve_usage_cost(usage, pricing, calculated_cost)
        saved = cache_read * max(0, pricing.get("input", 0) - cached_price) / 1_000_000
        return {
            "input_tokens": input_t,
            "output_tokens": output_t,
            "reasoning_tokens": reasoning_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "cost": cost,
            "cost_source": cost_source,
            "saved": round(saved, 6),
            "price_snapshot": pricing,
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Shared helpers / model discovery
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _fallback_usage(self, full_text: str, conf: dict[str, Any]) -> dict[str, Any]:
        return {
            "input_tokens": 0,
            "output_tokens": max(0, len(full_text) // 3),
            "reasoning_tokens": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "cost": 0.0,
            "cost_source": "unavailable",
            "saved": 0.0,
            "price_snapshot": conf["pricing"],
            "estimated": True,
        }

    def _raise_upstream_error(self, resp: httpx.Response, provider_name: str) -> None:
        if resp.status_code >= 400:
            self._raise_upstream_error_bytes(resp.status_code, resp.content, provider_name)

    def _raise_upstream_error_bytes(self, status_code: int, raw: bytes, provider_name: str) -> None:
        message = raw.decode("utf-8", "replace")[:1200]
        try:
            data = json.loads(message)
            err = data.get("error", data)
            if isinstance(err, dict):
                message = err.get("message") or err.get("type") or str(err)
            else:
                message = str(err)
        except Exception:
            pass
        raise RuntimeError(f"{provider_name} 返回 {status_code}: {message}")

    def _flatten_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _provider_name_from_conf(conf: dict[str, Any]) -> str:
        explicit = str(conf.get("provider_name") or "").strip()
        if explicit:
            return explicit
        for name, item in PROVIDERS.items():
            if item is conf or (item.get("base_url") == conf.get("base_url") and item.get("protocol") == conf.get("protocol")):
                return name
        return "compatible"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _join_url(base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}/{(path or '').lstrip('/')}"

    @staticmethod
    def _remote_model_allowed(
        conf: dict[str, Any], raw: dict[str, Any], model_id: str
    ) -> bool:
        prefixes = tuple(
            str(prefix) for prefix in (conf.get("model_prefixes") or []) if str(prefix)
        )
        if prefixes and not model_id.startswith(prefixes):
            return False
        required = str(conf.get("required_output_modality") or "").strip().lower()
        architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
        modalities = architecture.get("output_modalities") or raw.get("output_modalities") or []
        if required and isinstance(modalities, list) and modalities:
            return required in {str(item).lower() for item in modalities}
        return True

    async def validate_credential(self, provider: str) -> dict[str, Any]:
        """Validate auth without falling back to the built-in model catalog."""
        conf = get_provider_config(provider)
        if not conf.get("api_key"):
            return {"status": "invalid", "detail": "Key 为空"}
        path = conf.get("models_path")
        if not path:
            return {"status": "unverified", "detail": "这个通道没有轻量远端验证接口"}
        url = self._join_url(conf["base_url"], path)
        if conf.get("protocol") == "anthropic":
            headers = {
                "x-api-key": conf["api_key"],
                "anthropic-version": conf.get("api_version", "2023-06-01"),
            }
        else:
            headers = {"Authorization": f"Bearer {conf['api_key']}"}
        headers.update(conf.get("extra_headers", {}))
        try:
            resp = await self._client.get(url, headers=headers, timeout=15.0)
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            return {"status": "unverified", "detail": f"网络暂不可用：{type(exc).__name__}"}
        if resp.status_code in {401, 403}:
            return {"status": "invalid", "detail": f"远端拒绝认证（HTTP {resp.status_code}）"}
        if 200 <= resp.status_code < 300:
            return {"status": "valid", "detail": "远端认证通过"}
        if resp.status_code == 429 or resp.status_code >= 500:
            return {"status": "unverified", "detail": f"远端暂不可验证（HTTP {resp.status_code}）"}
        return {"status": "invalid", "detail": f"远端验证失败（HTTP {resp.status_code}）"}

    async def list_models(self, provider: str, force: bool = False) -> dict[str, Any]:
        conf = get_provider_config(provider)
        now = time.time()
        cached = self._models_cache.get(provider)
        if cached and not force and now - cached[0] < 300:
            return cached[1]

        merged: dict[str, dict[str, Any]] = {}
        for item in conf.get("model_catalog", []):
            if isinstance(item, str):
                item = {"id": item, "label": item}
            model_id = item.get("id")
            if model_id:
                merged[model_id] = dict(item)

        source = "static"
        warning = None
        path = conf.get("models_path")
        if path and conf.get("api_key"):
            try:
                url = self._join_url(conf["base_url"], path)
                if conf.get("auth_mode") == "bearer":
                    headers = {"Authorization": f"Bearer {conf['api_key']}"}
                elif conf.get("protocol") == "anthropic":
                    headers = {
                        "x-api-key": conf["api_key"],
                        "anthropic-version": conf.get("api_version", "2023-06-01"),
                    }
                else:
                    headers = {"Authorization": f"Bearer {conf['api_key']}"}
                headers.update(conf.get("extra_headers", {}))
                resp = await self._client.get(url, headers=headers)
                self._raise_upstream_error(resp, f"{provider} 模型列表")
                data = resp.json().get("data", [])
                for raw in data:
                    if not isinstance(raw, dict):
                        continue
                    model_id = raw.get("id") or raw.get("name")
                    if not model_id:
                        continue
                    if not self._remote_model_allowed(conf, raw, str(model_id)):
                        continue
                    item = merged.get(model_id, {"id": model_id})
                    item["label"] = raw.get("display_name") or raw.get("name") or item.get("label") or model_id
                    if raw.get("context_length") is not None:
                        item["context_length"] = raw["context_length"]
                    if isinstance(raw.get("pricing"), dict):
                        item["pricing"] = raw["pricing"]
                    item["remote"] = True
                    merged[model_id] = item
                source = "remote"
            except Exception as exc:
                diagnostics.record_error(
                    "provider_model_discovery", exc, metadata={"provider": provider}
                )
                warning = "远端模型列表读取失败，已保留本地模型清单"

        for model_id in (conf.get("default_model"), config.get_active_model(provider)):
            if model_id and model_id not in merged:
                merged[model_id] = {"id": model_id, "label": model_id}

        selected_model = config.get_active_model(provider)
        model_items = []
        for item in merged.values():
            enriched = dict(item)
            profile = get_model_capabilities(provider, enriched.get("id", ""), conf)
            enriched["brain_capabilities"] = profile.public_dict()
            if not enriched.get("context_length") and profile.context_window:
                enriched["context_length"] = profile.context_window
            model_items.append(enriched)
        payload = {
            "provider": provider,
            "display_name": conf.get("display_name", provider),
            "selected_model": selected_model,
            "default_model": conf.get("default_model", ""),
            "models": sorted(
                model_items,
                key=lambda item: (str(item.get("label", "")).lower(), item.get("id", "")),
            ),
            "source": source,
            "warning": warning,
            "custom_model_allowed": True,
            "capabilities": conf.get("capabilities", {}),
            "selected_brain_capabilities": get_model_capabilities(provider, selected_model, conf).public_dict(),
            "default_options": conf.get("default_options", {}),
        }
        self._models_cache[provider] = (now, payload)
        return payload

    def invalidate_models_cache(self, providers: list[str] | None = None) -> None:
        """Drop model discovery results produced with an older credential."""
        if providers is None:
            self._models_cache.clear()
            return
        for provider in providers:
            self._models_cache.pop(str(provider), None)

    async def invalidate_p_session(
        self, session_id: str, *, discard_native: bool = False, reason: str = ""
    ) -> None:
        await claude_code_p.invalidate_session(
            session_id, discard_native=discard_native, reason=reason
        )

    async def commit_p_visible_turn(
        self,
        session_id: str,
        *,
        generation: int,
        commit_token: str,
    ) -> bool:
        return await claude_code_p.commit_visible_turn(
            session_id,
            generation=generation,
            commit_token=commit_token,
        )

    async def close(self) -> None:
        await claude_code_p.close()
        await self._client.aclose()


gateway = Gateway()
