"""Anthropic Messages API 原生适配器，使用官方 anthropic Python SDK。"""
from __future__ import annotations

import copy
import json
import time
from typing import Any, AsyncGenerator, Awaitable, Callable

from brain_integrity import finish, omitted, record_event, sent
from model_capabilities import ModelCapabilities
from providers.http_client import make_async_http_client


class AnthropicMessagesAdapter:
    def __init__(self, conf: dict[str, Any], client: Any | None = None) -> None:
        self._provider_name = str(conf.get("provider_name") or "anthropic")
        self._display_name = str(conf.get("display_name") or "Claude")
        self._request_extra_body = copy.deepcopy(conf.get("request_extra_body") or {})
        if client is not None:
            self._client = client
            return
        try:
            from anthropic import AsyncAnthropic
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("缺少官方 anthropic SDK，请重新运行 pip install -r requirements.txt") from exc
        auth: dict[str, Any]
        if conf.get("auth_mode") == "bearer":
            auth = {"api_key": None, "auth_token": conf["api_key"]}
        else:
            auth = {"api_key": conf["api_key"], "auth_token": None}
        self._client = AsyncAnthropic(
            **auth,
            base_url=str(conf.get("base_url") or "https://api.anthropic.com").rstrip("/"),
            timeout=300.0,
            max_retries=1,
            default_headers=conf.get("extra_headers") or None,
            http_client=make_async_http_client(timeout=300.0),
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _dump(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return json.loads(json.dumps(value, default=str))

    @staticmethod
    def _show_thinking(
        kwargs: dict[str, Any], report: dict[str, Any] | None = None
    ) -> bool:
        requested = (report or {}).get("requested_options") or {}
        if "thinking_visibility" in requested:
            return (
                str(requested.get("thinking_visibility") or "full")
                .strip()
                .lower()
                == "full"
            )
        thinking = kwargs.get("thinking") or {}
        if not isinstance(thinking, dict) or thinking.get("type") == "disabled":
            return False
        return str(thinking.get("display") or "").strip().lower() != "omitted"

    @staticmethod
    def _visible_reasoning(blocks: list[dict[str, Any]]) -> str:
        """Return only plaintext thinking explicitly exposed by the API."""
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "thinking":
                continue
            text = block.get("thinking") or block.get("text") or ""
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return "\n\n".join(parts)

    @staticmethod
    def _selected_router_endpoint(metadata: Any) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {}
        endpoints = metadata.get("endpoints") or {}
        available = endpoints.get("available") if isinstance(endpoints, dict) else []
        if isinstance(available, list):
            for item in available:
                if isinstance(item, dict) and item.get("selected"):
                    return item
        return {}

    @classmethod
    def _enrich_usage_observability(
        cls,
        usage: dict[str, Any],
        *,
        response_id: Any = "",
        actual_model: Any = "",
        metadata: Any = None,
    ) -> dict[str, Any]:
        """Attach route identifiers only; never prompt/response content."""
        usage["generation_id"] = str(response_id or usage.get("generation_id") or "")
        if actual_model:
            usage["actual_model"] = str(actual_model)
        if isinstance(metadata, dict):
            selected = cls._selected_router_endpoint(metadata)
            if selected.get("provider"):
                usage["actual_provider"] = str(selected.get("provider") or "")
            if selected.get("model"):
                usage["actual_model"] = str(selected.get("model") or usage.get("actual_model") or "")
            usage["router_region"] = str(metadata.get("region") or "")
            usage["router_strategy"] = str(metadata.get("strategy") or "")
        return usage

    def build_kwargs(
        self,
        *,
        model: str,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        profile: ModelCapabilities,
        report: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        requested_max = int(options.get("max_output_tokens") or 16000)
        requested_mode = str(options.get("thinking_mode", "auto")).strip().lower()
        # Tiny hidden judgments (notably proactive in v8.2) may explicitly turn
        # reasoning off and use a true 256–512 token hard cap. Normal Claude
        # conversation requests retain the historical 1024-token floor.
        floor = 256 if requested_mode == "off" else 1024
        max_tokens = max(floor, requested_max)
        if profile.max_output_tokens:
            max_tokens = min(max_tokens, profile.max_output_tokens)
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        report["adapter"] = "official-anthropic-sdk/messages"
        if self._provider_name != "anthropic":
            report["transport"] = self._provider_name
        sent(report, "max_tokens", max_tokens)

        if self._request_extra_body:
            kwargs["extra_body"] = copy.deepcopy(self._request_extra_body)
            sent(report, "provider_routing", self._request_extra_body.get("provider", {}))

        mode = str(options.get("thinking_mode", "auto")).strip().lower()
        effort = str(options.get("reasoning_effort", profile.default_reasoning_effort or "high")).strip().lower()
        thinking_visibility = str(options.get("thinking_visibility") or "full").strip().lower()
        thinking_display = "summarized" if thinking_visibility == "full" else "omitted"
        if mode == "off":
            if "off" in profile.reasoning_modes:
                kwargs["thinking"] = {"type": "disabled"}
                sent(report, "thinking", kwargs["thinking"])
            else:
                omitted(report, "thinking_mode", "当前模型不支持关闭 thinking。")
        elif mode == "adaptive" or (mode == "auto" and profile.default_reasoning_mode == "adaptive"):
            if "adaptive" in profile.reasoning_modes:
                kwargs["thinking"] = {"type": "adaptive"}
                if self._provider_name != "anthropic":
                    kwargs["thinking"]["display"] = thinking_display
                sent(report, "thinking", kwargs["thinking"])
                if effort in profile.reasoning_efforts:
                    kwargs["output_config"] = {"effort": effort}
                    sent(report, "output_config.effort", effort)
                elif profile.reasoning_efforts:
                    omitted(report, "reasoning_effort", f"当前模型不支持 {effort}。")
            else:
                omitted(report, "thinking_mode", "当前能力卡不支持 adaptive thinking。")
        elif mode == "manual" or (mode == "auto" and profile.default_reasoning_mode == "manual"):
            if "manual" in profile.reasoning_modes:
                budget = max(1024, int(options.get("thinking_budget") or 8000))
                budget = min(budget, max_tokens - 1)
                if budget >= 1024 and budget < max_tokens:
                    kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                    if self._provider_name != "anthropic":
                        kwargs["thinking"]["display"] = thinking_display
                    sent(report, "thinking", kwargs["thinking"])
                else:
                    omitted(report, "thinking_budget", "budget_tokens 必须小于 max_tokens。")
            else:
                omitted(report, "thinking_mode", "当前能力卡不支持 manual thinking。")
        elif mode == "auto":
            omitted(report, "thinking_mode", "未知 Claude 型号采用保守自动模式，不发送 thinking 参数。")

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = {"type": "auto"}
            sent(report, "tools", len(tools))
            sent(report, "tool_choice", "auto")
        return kwargs

    async def stream(
        self,
        *,
        kwargs: dict[str, Any],
        report: dict[str, Any],
        usage_parser: Callable[[dict[str, Any]], dict[str, Any]],
        fallback_usage: Callable[[str], dict[str, Any]],
    ) -> AsyncGenerator[dict[str, Any], None]:
        full_text = ""
        raw_usage: dict[str, Any] = {}
        actual_model = kwargs.get("model")
        stop_reason: Any = None
        reasoning_announced = False
        show_thinking = self._show_thinking(kwargs, report)
        response_id = ""
        router_metadata: dict[str, Any] = {}
        content_blocks: dict[int, dict[str, Any]] = {}
        known_events = {"message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop", "ping", "error"}
        yield {"type": "status", "stage": "connecting", "label": f"连接 {self._display_name} Messages API…"}
        try:
            stream = await self._client.messages.create(**kwargs, stream=True)
            async for event in stream:
                payload = self._dump(event)
                event_type = str(payload.get("type") or getattr(event, "type", "unknown"))
                if isinstance(payload.get("openrouter_metadata"), dict):
                    router_metadata = dict(payload["openrouter_metadata"])
                record_event(report, event_type, known=event_type in known_events)
                if event_type == "message_start":
                    message = payload.get("message") or {}
                    response_id = str(message.get("id") or response_id)
                    actual_model = message.get("model") or actual_model
                    raw_usage.update(message.get("usage") or {})
                    yield {"type": "status", "stage": "reasoning", "label": "Claude 正在思考…"}
                elif event_type == "content_block_start":
                    block = payload.get("content_block") or {}
                    index = int(payload.get("index", len(content_blocks)))
                    content_blocks[index] = copy.deepcopy(block)
                    if block.get("type") in {"thinking", "redacted_thinking"} and not reasoning_announced:
                        reasoning_announced = True
                        yield {"type": "status", "stage": "reasoning", "label": "Claude 正在深度思考…"}
                elif event_type == "content_block_delta":
                    delta = payload.get("delta") or {}
                    index = int(payload.get("index", 0))
                    block = content_blocks.setdefault(index, {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            block["type"] = block.get("type") or "text"
                            block["text"] = str(block.get("text") or "") + text
                            full_text += text
                            yield {"type": "text", "text": text}
                    elif delta.get("type") == "thinking_delta":
                        block["type"] = block.get("type") or "thinking"
                        thinking_piece = str(delta.get("thinking") or "")
                        block["thinking"] = str(block.get("thinking") or "") + thinking_piece
                        if not reasoning_announced:
                            reasoning_announced = True
                            yield {"type": "status", "stage": "reasoning", "label": "Claude 正在深度思考…"}
                        if show_thinking and thinking_piece:
                            yield {"type": "thinking", "text": thinking_piece}
                    elif delta.get("type") == "signature_delta":
                        block["signature"] = str(block.get("signature") or "") + str(delta.get("signature") or "")
                elif event_type == "message_delta":
                    raw_usage.update(payload.get("usage") or {})
                    delta = payload.get("delta") or {}
                    stop_reason = delta.get("stop_reason") or stop_reason
                elif event_type == "error":
                    raise RuntimeError(f"Anthropic 流式错误: {payload.get('error') or payload}")
        finally:
            await self.close()
        usage = usage_parser(raw_usage) if raw_usage else fallback_usage(full_text)
        if isinstance(report.get("cache_diagnostics"), dict):
            usage.update(report["cache_diagnostics"])
        self._enrich_usage_observability(
            usage, response_id=response_id, actual_model=actual_model, metadata=router_metadata
        )
        usage["native_protocol"] = "messages"
        usage["integrity"] = finish(report, actual_model=actual_model, stop_reason=stop_reason or "end_turn", usage=usage)
        native_content = [content_blocks[index] for index in sorted(content_blocks) if content_blocks[index].get("type")]
        envelope = ({"api_family": "messages", "response_id": response_id,
                     "content": native_content, "stop_reason": stop_reason or "end_turn"}
                    if native_content else None)
        done = {"type": "done", "usage": usage, "native_envelope": envelope}
        reasoning_content = self._visible_reasoning(native_content) if show_thinking else ""
        if reasoning_content:
            done["reasoning_content"] = reasoning_content
        yield done

    async def request(
        self,
        *,
        kwargs: dict[str, Any],
        report: dict[str, Any],
        usage_parser: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            response = await self._client.messages.create(**kwargs)
            data = self._dump(response)
        finally:
            await self.close()
        text = "".join(block.get("text") or "" for block in data.get("content", []) or [] if isinstance(block, dict) and block.get("type") == "text")
        usage = usage_parser(data.get("usage") or {})
        self._enrich_usage_observability(
            usage, response_id=data.get("id") or "",
            actual_model=data.get("model") or kwargs.get("model"),
            metadata=data.get("openrouter_metadata"),
        )
        usage["native_protocol"] = "messages"
        usage["integrity"] = finish(report, actual_model=data.get("model") or kwargs.get("model"), stop_reason=data.get("stop_reason") or "end_turn", usage=usage)
        blocks = [item for item in data.get("content", []) or [] if isinstance(item, dict)]
        envelope = ({"api_family": "messages", "response_id": data.get("id") or "",
                     "content": blocks, "stop_reason": data.get("stop_reason") or "end_turn"}
                    if blocks else None)
        return {
            "content": text,
            "reasoning_content": (
                self._visible_reasoning(blocks)
                if self._show_thinking(kwargs, report)
                else ""
            ),
            "model": data.get("model") or kwargs.get("model"),
            "provider": self._provider_name,
            "usage": usage,
            "native_envelope": envelope,
        }

    async def tool_loop(
        self,
        *,
        kwargs: dict[str, Any],
        report: dict[str, Any],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[Any]],
        max_rounds: int,
        observer: Callable[[dict[str, Any]], Awaitable[Any]],
        usage_parser: Callable[[dict[str, Any]], dict[str, Any]],
        add_usage: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
        message_preparer: (
            Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None
        ) = None,
    ) -> dict[str, Any]:
        messages = list(kwargs.pop("messages"))
        total_usage: dict[str, Any] = {}
        tool_log: list[dict[str, Any]] = []
        final_text = ""
        reasoning_parts: list[str] = []
        actual_model = kwargs.get("model")
        stop_reason: Any = None
        last_envelope: dict[str, Any] | None = None
        try:
            for round_index in range(max(1, min(max_rounds, 8))):
                # A tool round appends assistant/tool_result blocks, so the
                # previous request boundary moves every time. Let the gateway
                # recompute its two overlapping history breakpoints immediately
                # before each request instead of freezing first-round markers.
                request_messages = (
                    message_preparer(messages)
                    if message_preparer is not None
                    else messages
                )
                round_kwargs = kwargs
                if round_index > 0 and isinstance(kwargs.get("tools"), list):
                    # Tool follow-ups are deliberately not cache-schema writes.
                    # Keep the schema identical but strip explicit tool breakpoints
                    # after the first independent tool-lane request.
                    round_kwargs = copy.deepcopy(kwargs)
                    for tool in round_kwargs.get("tools") or []:
                        if isinstance(tool, dict):
                            tool.pop("cache_control", None)
                response = await self._client.messages.create(
                    **round_kwargs, messages=request_messages
                )
                data = self._dump(response)
                actual_model = data.get("model") or actual_model
                stop_reason = data.get("stop_reason") or stop_reason
                round_usage = usage_parser(data.get("usage") or {})
                self._enrich_usage_observability(
                    round_usage, response_id=data.get("id") or "",
                    actual_model=data.get("model") or actual_model,
                    metadata=data.get("openrouter_metadata"),
                )
                add_usage(total_usage, round_usage)
                blocks = [x for x in data.get("content", []) or [] if isinstance(x, dict)]
                if self._show_thinking(kwargs, report):
                    visible_reasoning = self._visible_reasoning(blocks)
                    if visible_reasoning:
                        reasoning_parts.append(visible_reasoning)
                last_envelope = ({"api_family": "messages", "response_id": data.get("id") or "",
                                  "content": blocks, "stop_reason": data.get("stop_reason") or "end_turn"}
                                 if blocks else None)
                final_text = "".join(x.get("text") or "" for x in blocks if x.get("type") == "text")
                calls = [x for x in blocks if x.get("type") == "tool_use"]
                if not calls:
                    total_usage["native_protocol"] = "messages+tools"
                    total_usage["integrity"] = finish(report, actual_model=actual_model, stop_reason=stop_reason or "end_turn", usage=total_usage, tool_calls=len(tool_log))
                    return {
                        "content": final_text,
                        "reasoning_content": "\n\n".join(reasoning_parts),
                        "model": actual_model,
                        "provider": self._provider_name,
                        "usage": total_usage,
                        "tool_calls": tool_log,
                        "native_envelope": last_envelope,
                    }
                messages.append({"role": "assistant", "content": blocks})
                result_blocks: list[dict[str, Any]] = []
                for call in calls:
                    name = call.get("name") or ""
                    args = call.get("input") if isinstance(call.get("input"), dict) else {}
                    started = time.perf_counter()
                    ok = True
                    try:
                        result = await tool_executor(name, args)
                    except Exception as exc:
                        ok = False
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                    item = {"name": name, "arguments": args, "result": result, "duration_ms": round((time.perf_counter()-started)*1000, 2), "ok": ok}
                    tool_log.append(item)
                    await observer(item)
                    result_blocks.append({"type": "tool_result", "tool_use_id": call.get("id"), "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str), "is_error": not ok})
                messages.append({"role": "user", "content": result_blocks})
        finally:
            await self.close()
        total_usage["native_protocol"] = "messages+tools"
        total_usage["integrity"] = finish(report, actual_model=actual_model, stop_reason=stop_reason or "max_tool_rounds", usage=total_usage, tool_calls=len(tool_log))
        return {
            "content": final_text or "工具调用达到上限，我已经停止继续调用。",
            "reasoning_content": "\n\n".join(reasoning_parts),
            "model": actual_model,
            "provider": self._provider_name,
            "usage": total_usage,
            "tool_calls": tool_log,
            "incomplete": True,
            "native_envelope": last_envelope,
        }
