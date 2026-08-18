"""具体模型能力注册表。

原则：前端只显示当前模型真实支持的控制项；不认识的模型采用保守配置，
避免把不受支持的参数发给上游，造成假开关或 400 错误。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    api_family: str
    source: str = "provider-conservative"
    reasoning_modes: tuple[str, ...] = ()
    reasoning_efforts: tuple[str, ...] = ()
    reasoning_contexts: tuple[str, ...] = ()
    default_reasoning_mode: str = "auto"
    default_reasoning_effort: str = "auto"
    supports_verbosity: bool = False
    supports_tools: bool = False
    supports_images: bool = False
    supports_pdf: bool = False
    supports_local_files: bool = False
    supports_prompt_cache: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reasoning_control(self) -> bool:
        return bool(self.reasoning_modes or self.reasoning_efforts)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasoning_control"] = self.reasoning_control
        data["reasoning_modes"] = list(self.reasoning_modes)
        data["reasoning_efforts"] = list(self.reasoning_efforts)
        data["reasoning_contexts"] = list(self.reasoning_contexts)
        data["notes"] = list(self.notes)
        return data


def _tail(model: str) -> str:
    return (model or "").lower().split("/")[-1]


def _openai(model: str) -> ModelCapabilities:
    m = _tail(model)
    common = dict(
        provider="openai",
        model=model,
        api_family="responses",
        supports_tools=True,
        supports_images=True,
        supports_pdf=True,
        supports_prompt_cache=True,
    )
    if m.startswith("gpt-5") or m == "gpt-latest":
        is_56 = m.startswith("gpt-5.6") or m == "gpt-latest"
        return ModelCapabilities(
            **common,
            source="builtin-pattern:gpt-5",
            reasoning_modes=("auto", "off", "on", *(("pro",) if is_56 else ())),
            reasoning_efforts=(
                "auto", "none", "minimal", "low", "medium", "high", "xhigh",
                *(("max",) if is_56 else ()),
            ),
            reasoning_contexts=("auto", "current_turn", "all_turns") if is_56 else (),
            default_reasoning_effort="auto",
            supports_verbosity=True,
            context_window=400_000,
            max_output_tokens=128_000,
        )
    if m.startswith(("o1", "o3", "o4")):
        return ModelCapabilities(
            **common,
            source="builtin-pattern:o-series",
            reasoning_modes=("auto", "on"),
            reasoning_efforts=("auto", "low", "medium", "high"),
            default_reasoning_effort="medium",
            context_window=200_000,
            max_output_tokens=100_000,
        )
    if m.startswith(("gpt-4o", "gpt-4.1", "gpt-4")):
        return ModelCapabilities(
            **common,
            source="builtin-pattern:non-reasoning-gpt",
            context_window=128_000,
            max_output_tokens=16_384,
            notes=("该模型不接收 reasoning 参数。",),
        )
    return ModelCapabilities(
        **common,
        notes=("未识别到具体 OpenAI 模型能力，采用保守模式：不发送推理控制参数。",),
    )


def _anthropic(model: str) -> ModelCapabilities:
    m = _tail(model)
    # Anthropic 直连常用 4-8，而 OpenRouter slug 使用 4.8；能力判断把两种
    # 写法归一化，实际发送的 model ID 保持原样。
    m_normalized = m.replace(".", "-")
    common = dict(
        provider="anthropic",
        model=model,
        api_family="messages",
        supports_tools=True,
        supports_images=True,
        supports_pdf=True,
        supports_prompt_cache=True,
        # Sonnet 5 exposes a 1M window by default; older/current sibling
        # families keep the conservative 200K capability unless overridden by
        # a concrete model catalog entry.
        context_window=(
            1_000_000
            if m.startswith("claude-sonnet-5") or m == "claude-sonnet-latest"
            else 200_000
        ),
    )
    adaptive_markers = (
        "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
        "claude-opus-4-6", "claude-sonnet-4-6", "claude-fable-5",
        "claude-mythos", "claude-sonnet-latest", "claude-fable-latest",
    )
    if any(x in m_normalized for x in adaptive_markers):
        return ModelCapabilities(
            **common,
            source="builtin-pattern:claude-adaptive",
            reasoning_modes=("auto", "adaptive"),
            reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
            default_reasoning_mode="adaptive",
            default_reasoning_effort="high",
            max_output_tokens=128_000,
            notes=("使用 adaptive thinking；不发送 budget_tokens。",),
        )
    manual_markers = (
        "claude-opus-4-5", "claude-haiku-4-5", "claude-sonnet-4-5",
        "claude-opus-4-1", "claude-sonnet-4-2025", "claude-opus-4-2025",
    )
    if any(x in m_normalized for x in manual_markers):
        return ModelCapabilities(
            **common,
            source="builtin-pattern:claude-manual",
            reasoning_modes=("auto", "off", "manual"),
            default_reasoning_mode="manual",
            max_output_tokens=64_000,
            notes=("使用 manual extended thinking；budget_tokens 必须小于 max_tokens。",),
        )
    return ModelCapabilities(
        **common,
        reasoning_modes=("auto", "off"),
        notes=("未识别到具体 Claude 型号，自动模式不会强行发送 thinking 参数。",),
    )


def _deepseek(model: str) -> ModelCapabilities:
    """DeepSeek V4 的官方 Chat Completions 能力卡。

    DeepSeek 的公开聊天 API 当前是文字协议；资料工作室会先在本机提取
    PDF/Office/EPUB 文字，不把“本地可读”误标成“上游原生文件”。
    """
    m = _tail(model)
    if m in {"deepseek-v4-pro", "deepseek-v4-flash"}:
        return ModelCapabilities(
            provider="deepseek",
            model=model,
            api_family="deepseek_chat_completions",
            source="builtin:deepseek-v4-official",
            reasoning_modes=("enabled", "disabled"),
            reasoning_efforts=("high", "max"),
            default_reasoning_mode="enabled",
            default_reasoning_effort="max" if m.endswith("pro") else "high",
            supports_tools=True,
            supports_local_files=True,
            supports_prompt_cache=True,
            context_window=1_000_000,
            max_output_tokens=393_216,
            notes=(
                "思考内容不展示；工具回合会原样续接 reasoning_content。",
                "图片与扫描件没有原生视觉输入，文档由本机提取文字。",
            ),
        )
    return ModelCapabilities(
        provider="deepseek",
        model=model,
        api_family="deepseek_chat_completions",
        source="deepseek-conservative",
        supports_local_files=True,
        supports_prompt_cache=True,
        notes=("未知 DeepSeek 型号采用保守模式，不发送思考控制参数。",),
    )


def _compatible(provider: str, model: str, provider_conf: dict[str, Any] | None) -> ModelCapabilities:
    conf = provider_conf or {}
    caps = conf.get("capabilities", {}) or {}
    return ModelCapabilities(
        provider=provider,
        model=model,
        api_family="chat_completions_compatible",
        source="provider-config",
        supports_tools=bool(caps.get("tools_ready")),
        supports_images=bool(caps.get("vision_ready")),
        supports_pdf=bool(caps.get("files_ready")),
        supports_prompt_cache=bool(conf.get("supports_cache")),
        notes=("兼容接口能力由服务商实现决定；不会套用 GPT/Claude 的原生参数。",),
    )



def _claude_code_p(model: str, provider_conf: dict[str, Any] | None) -> ModelCapabilities:
    """Capabilities of the local Claude Code subscription transport.

    Claude Code itself may have tools and skills, but JTYHome's default P-mode
    deliberately launches with ``--tools ""`` and keeps application diagnostics
    in the existing local-prefetch path. PDF/document attachments therefore use
    local text extraction; image blocks can be passed through stream-json.
    """
    conf = provider_conf or {}
    return ModelCapabilities(
        provider="claude_code_p",
        model=model,
        api_family="claude_code_stream_json",
        source="builtin:claude-code-p",
        reasoning_modes=("auto", "on"),
        reasoning_efforts=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_mode="auto",
        default_reasoning_effort="high",
        supports_tools=bool((conf.get("capabilities") or {}).get("tools_ready")),
        supports_images=True,
        supports_pdf=False,
        supports_local_files=True,
        supports_prompt_cache=True,
        context_window=None,
        max_output_tokens=None,
        notes=(
            "主聊天是一聊天一常驻 Claude Code -p 进程；上下文由进程维持并可自动 compact。",
            "P 模式使用订阅 OAuth；子进程会剔除 ANTHROPIC_API_KEY。",
            "PDF/Office 由本机提取文字；图片可走 stream-json content block。",
        ),
    )

def get_model_capabilities(
    provider: str,
    model: str,
    provider_conf: dict[str, Any] | None = None,
) -> ModelCapabilities:
    p = (provider or "").lower()
    conf = provider_conf or {}
    capability_provider = str(conf.get("capability_provider") or p).lower()
    if p == "claude_code_p":
        profile = _claude_code_p(model, conf)
    elif capability_provider == "openai":
        profile = _openai(model)
    elif capability_provider == "anthropic":
        profile = _anthropic(model)
    elif capability_provider == "deepseek":
        profile = _deepseek(model)
    else:
        profile = _compatible(p, model, conf)

    # model_catalog 可对具体模型做显式覆盖，优先于模式推断。
    for item in conf.get("model_catalog", []):
        if not isinstance(item, dict) or item.get("id") != model:
            continue
        override = item.get("capabilities")
        if not isinstance(override, dict):
            break
        data = profile.public_dict()
        data.pop("reasoning_control", None)
        for key, value in override.items():
            if key in {"reasoning_modes", "reasoning_efforts", "reasoning_contexts", "notes"} and isinstance(value, list):
                value = tuple(value)
            if key in data:
                data[key] = value
        for key in ("reasoning_modes", "reasoning_efforts", "reasoning_contexts", "notes"):
            if isinstance(data.get(key), list):
                data[key] = tuple(data[key])
        data["source"] = f"model-catalog:{model}"
        profile = ModelCapabilities(**data)
        break

    # 同一种原生协议经过聚合层时，参数范围不一定与直连完全相同。
    # 过滤器只会收窄已有能力，不会给本来不支持推理的模型凭空加开关。
    filters = conf.get("capability_filters") or {}
    if isinstance(filters, dict) and filters:
        data = profile.public_dict()
        data.pop("reasoning_control", None)
        filtered = False
        for key in ("reasoning_modes", "reasoning_efforts", "reasoning_contexts"):
            allowed = filters.get(key)
            if not isinstance(allowed, (list, tuple, set)):
                continue
            original = tuple(data.get(key) or ())
            data[key] = tuple(value for value in original if value in allowed)
            filtered = filtered or data[key] != original
        if data.get("default_reasoning_mode") not in {"auto", *(data.get("reasoning_modes") or ())}:
            data["default_reasoning_mode"] = "auto"
        if data.get("default_reasoning_effort") not in {"auto", *(data.get("reasoning_efforts") or ())}:
            data["default_reasoning_effort"] = "auto"
        if filtered:
            data["source"] = f"{data.get('source', 'capability')}+transport-filter"
        if isinstance(data.get("notes"), list):
            data["notes"] = tuple(data["notes"])
        profile = ModelCapabilities(**data)

    extra_notes = conf.get("capability_notes") or []
    if isinstance(extra_notes, (list, tuple)) and extra_notes:
        profile = replace(
            profile,
            notes=tuple(profile.notes) + tuple(str(note) for note in extra_notes if str(note).strip()),
        )

    # provider 字段记录实际计费/通道路由，api_family 和能力仍来自原生协议族。
    if profile.provider != p:
        profile = replace(profile, provider=p, source=f"{profile.source}+via:{p}")
    return profile
