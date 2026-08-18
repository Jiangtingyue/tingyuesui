"""OpenAI Responses API 原生适配器。

使用官方 openai Python SDK；大西瓜只把事件翻译成自己的 UI 事件，
不会把 Responses API 降格为 Chat Completions。
"""
from __future__ import annotations

import copy
import json
from typing import Any, AsyncGenerator, Awaitable, Callable

from brain_integrity import finish, omitted, record_event, sent
from model_capabilities import ModelCapabilities
from providers.http_client import make_async_http_client


class OpenAIResponsesAdapter:
    def __init__(self, conf: dict[str, Any], client: Any | None = None) -> None:
        self._provider_name = str(conf.get("provider_name") or "openai")
        self._display_name = str(conf.get("display_name") or "OpenAI")
        self._request_extra_body = copy.deepcopy(conf.get("request_extra_body") or {})
        if client is not None:
            self._client = client
            return
        try:
            from openai import AsyncOpenAI
        except Exception as exc:  # pragma: no cover - exercised in deployment error path
            raise RuntimeError("缺少官方 openai SDK，请重新运行 pip install -r requirements.txt") from exc
        base_url = str(conf.get("base_url") or "https://api.openai.com").rstrip("/")
        if conf.get("chat_path", "").startswith("/v1/") and not base_url.endswith("/v1"):
            base_url += "/v1"
        self._client = AsyncOpenAI(
            api_key=conf["api_key"],
            base_url=base_url,
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
    def _show_thinking(report: dict[str, Any]) -> bool:
        options = report.get("requested_options") or {}
        return str(options.get("thinking_visibility") or "full").strip().lower() == "full"

    @staticmethod
    def _visible_reasoning(output: list[dict[str, Any]]) -> str:
        """Extract provider-exposed reasoning text/summary, never encrypted data."""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            summary = item.get("summary") or []
            if isinstance(summary, str):
                summary = [summary]
            for block in summary if isinstance(summary, list) else []:
                if isinstance(block, str) and block.strip():
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text") or block.get("summary") or ""
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
            content = item.get("content") or []
            if isinstance(content, str):
                content = [content]
            for block in content if isinstance(content, list) else []:
                if isinstance(block, str) and block.strip():
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text") or block.get("reasoning") or ""
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
        return "\n\n".join(parts)

    def build_kwargs(
        self,
        *,
        model: str,
        instructions: str,
        input_items: list[dict[str, Any]],
        options: dict[str, Any],
        profile: ModelCapabilities,
        report: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
        }
        report["adapter"] = "official-openai-sdk/responses"
        if self._provider_name != "openai":
            report["transport"] = self._provider_name
        sent(report, "store", False)
        if self._request_extra_body:
            kwargs["extra_body"] = copy.deepcopy(self._request_extra_body)
            sent(report, "provider_routing", self._request_extra_body.get("provider", {}))

        max_output = int(options.get("max_output_tokens") or 0)
        if max_output > 0:
            if profile.max_output_tokens:
                max_output = min(max_output, profile.max_output_tokens)
            kwargs["max_output_tokens"] = max_output
            sent(report, "max_output_tokens", max_output)

        requested_mode = str(options.get("thinking_mode", "auto")).strip().lower()
        requested_effort = str(options.get("reasoning_effort", "auto")).strip().lower()
        if profile.reasoning_efforts:
            # store:false 下仍把不可读的加密推理项交还本机；下一轮只会原样
            # 回放给同一 OpenAI 模型，不向 UI 或其他模型暴露。
            kwargs["include"] = ["reasoning.encrypted_content"]
            sent(report, "include", kwargs["include"])
            reasoning: dict[str, Any] = {}
            if requested_mode == "pro":
                if "pro" in profile.reasoning_modes:
                    reasoning["mode"] = "pro"
                    if requested_effort != "auto":
                        omitted(report, "reasoning_effort", "Pro 模式由模型管理推理预算，不再叠加 effort。")
                    requested_effort = "auto"
                else:
                    omitted(report, "thinking_mode", "当前模型不支持 Pro 推理模式。")
            elif requested_mode == "off":
                if "none" in profile.reasoning_efforts:
                    requested_effort = "none"
                else:
                    omitted(report, "thinking_mode", "当前模型不能关闭推理。")
            elif requested_mode not in {"", "auto", "on"}:
                omitted(report, "thinking_mode", f"当前模型不支持 {requested_mode} 模式。")

            if requested_effort == "auto":
                omitted(report, "reasoning_effort", "使用模型默认推理深度。")
            elif requested_effort in profile.reasoning_efforts:
                reasoning["effort"] = requested_effort
            else:
                omitted(report, "reasoning_effort", f"当前模型不支持 {requested_effort}。")
            requested_context = str(options.get("reasoning_context", "auto")).strip().lower()
            if requested_context in profile.reasoning_contexts and requested_context != "auto":
                reasoning["context"] = requested_context
            elif requested_context not in {"", "auto"}:
                omitted(report, "reasoning_context", "当前模型能力卡未确认支持该持久推理模式。")
            if self._provider_name == "openrouter_gpt" and self._show_thinking(report):
                # OpenRouter Responses exposes readable reasoning through its
                # summary stream/output; encrypted_content remains continuity
                # data and is never presented as text in the UI.
                reasoning["summary"] = "auto"
            if reasoning:
                kwargs["reasoning"] = reasoning
                sent(report, "reasoning", reasoning)
        elif requested_effort not in {"", "auto", "none", "off"}:
            omitted(report, "reasoning_effort", "当前模型能力卡不支持 reasoning 参数。")

        verbosity = str(options.get("verbosity", "auto")).strip().lower()
        if profile.supports_verbosity and verbosity in {"low", "medium", "high"}:
            kwargs["text"] = {"verbosity": verbosity}
            sent(report, "text.verbosity", verbosity)
        elif verbosity not in {"", "auto"}:
            omitted(report, "verbosity", "当前模型不支持 Responses verbosity。")

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            kwargs["parallel_tool_calls"] = True
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
        final_usage: dict[str, Any] | None = None
        actual_model = kwargs.get("model")
        stop_reason: Any = None
        native_envelope: dict[str, Any] | None = None
        reasoning_text = ""
        completed_reasoning = ""
        show_thinking = self._show_thinking(report)
        known_events = {
            "response.created", "response.in_progress", "response.completed",
            "response.failed", "response.incomplete", "response.output_item.added",
            "response.output_item.done", "response.content_part.added",
            "response.content_part.done", "response.output_text.delta",
            "response.output_text.done", "response.reasoning_summary_text.delta",
            "response.reasoning_summary_text.done", "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done", "response.reasoning.delta",
            "response.reasoning.done", "error",
        }
        yield {"type": "status", "stage": "connecting", "label": f"连接 {self._display_name} Responses API…"}
        try:
            stream = await self._client.responses.create(**kwargs, stream=True)
            async for event in stream:
                payload = self._dump(event)
                event_type = str(payload.get("type") or getattr(event, "type", "unknown"))
                record_event(report, event_type, known=event_type in known_events)
                if event_type == "response.created":
                    response = payload.get("response") or {}
                    actual_model = response.get("model") or actual_model
                    yield {"type": "status", "stage": "reasoning", "label": "GPT 正在思考…"}
                elif event_type == "response.output_text.delta":
                    text = payload.get("delta") or ""
                    if text:
                        full_text += text
                        yield {"type": "text", "text": text}
                elif event_type in {
                    "response.reasoning_summary_text.delta",
                    "response.reasoning.delta",
                }:
                    reasoning_piece = str(payload.get("delta") or "")
                    if show_thinking and reasoning_piece:
                        reasoning_text += reasoning_piece
                        yield {"type": "thinking", "text": reasoning_piece}
                    yield {"type": "status", "stage": "reasoning", "label": "GPT 正在深度推理…"}
                elif event_type.startswith("response.reasoning_summary") or event_type.startswith("response.reasoning."):
                    yield {"type": "status", "stage": "reasoning", "label": "GPT 正在深度推理…"}
                elif event_type == "response.completed":
                    response = payload.get("response") or {}
                    actual_model = response.get("model") or actual_model
                    stop_reason = response.get("status") or "completed"
                    final_usage = usage_parser(response.get("usage") or {})
                    output = [item for item in response.get("output", []) or [] if isinstance(item, dict)]
                    if show_thinking:
                        completed_reasoning = self._visible_reasoning(output)
                    if output:
                        native_envelope = {
                            "api_family": "responses",
                            "response_id": response.get("id") or "",
                            "output": output,
                        }
                elif event_type in {"response.failed", "response.incomplete"}:
                    response = payload.get("response") or {}
                    stop_reason = response.get("status") or event_type
                    error = response.get("error") or response.get("incomplete_details") or payload
                    raise RuntimeError(f"OpenAI 响应未完成: {error}")
                elif event_type == "error":
                    error = payload.get("error") or payload
                    raise RuntimeError(f"OpenAI 流式错误: {error}")
        finally:
            await self.close()

        if final_usage is None:
            final_usage = fallback_usage(full_text)
        final_usage["native_protocol"] = "responses"
        final_usage["integrity"] = finish(
            report,
            actual_model=actual_model,
            stop_reason=stop_reason or "completed",
            usage=final_usage,
        )
        done = {"type": "done", "usage": final_usage, "native_envelope": native_envelope}
        visible_reasoning = reasoning_text or completed_reasoning
        if show_thinking and visible_reasoning:
            done["reasoning_content"] = visible_reasoning
        yield done

    async def request(
        self,
        *,
        kwargs: dict[str, Any],
        report: dict[str, Any],
        usage_parser: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            response = await self._client.responses.create(**kwargs)
            data = self._dump(response)
        finally:
            await self.close()
        text = data.get("output_text") or ""
        if not text:
            parts: list[str] = []
            for item in data.get("output", []) or []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for block in item.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        parts.append(block.get("text") or "")
            text = "".join(parts)
        usage = usage_parser(data.get("usage") or {})
        usage["native_protocol"] = "responses"
        usage["integrity"] = finish(
            report,
            actual_model=data.get("model") or kwargs.get("model"),
            stop_reason=data.get("status") or "completed",
            usage=usage,
        )
        output = [item for item in data.get("output", []) or [] if isinstance(item, dict)]
        envelope = ({"api_family": "responses", "response_id": data.get("id") or "", "output": output}
                    if output else None)
        return {
            "content": text,
            "reasoning_content": (
                self._visible_reasoning(output) if self._show_thinking(report) else ""
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
    ) -> dict[str, Any]:
        input_items = list(kwargs.pop("input"))
        total_usage: dict[str, Any] = {}
        tool_log: list[dict[str, Any]] = []
        final_text = ""
        reasoning_parts: list[str] = []
        actual_model = kwargs.get("model")
        stop_reason: Any = None
        last_envelope: dict[str, Any] | None = None
        try:
            for _ in range(max(1, min(max_rounds, 8))):
                response = await self._client.responses.create(**kwargs, input=input_items)
                data = self._dump(response)
                actual_model = data.get("model") or actual_model
                stop_reason = data.get("status") or stop_reason
                add_usage(total_usage, usage_parser(data.get("usage") or {}))
                outputs = [x for x in data.get("output", []) or [] if isinstance(x, dict)]
                if self._show_thinking(report):
                    visible_reasoning = self._visible_reasoning(outputs)
                    if visible_reasoning:
                        reasoning_parts.append(visible_reasoning)
                last_envelope = ({"api_family": "responses", "response_id": data.get("id") or "",
                                  "output": outputs} if outputs else None)
                calls = [x for x in outputs if x.get("type") == "function_call"]
                parts: list[str] = []
                for item in outputs:
                    if item.get("type") != "message":
                        continue
                    for block in item.get("content", []) or []:
                        if isinstance(block, dict) and block.get("type") == "output_text":
                            parts.append(block.get("text") or "")
                final_text = "".join(parts)
                if not calls:
                    total_usage["native_protocol"] = "responses+tools"
                    total_usage["integrity"] = finish(report, actual_model=actual_model, stop_reason=stop_reason or "completed", usage=total_usage, tool_calls=len(tool_log))
                    return {
                        "content": final_text,
                        "reasoning_content": "\n\n".join(reasoning_parts),
                        "model": actual_model,
                        "provider": self._provider_name,
                        "usage": total_usage,
                        "tool_calls": tool_log,
                        "native_envelope": last_envelope,
                    }
                input_items.extend(outputs)
                for call in calls:
                    raw = call.get("arguments") or "{}"
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                    except Exception:
                        args = {"_raw": str(raw)}
                    name = call.get("name") or ""
                    import time
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
                    input_items.append({"type": "function_call_output", "call_id": call.get("call_id") or call.get("id"), "output": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)})
        finally:
            await self.close()
        total_usage["native_protocol"] = "responses+tools"
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
