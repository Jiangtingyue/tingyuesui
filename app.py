"""大西瓜 FastAPI 主应用：页面、API、流式聊天与后台节律统一运行。"""
import os
import base64
import copy
import json
import uuid
import re
import asyncio
import time
import math
import shutil
import tempfile
import importlib.metadata
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse, Response
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from config import (
    get_provider_config, get_active_model, switch_provider, switch_model,
    PROVIDERS, PUSH_CONFIG
)
from models import (
    get_db, create_session, save_message, get_session_messages, get_session_message_page,
    get_session_messages_after, get_session_context_messages,
    select_recent_context_messages,
    estimate_context_tokens_from_lengths,
    calculate_claude_cache_metrics,
    get_sessions, rename_session, delete_session, get_token_stats, get_detailed_stats,
    branch_session_before_message, get_session,
    enrich_cache_diagnostic_rows,
)
from gateway import gateway
from cache_keepalive import cache_keepalive
from pipeline import pipeline                      # v5 完整记忆管线（替代 vector_search v1）
from vector_search_v2 import vector_search_v2      # 手动记忆API用
from integration_patch import apply_patch          # Ombre Brain 路由 + decay 任务
from memory_optimizer import init_jieba_fts5       # jieba FTS5 建表
from decay_engine import run_decay_cycle
import music_artwork
from push_service import push_service
from proactive import proactive
from screen_time import screen_tracker
from mcp_handler import mcp_handler
from desire_host import desire_host
from diagnostics import diagnostics, StageTimer
from tool_bridge import tool_bridge
from attachment_service import attachment_service
from webpage_service import webpage_service
from audio_analysis import ocean_listen
from sticker_service import sticker_service
from memory_archive import memory_archive
from natural_memory import natural_memory
from affect_core import affect_core
from plugin_registry import plugin_registry
from model_capabilities import get_model_capabilities
from brain_integrity import start_report
from context_composer import ContextBlock, context_composer
from context_compactor import context_compactor
from context_budgeter import plan_main_context
from character_integrity import character_integrity
from native_continuity import native_continuity
from living_state import living_state
from inner_state import inner_state
from morning_response import morning_response
from relationship_continuity import relationship_continuity
from file_workspace import file_workspace
from thinking_vault import thinking_vault
from local_data_hub import local_data_hub
from local_service import local_service
from style_profiles import style_profiles
from conversation_import import (
    extract_foundation_bytes, import_bundle, parse_conversation_bytes,
    preview_bundle,
)
import water_scene


_ANTHROPIC_REPLAY_META_KEY = "anthropic_replay_v1"

def _anthropic_replay_content(metadata: dict | None):
    """Return provider-canonical historical content saved from a prior turn.

    The replay snapshot contains only stable conversational blocks. Volatile
    <application_turn_context>, cache markers and one-turn binary attachments
    are never persisted here.
    """
    meta = metadata if isinstance(metadata, dict) else {}
    payload = meta.get(_ANTHROPIC_REPLAY_META_KEY)
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    clean = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            # Native image/document turns are intentionally not made rolling
            # history checkpoints; replaying base64 in metadata would bloat DB.
            return None
        text = str(block.get("text") or "")
        if text.lstrip().startswith("<application_turn_context>"):
            break
        clean.append({"type": "text", "text": text})
    return copy.deepcopy(clean) if clean else None

def _anthropic_text_replay_payload(content: str) -> dict:
    return {
        "version": 1,
        "content": [{"type": "text", "text": str(content or "")}],
    }

def _persist_anthropic_replay(message_id: int, metadata: dict, content: str) -> None:
    """Seed an immutable text replay for older pre-8.2.1 messages once.

    This mutates only message metadata; visible content and memory archives are
    untouched. Once seeded, later turns read the exact same block array instead
    of reconstructing it from attachment/web metadata.
    """
    if not message_id or not content:
        return
    meta = dict(metadata or {})
    if _anthropic_replay_content(meta) is not None:
        return
    meta[_ANTHROPIC_REPLAY_META_KEY] = _anthropic_text_replay_payload(content)
    with get_db() as db:
        db.execute(
            "UPDATE messages SET metadata=? WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), int(message_id)),
        )
        db.commit()
from history_migration import history_migration
from persona_cards import persona_cards
from voice_service import VoiceServiceError, voice_service
from access_control import access_control
from co_presence import co_presence
from persona_context_gate import persona_context_gate
from api.reliability_routes import router as reliability_router
from chat_reliability import (
    InvalidClientRequestId, RequestIdentityConflict, chat_request_runtime,
    chat_request_store, normalize_client_request_id, replay_request_events,
    resilient_request_events,
)
from relational_honesty import (
    BLOCKED_FALLBACK, REWRITE_SYSTEM_PROMPT, RULE_VERSION, merge_rewrite_usage,
    relational_honesty_guard,
)
from api_cost import apply_runaway_output_guard, public_auxiliary_status
from schema_migrations import run_schema_migrations
from runtime_paths import DATA_DIR
import dwell_life
import life_memory_bridge


# ── 定时任务 ──
scheduler = AsyncIOScheduler()

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_MAX_CHAT_CHARS = max(
    1000, min(config._env_int("DAXIGUA_MAX_CHAT_CHARS", 120000, min_value=1000, max_value=500000), 500000)
)


def _validated_session_id(value, *, default: str = "") -> str:
    session_id = str(value or default).strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="session_id 格式无效")
    return session_id


def _public_model_request_error(exc: Exception) -> dict[str, str]:
    """Translate common upstream failures without echoing secrets or raw payloads."""
    raw = str(exc or "")
    lowered = raw.lower()
    exc_name = type(exc).__name__.lower()
    exc_code = str(getattr(exc, "code", "") or "")

    if exc_code == "claude_code_cli_missing":
        return {
            "code": "claude_code_cli_missing",
            "message": "没有找到 Claude Code CLI。请先在这台电脑安装 Claude Code，并运行 claude auth login 登录订阅账号。",
        }
    if (
        "not logged in" in lowered
        or "please run /login" in lowered
        or "claude login" in lowered
        or "claude auth login" in lowered
    ):
        return {
            "code": "claude_code_not_logged_in",
            "message": "Claude Code P 模式没有可用的订阅登录。请在这台电脑终端运行 claude auth login 后重试。",
        }
    if exc_code == "claude_code_not_subscription_auth":
        return {
            "code": "claude_code_not_subscription_auth",
            "message": "Claude Code 当前不是订阅 OAuth / first-party 认证。P 模式已阻止启动，避免误走 API Key、代理或云厂商计费；请先在终端运行 claude auth status 核对认证。",
        }
    if exc_code.startswith("claude_code_error_max_budget") or "error_max_budget" in lowered:
        return {
            "code": "claude_code_budget_limit",
            "message": "Claude Code P 模式触发了本轮预算上限；消息已保存在本机，可以调整 P 模式上限后重试。",
        }
    if exc_code.startswith("claude_code_error_max_turns") or "error_max_turns" in lowered:
        return {
            "code": "claude_code_turn_limit",
            "message": "Claude Code P 模式触发了本轮最大轮数；消息已保存在本机，可以调整 P 模式轮数后重试。",
        }

    if "not available in your region" in lowered:
        return {
            "code": "model_region_unavailable",
            "message": (
                "当前模型在你所在地区不可用。Key 已成功连接，这不是余额问题；"
                "请到工作室切换“OR · 通用兼容 → openrouter/auto”，或继续使用 DeepSeek。"
            ),
        }
    if "insufficient credit" in lowered or "payment required" in lowered or "error code: 402" in lowered:
        return {
            "code": "provider_credit_required",
            "message": "模型通道返回余额或 Key 额度不足（HTTP 402），请检查对应供应商账户与这把 Key 的单独限额。",
        }
    if "authenticationerror" in exc_name or "error code: 401" in lowered:
        return {
            "code": "provider_auth_failed",
            "message": "模型通道拒绝了当前 Key（HTTP 401）。请确认右上角通道与保存的 Key 类型一致。",
        }
    if "permissiondenied" in exc_name or "error code: 403" in lowered:
        return {
            "code": "provider_permission_denied",
            "message": "模型通道拒绝了本次请求（HTTP 403）。请检查这把 Key 的模型白名单、Guardrail 与隐私设置。",
        }
    if "ratelimit" in exc_name or "error code: 429" in lowered:
        return {
            "code": "provider_rate_limited",
            "message": "模型通道正在限流（HTTP 429），你的消息已保存，稍后直接重发即可。",
        }
    if "context_length" in lowered or "context length" in lowered:
        return {
            "code": "model_context_too_long",
            "message": "本轮上下文超过了当前模型可接收的长度。请新开对话或减少固定资料后重试。",
        }
    if any(marker in lowered for marker in ("connecterror", "connection error", "timed out", "timeout")):
        return {
            "code": "provider_network_error",
            "message": "连接模型供应商超时或失败。请检查网络后重试；你的消息已经保存在本机。",
        }
    return {
        "code": "model_request_failed",
        "message": "模型请求失败，请查看本机诊断记录。你的消息已经保存在本机。",
    }


def _visible_thinking_label(provider: str, *, interrupted: bool = False) -> str:
    """Name only the reasoning text an upstream API explicitly exposed."""
    labels = {
        "deepseek": "DeepSeek 模型草稿",
        "anthropic": "Claude API 可见思考",
        "openrouter_claude": "Claude API 可见思考摘要",
        "openai": "GPT API 推理摘要",
        "openrouter_gpt": "OpenRouter GPT 推理摘要",
        "openrouter": "OpenRouter 可见推理",
        "claude_code_p": "Claude Code 可见思考摘要",
    }
    label = labels.get(str(provider or "").strip(), "模型 API 可见推理")
    return f"{label}（已停止）" if interrupted else label


def _refresh_provider_credentials() -> dict:
    """Reload the current .env and discard model lists tied to old keys."""
    result = config.reload_provider_credentials()
    changed = list(result.get("changed_providers") or [])
    if changed:
        gateway.invalidate_models_cache(changed)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时
    ocean_listen.start()
    pairing_token = access_control.prepare()
    attachment_reconcile = attachment_service.reconcile()
    applied_migrations = run_schema_migrations()
    relational_honesty_guard.ensure_schema()
    relational_honesty_guard.settings(refresh=True)
    recovered_requests = chat_request_store.recover_stale_processing()
    init_jieba_fts5()  # 确保 jieba FTS5 表存在（retriever_v4 的关键词检索依赖它）
    memory_archive.ensure_schema()
    natural_memory.ensure_schema()
    archived_raw = memory_archive.archive_existing_messages()
    affect_core.ensure_schema()
    affect_core.load(refresh=True)
    character_integrity.ensure_schema()
    character_integrity.load_settings(refresh=True)
    native_continuity.ensure_schema()
    living_state.ensure_schema()
    living_state.load(refresh=True)
    inner_state.ensure_schema()
    inner_state.settings(refresh=True)
    inner_state.initialize()
    inner_state.advance()
    relationship_continuity.ensure_schema()
    relationship_continuity.load(refresh=True)
    file_workspace.ensure_schema()
    migrated_files = file_workspace.migrate_existing()
    queued_audio = ocean_listen.reconcile()
    thinking_vault.ensure_schema()
    local_data_hub.ensure_schema()
    style_profiles.ensure_schema()
    context_compactor.ensure_schema()
    dwell_life.ensure_schema()
    history_migration.recover_interrupted()
    persona_cards.load(refresh=True)
    voice_service.ensure_schema()
    voice_service.load_settings(refresh=True)
    voice_service.close_stale_calls()
    co_presence.ensure_schema()
    recovered_co_presence = co_presence.recover_stale_processing(startup=True)
    co_presence.settings(refresh=True)
    persona_context_gate.ensure_schema()
    plugin_registry.register(
        name="source_faithful_memory", display_name="原文记忆档案", version="1.0",
        description="原始聊天不可改写；派生记忆保留精确引用和来源。",
        source="大西瓜重写（研究 Paramecium 思路）", health_check=memory_archive.health,
    )
    plugin_registry.register(
        name="natural_user_memory", display_name="随口记忆", version="1.0",
        description="短句偏好与稳定事实只在原窗口召回；改口会替代旧值，并保留原消息来源。",
        source="大西瓜 v7.1 本地规则层", health_check=natural_memory.health,
    )
    plugin_registry.register(
        name="affect_core", display_name="情绪与亲密意图", version="1.0",
        description="16维即时情绪、慢速心境、嫉妒/占有欲/欲望与亲密意图余波。",
        source="大西瓜重写（研究 Drivesoid 思路）", health_check=affect_core.health,
    )
    plugin_registry.register(
        name="tool_bridge", display_name="只读工具桥", version="1.1",
        description="真实诊断、请求轨迹与受限源码读取。",
        health_check=lambda: {"health": "ok", "detail": f"{len(tool_bridge.tools)} 个只读工具"},
    )
    plugin_registry.register(
        name="history_migration", display_name="大型历史迁移", version="1.0",
        description="ZIP/JSON 按窗口流式解析、预览、去重并恢复为本机对话。",
        health_check=history_migration.health,
    )
    plugin_registry.register(
        name="character_integrity", display_name="性格脊柱与表达疲劳", version="1.0",
        description="保留 Claude/GPT 原生声音，只对近期重复口癖与标点做临时降频。",
        source="大西瓜 v5.8.2 原生实现", health_check=character_integrity.health,
    )
    plugin_registry.register(
        name="native_continuity", display_name="原生回合保真", version="1.0",
        description="同供应商同模型原样续接 Responses/ Messages 内容块；跨模型只回放可见原文。",
        source="大西瓜 v5.8.2 原生实现", health_check=native_continuity.health,
    )
    plugin_registry.register(
        name="living_state", display_name="活体节律", version="1.0",
        description="昼夜、身体、梦后余波、连接/骄傲/沉浸与确定性主动意识。",
        source="大西瓜 v5.9 原生实现", health_check=living_state.health,
    )
    plugin_registry.register(
        name="inner_state_runtime", display_name="三合一内心系统", version="2.0",
        description="情绪、欲望和生活会互相推动，并形成自控、反刍、情绪夺权、亲密状态与私人内心 OS。",
        source="大西瓜 v6.8 原生实现", health_check=inner_state.health,
    )
    plugin_registry.register(
        name="morning_response", display_name="独立晨间反应", version="1.0",
        description="硬度、主观性欲、敏感、身体张力、自控和身体夺权分开演化。",
        source="大西瓜 v6.8 原生实现", health_check=morning_response.health,
    )
    plugin_registry.register(
        name="relationship_continuity", display_name="关系连续性", version="1.0",
        description="以原话证据保存关系章节、未完之事与共同空间，不重写模型输出。",
        source="大西瓜 v6.0 原生实现", health_check=relationship_continuity.health,
    )
    plugin_registry.register(
        name="file_workspace", display_name="本地资料工作室", version="1.0",
        description="资料库、本轮使用、按需摘取和全文常驻四态分离，并显示上下文预算。",
        source="大西瓜 v6.0.2 原生实现", health_check=file_workspace.health,
    )
    plugin_registry.register(
        name="ocean_listen", display_name="听海 · 本机听觉", version="1.0",
        description="唱歌与声音附件逐个在 Mac 本机分析；所有模型收到压缩报告，视觉通道另收频谱图。",
        source="Ocean Listen（MIT）+ 大西瓜隔离队列", health_check=ocean_listen.health,
    )
    plugin_registry.register(
        name="dwell_life", display_name="生活空间", version="2.0",
        description="日记、待办、日历、悄悄话、共读与音乐使用本机 SQLite 事务存储。",
        source="jtyhome × dwell 融合层", health_check=dwell_life.health,
    )
    plugin_registry.register(
        name="thinking_vault", display_name="可见思考分层库", version="1.0",
        description="单独保存 API 或导出文件明确提供的思考内容，不进入跨模型聊天历史。",
        source="大西瓜 v6.0.2 原生实现", health_check=thinking_vault.health,
    )
    plugin_registry.register(
        name="local_data_ownership", display_name="本机数据所有权", version="1.0",
        description="整机备份恢复、全文搜索、消息收藏和逐窗口导出；备份不包含 API Key 与配对码。",
        source=f"大西瓜 v{config.APP_VERSION} 本机数据层", health_check=local_data_hub.health,
    )
    plugin_registry.register(
        name="selected_style_profile", display_name="人工选定聊天风格", version="1.0",
        description="只从使用者亲自选中的优质窗口学习表达形式；样本事实不会成为记忆。",
        source=f"大西瓜 v{config.APP_VERSION} 风格层", health_check=style_profiles.health,
    )
    plugin_registry.register(
        name="macos_local_service", display_name="macOS 本机常驻", version="1.0",
        description="可选 LaunchAgent 自动启动与异常重启；Mac 休眠或关机时不会运行。",
        source="macOS LaunchAgent", health_check=local_service.health,
    )
    plugin_registry.register(
        name="context_composer", display_name="上下文编排器", version="1.0",
        description="按优先级和字符预算组织动态上下文，避免子系统无限堆叠。",
        source="大西瓜 v5.8.2 原生实现", health_check=context_composer.health,
    )
    plugin_registry.register(
        name="incremental_context", display_name="增量上下文压缩", version="2.0",
        description="旧消息只整理一次并缓存，章节逐条连接原文；默认不调用付费模型。",
        source="大西瓜 v6.2 原生实现", health_check=context_compactor.health,
    )
    plugin_registry.register(
        name="artifact_persona_cards", display_name="相处底色卡", version="1.0",
        description="旧作品只提炼为少量话题卡；共同剧本不会覆盖当前模型身份。",
        source="大西瓜 v6.4+ 本机提炼层", health_check=persona_cards.health,
    )
    plugin_registry.register(
        name="elevenlabs_voice", display_name="完整语音、陪睡与电台", version="3.0",
        description="Scribe 转写、时间戳朗读、VAD 通话、声学/情绪、私密陪睡、录音归档与电台。",
        source="ElevenLabs + Qwen Omni + 大西瓜本机语音层", health_check=voice_service.health,
    )
    plugin_registry.register(
        name="co_presence", display_name="共处感知与晨间主动开口", version="1.3",
        description="表达节奏、回合余韵、独立联系与实时晨间身体事件共用原窗口原模型；输入期间自动顺延。",
        source=f"大西瓜 v{config.APP_VERSION} 本机共处层", health_check=co_presence.health,
    )
    plugin_registry.register(
        name="conditional_persona_constitution",
        display_name="条件人格宪法门",
        version="1.0",
        description="争执或明确第三方话题首次触发；长对话每十条消息刷新，明确结束后停止。",
        source="大西瓜本机规则与内容无关表达节奏",
        health_check=persona_context_gate.health,
    )
    await pipeline.start()
    life_memory_sync = life_memory_bridge.sync_existing(pipeline)
    if any(life_memory_sync.values()):
        print(
            "[Memory] Life 桥接排队: "
            f"信件 {life_memory_sync['letters']} / 日记 {life_memory_sync['diary']} / "
            f"待办 {life_memory_sync['todos']} / 错误 {life_memory_sync['errors']}"
        )

    async def _natural_memory_backfill():
        result = await asyncio.to_thread(natural_memory.backfill_batch, 500)
        if result.get("scanned"):
            print(
                "[Memory] 旧窗口随口记忆补扫: "
                f"{result['scanned']} 条消息 / {result['facts']} 条事实"
            )

    async def _proactive_beat():
        await proactive.check_and_send()

    # v8.4 prompt-cache warmer: exact one-shot scheduling + 10-minute watchdog.
    # The sender bypasses chat orchestration, so keepalive cannot mutate history,
    # memory/state, notifications, timeline or proactive queues.
    cache_keepalive.bind(scheduler, gateway.send_cache_keepalive)
    await cache_keepalive.startup_catchup()
    scheduler.add_job(
        _proactive_beat,
        "interval",
        seconds=config.CO_PRESENCE_CONFIG.get("check_interval_seconds", 20),
        id="co_presence",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        cache_keepalive.watchdog,
        "interval",
        minutes=10,
        id="cache_keepalive_watchdog",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _natural_memory_backfill,
        "interval",
        minutes=2,
        id="natural_memory_backfill",
        next_run_time=datetime.now(),
    )
    scheduler.add_job(inner_state.advance, "interval", minutes=10, id="living_tick")
    scheduler.add_job(
        desire_host.heartbeat,
        "interval",
        minutes=10,
        id="desire_tick",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(run_decay_cycle, "interval", hours=1, id="decay_cycle")  # 遗忘衰减
    scheduler.add_job(
        voice_service.close_stale_calls,
        "interval",
        seconds=60,
        id="voice_call_watchdog",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    print("[大西瓜] 定时任务已启动 (co-presence + decay)")
    print(f"[大西瓜] 原文档案补齐：{archived_raw} 条新增")
    if recovered_co_presence:
        print(f"[大西瓜] 已恢复 {recovered_co_presence} 条中断的主动消息任务")
    print(f"[大西瓜] v{config.APP_VERSION} 记忆引擎: 原文档案 + 持久写入队列 + 随口事实层")
    honesty_mode = "严格发送前检查" if relational_honesty_guard.enabled() else "已关闭"
    print(
        f"[大西瓜] 关系诚实保护: {honesty_mode}；"
        f"数据库迁移 {applied_migrations or '已是最新'}；恢复 {recovered_requests} 个中断请求"
    )
    print("[大西瓜] 情感核心: 16维状态 + 亲密意图")
    print(f"[大西瓜] 资料工作室迁移：{migrated_files} 份旧附件新增索引")
    if queued_audio:
        print(f"[大西瓜] 听海恢复：{queued_audio} 份等待音频重新进入本机队列")
    if attachment_reconcile.get("quarantined") or attachment_reconcile.get("removed_temps"):
        print(f"[大西瓜] 附件一致性修复：{attachment_reconcile}")
    print("[大西瓜] v6.2: 增量上下文压缩 + 原文溯源 + 上下文检查器")
    print(
        f"[大西瓜] v{config.APP_VERSION}: 清透水晶 + 关系诚实保护 + 断线恢复 + 有依据同窗口续话 + 陪伴昵称 {config.COMPANION_NAME}"
        " + 三合一内心 + 可靠记忆 + 人格稳定实验室"
    )
    print(
        "[大西瓜] iPhone/局域网配对码: "
        f"{pairing_token}（也保存在 {access_control.token_path}）"
    )
    yield
    # 关闭时
    scheduler.shutdown()
    ocean_listen.shutdown()
    await pipeline.stop()
    await gateway.close()


app = FastAPI(lifespan=lifespan)

@app.exception_handler(dwell_life.DwellDataCorruptionError)
async def dwell_corruption_handler(request: Request, exc: dwell_life.DwellDataCorruptionError):
    return JSONResponse({"detail": str(exc), "code": "dwell_data_corrupt"}, status_code=409)

@app.exception_handler(dwell_life.DwellStorageError)
async def dwell_storage_handler(request: Request, exc: dwell_life.DwellStorageError):
    return JSONResponse({"detail": str(exc), "code": "dwell_storage_error"}, status_code=503)

app.include_router(reliability_router)

# Same-origin is the safe local default. Explicit deployments can opt in to a
# small allowlist; a wildcard would let an arbitrary website drive the local
# companion API from the user's browser.
_cors_origins = [
    value.strip().rstrip("/")
    for value in os.getenv("DAXIGUA_CORS_ORIGINS", "").split(",")
    if value.strip() and value.strip() != "*"
]


_PUBLIC_REMOTE_PATHS = {
    "/",
    "/manifest.json",
    "/sw.js",
    "/api/launcher-ready",
    "/api/access/status",
    "/api/access/unlock",
}


_DIAGNOSTIC_SELF_TEST_LOCK = asyncio.Lock()
_DIAGNOSTIC_SELF_TEST_LAST = 0.0


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    """Reject rebinding/CSRF and require a browser session or pairing token."""
    path = request.url.path
    unsafe = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    public = (
        request.method == "OPTIONS"
        or path in _PUBLIC_REMOTE_PATHS
        or path.startswith("/static/")
    )

    if not access_control.allowed_host(request.headers.get("host", "")):
        return JSONResponse(
            {"error": "请求 Host 未被允许", "code": "UNTRUSTED_HOST"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    if unsafe and not access_control.origin_allowed(request):
        return JSONResponse(
            {"error": "请求来源未被允许", "code": "UNTRUSTED_ORIGIN"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )

    if not public and not access_control.authenticated(request):
        return JSONResponse(
            {
                "error": "这台浏览器还没有建立大西瓜本机会话",
                "code": "PAIRING_REQUIRED",
            },
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )
    try:
        response = await call_next(request)
    except asyncio.CancelledError:
        request_id = getattr(request.state, "client_request_id", "")
        if request_id:
            try:
                current = chat_request_store.get(request_id) or {}
                if current.get("status") == "processing":
                    if current.get("cancel_requested"):
                        # Only an explicit stop is a user cancellation.
                        chat_request_store.interrupt(
                            request_id,
                            error_code="user_cancelled",
                        )
                    elif not chat_request_runtime.active(request_id):
                        # The transport vanished before a detached producer ever
                        # took ownership. This is a delivery failure, not a stop.
                        chat_request_store.interrupt(
                            request_id,
                            error_code="connection_lost_before_producer",
                        )
                    # Once a detached producer exists, a dead HTTP/SSE consumer
                    # must not mutate request state at all. It will finish and
                    # persist independently, then the browser can reconcile it.
            except Exception:
                pass
        raise
    except Exception:
        request_id = getattr(request.state, "client_request_id", "")
        if request_id:
            try:
                current = chat_request_store.get(request_id) or {}
                if current.get("status") == "processing":
                    chat_request_store.fail(request_id, "request_preflight_failed")
            except Exception:
                pass
        raise

    # Loading the local root establishes an HttpOnly session automatically.
    # Remote/LAN devices still use /api/access/unlock with the pairing token.
    if path == "/" and not access_control.remote_required(request):
        response.set_cookie(
            access_control.cookie_name,
            access_control.cookie_value(),
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            secure=access_control.effective_scheme(request) == "https",
            samesite="strict",
            path="/",
        )

    # Uvicorn-direct deployments receive the same baseline protections as nginx.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), payment=(), usb=(), microphone=(self)",
    )
    if path.startswith("/api/"):
        # Service Worker deliberately bypasses API routes, but Safari's normal
        # HTTP cache can still reuse an unqualified GET after the backend was
        # restarted with a new Key or Provider configuration.
        response.headers.setdefault("Cache-Control", "no-store")
    connect_sources = ["'self'", *_cors_origins]
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'self'; form-action 'self'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; "
        f"connect-src {' '.join(connect_sources)}; worker-src 'self' blob:",
    )
    return response

if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Content-Type", "Authorization", access_control.header_name,
        ],
    )

# 静态文件 & 模板
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


async def _json_object(
    request: Request, *, allow_empty: bool = False
) -> dict:
    try:
        raw = await request.body()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法读取请求内容") from exc
    if not raw:
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="请求内容不能为空")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="JSON 格式无效") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="JSON 顶层必须是对象")
    return value


def _body_str(
    body: dict, key: str, *, default: str = "", max_chars: int | None = None
) -> str:
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{key} 必须是字符串")
    value = value.strip()
    if max_chars is not None and len(value) > max_chars:
        raise HTTPException(status_code=422, detail=f"{key} 不能超过 {max_chars} 个字符")
    return value


def _body_dict(body: dict, key: str) -> dict:
    value = body.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail=f"{key} 必须是对象")
    return value


def _body_list(body: dict, key: str, *, max_items: int = 100) -> list:
    value = body.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail=f"{key} 必须是数组")
    if len(value) > max_items:
        raise HTTPException(status_code=422, detail=f"{key} 项目过多")
    return value


def _body_bool(body: dict, key: str, *, default: bool = False) -> bool:
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"{key} 必须是布尔值")
    return value


def _body_int(
    body: dict, key: str, *, default: int | None = None,
    min_value: int | None = None, max_value: int | None = None,
) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail=f"{key} 必须是整数")
    if min_value is not None and value < min_value:
        raise HTTPException(status_code=422, detail=f"{key} 不能小于 {min_value}")
    if max_value is not None and value > max_value:
        raise HTTPException(status_code=422, detail=f"{key} 不能大于 {max_value}")
    return value


def _body_float(
    body: dict, key: str, *, default: float = 0.0,
    min_value: float | None = None, max_value: float | None = None,
) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=422, detail=f"{key} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise HTTPException(status_code=422, detail=f"{key} 必须是有限数字")
    if min_value is not None and result < min_value:
        raise HTTPException(status_code=422, detail=f"{key} 不能小于 {min_value}")
    if max_value is not None and result > max_value:
        raise HTTPException(status_code=422, detail=f"{key} 不能大于 {max_value}")
    return result


def _payload_text_chars(value) -> int:
    """Estimate prompt text while ignoring base64/native binary payloads."""
    if value is None:
        return 0
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value[:100]:
            return 0
        return len(value)
    if isinstance(value, list):
        return sum(_payload_text_chars(item) for item in value)
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key in {"data", "file_data", "image_url"}:
                continue
            total += _payload_text_chars(item)
        return total
    return 0


def _context_budget(
    *,
    messages: list[dict],
    memory_context: str,
    explicit_context_chars: int,
    pinned_context_chars: int,
    retrieval_context_chars: int,
    web_context_chars: int = 0,
    compressed_context_chars: int = 0,
    compression_status: dict | None = None,
    context_snapshot: dict | None = None,
    file_sources: list[dict] | None = None,
    conditional_system_chars: int = 0,
    history_selection: dict | None = None,
    unified_budget: dict | None = None,
) -> dict:
    system_chars = sum(len(str(value or "")) for value in config.SYSTEM_PROMPT_BLOCKS.values())
    message_chars = sum(_payload_text_chars(item.get("content")) for item in messages)
    dynamic_chars = len(memory_context or "")
    compressed_chars = max(0, min(int(compressed_context_chars or 0), dynamic_chars))
    file_chars = max(
        0,
        int(explicit_context_chars or 0)
        + int(pinned_context_chars or 0)
        + int(retrieval_context_chars or 0),
    )
    web_chars = max(0, int(web_context_chars or 0))
    conversation_chars = max(0, message_chars - file_chars - web_chars)
    conditional_system_chars = max(0, int(conditional_system_chars or 0))
    total_chars = system_chars + conditional_system_chars + dynamic_chars + message_chars
    history_estimate = max(
        0, int((history_selection or {}).get("selected_tokens_estimate") or 0)
    )
    historical_message_chars = max(
        0, int((history_selection or {}).get("selected_chars") or 0)
    )
    non_history_text = " ".join(
        str(value or "")
        for value in (
            *config.SYSTEM_PROMPT_BLOCKS.values(),
            memory_context,
        )
    )
    non_history_tokens = estimate_context_tokens_from_lengths(
        len(non_history_text), len(non_history_text.encode("utf-8", "ignore"))
    )
    # conditional_system_chars is already represented inside the dynamic turn
    # context for Anthropic; for other lanes it remains a small conservative
    # addition.  Do not double-count the complete raw history by chars.
    estimated = max(
        1,
        history_estimate
        + non_history_tokens
        + estimate_context_tokens_from_lengths(conditional_system_chars)
        + estimate_context_tokens_from_lengths(
            max(0, message_chars - historical_message_chars)
        ),
    )
    if estimated >= 120_000:
        level, label = "danger", "上下文很重，模型容易分心"
    elif estimated >= 48_000:
        level, label = "warn", "上下文偏重"
    else:
        level, label = "good", "上下文清爽"
    return {
        "estimated_input_tokens": estimated,
        "total_text_chars": total_chars,
        "level": level,
        "label": label,
        "estimate_only": True,
        "breakdown": {
            "system": system_chars,
            "conditional_persona_guard": conditional_system_chars,
            "dynamic_memory": max(0, dynamic_chars - compressed_chars),
            "compressed_history": compressed_chars,
            "messages": conversation_chars,
            "explicit_files": explicit_context_chars,
            "pinned_files": pinned_context_chars,
            "retrieved_files": retrieval_context_chars,
            "webpage": web_chars,
        },
        "unified_budget": dict(unified_budget or {}),
        "compression": compression_status or {},
        "history": dict(history_selection or {}),
        "dynamic_stack": {
            "budget_chars": int((context_snapshot or {}).get("budget_chars", 0) or 0),
            "used_chars": int((context_snapshot or {}).get("used_chars", 0) or 0),
            "included": list((context_snapshot or {}).get("included", []) or []),
            "dropped": list((context_snapshot or {}).get("dropped", []) or []),
        },
        "file_sources": list(file_sources or []),
    }


async def _read_import_upload(file: UploadFile, max_bytes: int = 60 * 1024 * 1024) -> bytes:
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"导入文件不能超过 {max_bytes // 1024 // 1024} MB")
    if not raw:
        raise HTTPException(status_code=400, detail="导入文件是空的")
    return raw


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  页面路由
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_version": config.APP_VERSION,
            "asset_revision": config.FRONTEND_REVISION,
            "companion_name": config.COMPANION_NAME,
            "companion_initial": config.COMPANION_NAME[-1:],
        },
        headers={"Cache-Control": "no-store, private"},
    )


@app.get("/voice/call", response_class=HTMLResponse)
async def voice_call_page(request: Request):
    """Same-origin iframe/full-page call surface; never cache in WebView."""
    return templates.TemplateResponse(
        request,
        "voice_call.html",
        {
            "app_version": config.APP_VERSION,
            "asset_revision": config.FRONTEND_REVISION,
            "companion_name": config.COMPANION_NAME,
            "companion_initial": config.COMPANION_NAME[-1:],
        },
        headers={"Cache-Control": "no-store, private"},
    )


@app.get("/manifest.json")
async def manifest():
    return JSONResponse({
        "name": "大西瓜",
        "short_name": "大西瓜",
        "version": config.APP_VERSION,
        "description": (
            f"本地私人 AI 伴侣系统 v{config.APP_VERSION} · "
            f"{config.COMPANION_NAME}的陪伴空间"
        ),
        "start_url": "/",
        "display": "standalone",
        "background_color": "#bcdedb",
        "theme_color": "#6fa9aa",
        "orientation": "any",
        "icons": [
            {"src": "/static/icons/icon-72.png", "sizes": "72x72", "type": "image/png"},
            {"src": "/static/icons/icon-96.png", "sizes": "96x96", "type": "image/png"},
            {"src": "/static/icons/icon-128.png", "sizes": "128x128", "type": "image/png"},
            {"src": "/static/icons/icon-144.png", "sizes": "144x144", "type": "image/png"},
            {"src": "/static/icons/icon-152.png", "sizes": "152x152", "type": "image/png"},
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-384.png", "sizes": "384x384", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ]
    }, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/version")
async def version():
    return JSONResponse({
        "version": config.APP_VERSION,
        "name": "大西瓜",
        "companion_name": config.COMPANION_NAME,
    })


@app.get("/api/water-scene/status")
async def water_scene_status():
    """Same-origin health for the bundled Hydrangea water scene."""
    return JSONResponse(water_scene.status(), headers={"Cache-Control": "no-store"})


@app.get("/api/water-scene/bootstrap")
async def water_scene_bootstrap_default():
    """Default scene contract for diagnostics and no-JS checks."""
    return JSONResponse(water_scene.bootstrap(), headers={"Cache-Control": "no-store"})


@app.post("/api/water-scene/bootstrap")
async def water_scene_bootstrap(request: Request):
    """Negotiate a bounded render/input profile; no capability data is stored."""
    client = await _json_object(request, allow_empty=True)
    return JSONResponse(
        water_scene.bootstrap(client),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/launcher-ready")
async def launcher_ready():
    """Public, non-personal readiness marker for the local browser launcher."""
    return JSONResponse({
        "version": config.APP_VERSION,
        "name": "大西瓜",
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/access/status")
async def access_status(request: Request):
    return JSONResponse(
        access_control.status(request),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/access/unlock")
async def access_unlock(request: Request):
    if not access_control.allow_attempt(request):
        raise HTTPException(status_code=429, detail="尝试次数太多，请五分钟后再试")
    body = await _json_object(request)
    token = _body_str(body, "token", max_chars=256)
    if not access_control.verify_token(token):
        raise HTTPException(status_code=401, detail="配对码不正确")
    access_control.clear_attempts(request)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        access_control.cookie_name,
        access_control.cookie_value(),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        secure=access_control.effective_scheme(request) == "https",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/sw.js")
async def service_worker():
    sw_path = os.path.join(BASE_DIR, "static", "sw.js")
    # v6.5.2：显式 UTF-8——Windows 默认 GBK 编码读中文注释会 500，Mac 行为不变。
    with open(sw_path, encoding="utf-8") as f:
        return HTMLResponse(
            content=f.read(),
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Service-Worker-Allowed": "/",
            },
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  聊天 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/chat")
async def chat_api(request: Request):
    """主聊天接口 - SSE + 诊断轨迹 + 附件/表情包框架。"""
    _refresh_provider_credentials()
    body = await _json_object(request)
    message = _body_str(body, "message")
    try:
        client_request_id = normalize_client_request_id(
            body.get("client_request_id"), create_if_missing=True
        )
    except InvalidClientRequestId as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    attachment_ids = _body_list(body, "attachments", max_items=24)
    raw_client_metadata = (
        _body_dict(body, "client_metadata")
    )
    session_id = _validated_session_id(
        body.get("session_id"), default=str(uuid.uuid4())
    )
    provider = _body_str(body, "provider", default=config.ACTIVE_PROVIDER, max_chars=64)
    # v6.5.2：无效通道直接返回干净的 400，而不是带 traceback 的 500。
    if provider not in PROVIDERS:
        return JSONResponse(
            {"error": f"未知的模型通道：{provider}。请刷新页面，或到「系统」重新选择模型。"},
            status_code=400,
        )
    provider_reselected = False
    if not config.provider_is_ready(provider):
        refreshed_provider = config.ACTIVE_PROVIDER
        if not config.provider_is_ready(refreshed_provider):
            refreshed_provider = next(
                (
                    name for name in PROVIDERS
                    if config.provider_is_ready(name)
                ),
                refreshed_provider,
            )
        if (
            refreshed_provider in PROVIDERS
            and config.provider_is_ready(refreshed_provider)
        ):
            provider = refreshed_provider
            provider_reselected = True
            switch_provider(provider)
    model = (
        get_active_model(provider)
        if provider_reselected
        else _body_str(body, "model", default=get_active_model(provider), max_chars=240)
    )
    model = config.cache_stable_model(provider, model)
    options = _body_dict(body, "options")

    message = (message or "").strip()
    options = apply_runaway_output_guard(options, message)
    if len(message) > _MAX_CHAT_CHARS:
        return JSONResponse(
            {"error": f"单条消息不能超过 {_MAX_CHAT_CHARS:,} 个字符；大型记录请使用导入功能。"},
            status_code=413,
        )
    explicit_attachments = attachment_service.resolve_many(attachment_ids)
    if not message and not explicit_attachments:
        return JSONResponse({"error": "消息和附件不能同时为空"}, status_code=400)
    pending_audio = [
        item for item in explicit_attachments
        if item.get("kind") == "audio" and item.get("analysis_status") != "ready"
    ]
    if pending_audio:
        failed = [item for item in pending_audio if item.get("analysis_status") == "error"]
        if failed:
            return JSONResponse({
                "error": "这段音频还没有被听海听完。请查看失败原因并点“重新分析”，不能把文件名当作听觉。",
                "code": "audio_analysis_failed",
                "attachments": [attachment_service.public_meta(item) for item in failed],
            }, status_code=422)
        return JSONResponse({
            "error": "听海还在准备或分析这段音频。显示“已听完”后才能发送，避免模型假装听见。",
            "code": "audio_analysis_pending",
            "attachments": [attachment_service.public_meta(item) for item in pending_audio],
        }, status_code=409)
    unreadable_video = [
        item for item in explicit_attachments
        if item.get("kind") == "video" and int(item.get("video_frame_count") or 0) <= 0
    ]
    if unreadable_video:
        return JSONResponse({
            "error": "视频没有成功读取关键帧，不能只把文件名发给模型。请确认本机已安装 ffmpeg/ffprobe 后重新上传。",
            "code": "video_decode_failed",
            "attachments": [attachment_service.public_meta(item) for item in unreadable_video],
        }, status_code=422)
    trace_id = diagnostics.start_trace(
        session_id=session_id,
        provider=provider,
        model=model,
        message_preview=message or "[附件]",
    )
    diagnostics.add_stage(trace_id, "request_received", label="请求接收", duration_ms=0)

    # 确保 session 存在。
    timer = StageTimer()
    try:
        with get_db() as db:
            exists = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not exists:
            try:
                create_session(session_id, provider, model or "")
            except Exception as create_exc:
                # v6.5.2：并发场景下会话可能刚被另一请求建好，属正常竞争；
                # 但真正的建表失败也要留痕，否则后面消息落库的外键错误会
                # 显示一个与根因完全无关的报错，排查非常迷惑。
                diagnostics.record_error("session_create", create_exc, request_id=trace_id)
        diagnostics.add_stage(trace_id, "session_ready", label="会话准备", duration_ms=timer.ms)
    except Exception as exc:
        diagnostics.add_stage(trace_id, "session_ready", label="会话准备", status="error", duration_ms=timer.ms, details={"error": str(exc)})
        diagnostics.record_error("session_ready", exc, request_id=trace_id)
        diagnostics.finish_trace(trace_id, status="failed", error=exc)
        return JSONResponse(
            {"error": "会话准备失败，请查看本机诊断记录", "request_id": trace_id},
            status_code=500,
        )

    # v7.4 reliability: the browser assigns one durable ID before sending.
    # Claiming it before any message/effect write makes retries idempotent.
    try:
        claim = chat_request_store.claim(
            client_request_id,
            session_id=session_id,
            provider=provider,
            model=model,
            trace_id=trace_id,
        )
    except RequestIdentityConflict as exc:
        diagnostics.finish_trace(trace_id, status="failed", error=exc)
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        diagnostics.record_error("chat_request_claim", exc, request_id=trace_id)
        diagnostics.finish_trace(trace_id, status="failed", error=exc)
        return JSONResponse(
            {"error": "聊天请求登记失败，请查看本机诊断记录", "request_id": trace_id},
            status_code=500,
        )
    if not claim.created:
        diagnostics.add_stage(
            trace_id, "idempotent_replay", label="复用已有聊天请求",
            details={"status": claim.request.get("status")},
        )
        diagnostics.finish_trace(trace_id, status="completed")
        return StreamingResponse(
            replay_request_events(client_request_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Client-Request-ID": client_request_id,
            },
        )
    request.state.client_request_id = client_request_id

    sticker_service.note_user_turn(session_id)

    # v7.4：一条真正发出的用户消息永远优先于旧的后台判断。浏览器附带的
    # 表达节奏经过硬白名单后才留下，输入框文字没有任何进入此层的字段。
    expression_rhythm = co_presence.sanitize_rhythm(
        raw_client_metadata.get("expression_rhythm")
    )
    try:
        co_presence.note_user_message(
            session_id, user_text=message,
            provider=provider, model=model or ""
        )
    except Exception as exc:
        diagnostics.record_error("co_presence_user_turn", exc, request_id=trace_id)

    # v8.1 Codex cost pass: keep the 7.9.9 stable-history lane intact and
    # allocate all side material through one per-model budget gate.
    file_conf = config.FILE_CONTEXT_CONFIG
    pinned_ids = file_workspace.mode_ids(
        session_id, "pinned", limit=file_conf.get("pinned_file_limit", 3)
    )
    retrieval_ids = file_workspace.mode_ids(
        session_id, "retrieval", limit=file_conf.get("retrieval_file_limit", 12)
    )
    explicit_ids = {str(item.get("id") or "") for item in explicit_attachments}
    pinned_attachments = [
        item for item in attachment_service.resolve_many(pinned_ids, limit=12)
        if item.get("id") not in explicit_ids
    ]
    pinned_id_set = {str(item.get("id") or "") for item in pinned_attachments}
    retrieval_attachments = [
        item for item in attachment_service.resolve_many(retrieval_ids, limit=40)
        if item.get("id") not in explicit_ids and item.get("id") not in pinned_id_set
    ]

    try:
        selected_style_context = style_profiles.prompt_context()
    except Exception as exc:
        selected_style_context = ""
        diagnostics.record_error("selected_style_profile", exc, request_id=trace_id)
    provider_conf_for_budget = get_provider_config(provider)
    page_urls = webpage_service.extract_urls(message)
    unified_budget = plan_main_context(
        provider=provider,
        model=model,
        provider_conf=provider_conf_for_budget,
        options=options,
        current_message=message,
        stable_style_chars=len(selected_style_context or ""),
        has_explicit=bool(explicit_attachments),
        has_web=bool(page_urls),
        has_retrieval=bool(retrieval_attachments),
        has_pinned=bool(pinned_attachments),
    )

    # Pinned no longer means "repeat the whole body". Keep a short catalog and
    # only query-matched excerpts; selecting the file explicitly still sends the
    # larger one-turn body.
    pinned_context, pinned_used_ids = attachment_service.pinned_index_context(
        pinned_attachments,
        message,
        max_total_chars=unified_budget.pinned_index_max_chars,
    )
    retrieval_context, retrieval_used_ids = attachment_service.relevant_text_context(
        retrieval_attachments,
        message,
        max_total_chars=unified_budget.retrieval_max_chars,
    )
    workspace_context = "\n\n".join(
        part for part in (pinned_context, retrieval_context) if part
    )

    # Webpage full text is current-turn only. Persist the full snapshot locally;
    # message history stores URL/title/hash/short excerpt instead of the body.
    web_timer = StageTimer()
    web_pages = await asyncio.to_thread(webpage_service.fetch_from_text, message) if page_urls else []
    web_page_refs = webpage_service.persist_pages(
        web_pages,
        excerpt_chars=config.WEB_CONTEXT_CONFIG.get("history_excerpt_chars", 420),
    ) if web_pages else []
    web_context = webpage_service.context(
        web_pages, max_total_chars=unified_budget.web_max_chars
    )
    diagnostics.add_stage(
        trace_id, "webpage_read", label="读取网页链接", duration_ms=web_timer.ms,
        status="warning" if any(p.get("status") != "ok" for p in web_pages) else "ok",
        details={
            "urls": len(web_pages),
            "success": sum(1 for p in web_pages if p.get("status") == "ok"),
            "chars": sum(int(p.get("chars") or 0) for p in web_pages),
            "current_turn_context_chars": len(web_context),
            "local_snapshot_refs": sum(1 for p in web_page_refs if p.get("snapshot_hash")),
        },
    )
    request_extra_context = "\n\n".join(part for part in (workspace_context, web_context) if part)
    cache_tail_protocol = str(provider_conf_for_budget.get("protocol") or "") in {
        "anthropic", "deepseek_chat"
    }
    # v8.2.1 cache continuity: Pinned/RAG/web snippets are useful this turn but
    # are not conversational history. Claude and DeepSeek therefore receive
    # them after the visible-user cache boundary, never inside the stable user
    # block that must replay byte-identically next turn.
    post_checkpoint_context = request_extra_context if cache_tail_protocol else ""

    # Pinned images/videos are native-delivered once, then only on an explicit
    # visual follow-up. Ordinary turns keep the short pinned reference above.
    pinned_native_media = [
        item for item in pinned_attachments
        if item.get("kind") in {"image", "video"}
        or (item.get("kind") == "audio" and item.get("analysis_status") == "ready")
    ]
    native_visual_protocol = str(provider_conf_for_budget.get("protocol") or "") in {
        "openai_responses", "anthropic", "claude_code_p"
    }
    current_native_attachments: list[dict] = []
    seen_native: set[str] = set()
    pinned_native_delivered: list[str] = []
    for item in explicit_attachments:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen_native:
            continue
        seen_native.add(item_id)
        current_native_attachments.append(item)
    for item in pinned_native_media:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in seen_native:
            continue
        if item.get("kind") == "audio":
            # Codex change targets repeated image/video visuals only. Keep the
            # existing Ocean spectrogram behaviour unchanged.
            seen_native.add(item_id)
            current_native_attachments.append({**item, "_native_only": True})
            continue
        # Text-only compatible lanes (for example DeepSeek) never receive the
        # pinned raw visual in the first place; their ordinary turns therefore
        # keep only the short pinned reference and do not destabilize the text
        # cache shape with a repeated blind-visual warning.
        if not native_visual_protocol:
            continue
        first_delivery = not file_workspace.native_was_delivered(session_id, item_id)
        explicit_visual_request = attachment_service.wants_visual_refresh(message, item)
        if not (first_delivery or explicit_visual_request):
            continue
        seen_native.add(item_id)
        current_native_attachments.append({**item, "_native_only": True})
        pinned_native_delivered.append(item_id)

    used_attachment_ids = {
        *explicit_ids,
        *pinned_used_ids,
        *retrieval_used_ids,
    }
    explicit_context_chars = len(attachment_service.model_text_context(
        explicit_attachments,
        max_total_chars=unified_budget.explicit_max_chars,
    ))

    timer = StageTimer()
    display_message = message
    stored_message = message or "请查看我发送的附件。"
    user_metadata = attachment_service.message_metadata(
        explicit_attachments, display_text=display_message
    )
    if web_page_refs:
        user_metadata["web_pages"] = web_page_refs
        user_metadata["webpage_read_count"] = sum(1 for page in web_page_refs if page.get("status") == "ok")
    voice_model_message = message
    if expression_rhythm:
        user_metadata["expression_rhythm"] = {
            key: value
            for key, value in expression_rhythm.items()
            if key != "draft_active"
        }
    # Only a tiny allowlist of voice observations is accepted. Numeric fields
    # are clamped and labels are length-limited before they become a short
    # current-turn context; the stored/displayed user message stays untouched.
    if raw_client_metadata.get("voice_transcript") is True:
        user_metadata["voice_transcript"] = True
        user_metadata["message_type"] = "voice"
        try:
            duration_ms = int(raw_client_metadata.get("voice_duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        user_metadata["voice_duration_ms"] = max(0, min(duration_ms, 60 * 60 * 1000))
        user_metadata["voice_transcriber"] = str(
            raw_client_metadata.get("voice_transcriber") or "browser"
        )[:32]
        acoustic = voice_service.sanitize_acoustic(
            raw_client_metadata.get("voice_acoustic")
        )
        raw_mood = raw_client_metadata.get("voice_mood")
        raw_mood = raw_mood if isinstance(raw_mood, dict) else {}

        def mood_number(name: str) -> float:
            try:
                value = float(raw_mood.get(name) or 0)
            except (TypeError, ValueError):
                value = 0
            return max(0.0, min(value, 1.0))

        mood = {
            "provider": str(raw_mood.get("provider") or "")[:40],
            "available": bool(raw_mood.get("available")),
            "speech": bool(raw_mood.get("speech", True)),
            "emotion": str(raw_mood.get("emotion") or "")[:80],
            "intensity": mood_number("intensity"),
            "pace": str(raw_mood.get("pace") or "")[:80],
            "background": str(raw_mood.get("background") or "")[:160],
            "confidence": mood_number("confidence"),
        }
        private_mode = bool(raw_client_metadata.get("voice_private_mode"))
        sleep_mode = bool(raw_client_metadata.get("voice_sleep_mode"))
        user_metadata.update({
            "voice_acoustic": acoustic,
            "voice_mood": mood,
            "voice_private_mode": private_mode,
            "voice_sleep_mode": sleep_mode,
        })
        audio_line = (
            "[audio] "
            f"RMS={acoustic['rms']:.4f}, peak={acoustic['peak']:.4f}, "
            f"pitch≈{acoustic['pitch_hz']:.1f}Hz, "
            f"voiced_ratio={acoustic['voiced_ratio']:.2f}, "
            f"frames={acoustic['frame_count']}"
        )
        mood_line = (
            "[mood] unavailable"
            if not mood["available"]
            else (
                f"[mood] {mood['emotion']}; intensity={mood['intensity']:.2f}; "
                f"pace={mood['pace']}; background={mood['background']}; "
                f"confidence={mood['confidence']:.2f}"
            )
        )
        mode_line = (
            f"[call] private={str(private_mode).lower()}, "
            f"sleep={str(sleep_mode).lower()}"
        )
        sleep_instruction = (
            "\n[sleep-mode] 下行默认静默；仅在确属重要提醒时以 [important] 开头，"
            "需要主动结束本次通话时输出 [call:hangup]。"
            if sleep_mode else ""
        )
        voice_model_message = (
            f"[voice]\n{message}\n{audio_line}\n{mood_line}\n{mode_line}"
            f"{sleep_instruction}"
        )
    # v8.2.1: save the exact stable Anthropic text block used for this visible
    # user turn. Dynamic Pinned/RAG/web/state lives after the cache checkpoint,
    # so it is deliberately excluded. Native/binary attachment turns remain
    # non-checkpointed and are not duplicated into SQLite metadata.
    if (
        str(provider_conf_for_budget.get("protocol") or "") == "anthropic"
        and not current_native_attachments
    ):
        user_metadata[_ANTHROPIC_REPLAY_META_KEY] = _anthropic_text_replay_payload(
            voice_model_message
        )

    # Commit barrier: once the visible user message is in SQLite, optional
    # request-link/archive work must never turn this request into a retryable
    # 500 (which would duplicate the already committed turn).
    try:
        user_message_id = save_message(
            session_id,
            "user",
            stored_message,
            provider=provider,
            model=model or "",
            metadata=user_metadata,
        )
    except Exception as exc:
        diagnostics.add_stage(trace_id, "user_message_save", label="保存用户消息", status="error", duration_ms=timer.ms, details={"error": str(exc)})
        diagnostics.record_error("user_message_save", exc, request_id=trace_id)
        try:
            chat_request_store.fail(client_request_id, "user_message_save_failed")
        except Exception:
            pass
        diagnostics.finish_trace(trace_id, status="failed", error=exc)
        return JSONResponse(
            {"error": "消息保存失败，请查看本机诊断记录", "request_id": trace_id},
            status_code=500,
        )

    user_save_warnings: list[str] = []
    try:
        chat_request_store.attach_user_message(client_request_id, user_message_id)
    except Exception as exc:
        user_save_warnings.append("request_link")
        diagnostics.record_error("user_message_request_link", exc, request_id=trace_id)
    try:
        memory_archive.archive_message(
            message_id=user_message_id, session_id=session_id, role="user",
            content=stored_message, metadata=user_metadata,
        )
    except Exception as exc:
        user_save_warnings.append("memory_archive")
        diagnostics.record_error("user_message_archive", exc, request_id=trace_id)
    diagnostics.add_stage(
        trace_id,
        "user_message_save",
        label="保存用户消息",
        status="warning" if user_save_warnings else "ok",
        duration_ms=timer.ms,
        details={
            "attachments": len(explicit_attachments),
            "workspace_pinned_files": len(pinned_attachments),
            "workspace_retrieved_files": len(retrieval_used_ids),
            "optional_warnings": user_save_warnings,
        },
    )

    user_state_changes: list[dict] = []
    user_inner_os = None
    inner_state_snapshot: dict | None = None
    timer = StageTimer()
    try:
        inner_turn = await asyncio.to_thread(
            inner_state.on_user_message,
            message or "我发送了附件",
            session_id=session_id,
        )
        user_state_changes = inner_turn.get("changes") or []
        user_inner_os = inner_turn.get("inner_os")
        inner_state_snapshot = inner_turn.get("snapshot") if isinstance(inner_turn.get("snapshot"), dict) else None
        diagnostics.add_stage(
            trace_id,
            "inner_life_update",
            label="推进三合一内心、身体与亲密状态",
            duration_ms=timer.ms,
            details={
                "events": inner_turn.get("detected_events") or [],
                "changes": len(user_state_changes),
                "inner_os": bool(user_inner_os),
            },
        )
    except Exception as exc:
        diagnostics.add_stage(
            trace_id,
            "inner_life_update",
            label="推进三合一内心、身体与亲密状态",
            status="error",
            duration_ms=timer.ms,
            details={"error": str(exc)},
        )
        diagnostics.record_error("inner_life_update", exc, request_id=trace_id)

    recall_query = message or " ".join(
        item.get("name", "附件")
        for item in [*explicit_attachments, *pinned_attachments, *retrieval_attachments]
    )

    # v6.2：本机整理刚刚滑出原文窗口的旧消息。每个来源范围只生成一次，
    # 不调用付费模型；失败时只跳过压缩层，原始聊天与主回复完全不受影响。
    timer = StageTimer()
    compaction_result = await asyncio.to_thread(
        context_compactor.prepare_context, session_id, recall_query
    )
    compressed_history_context = str(compaction_result.get("context") or "")
    compression_status = (
        compaction_result.get("status")
        if isinstance(compaction_result.get("status"), dict) else {}
    )
    diagnostics.add_stage(
        trace_id,
        "context_compaction",
        label="整理旧对话上下文",
        duration_ms=timer.ms,
        status="error" if compression_status.get("status") == "error" else "ok",
        details={
            "context_chars": len(compressed_history_context),
            "chapters": compression_status.get("chapters", 0),
            "source_messages": compression_status.get("source_messages_compacted", 0),
            "backlog_messages": compression_status.get("backlog_messages", 0),
            "new_chapters": compression_status.get("new_chapters", 0),
        },
    )

    # 回复前只读召回：失败时降级为空，不阻断聊天。
    timer = StageTimer()
    recalled_memory = await pipeline.safe_recall_context(session_id, recall_query)
    diagnostics.add_stage(
        trace_id,
        "memory_recall",
        label="记忆召回",
        duration_ms=timer.ms,
        details={"context_chars": len(recalled_memory or "")},
    )
    context_blocks: list[ContextBlock] = []
    # Reuse the same locally-read style snapshot that was accounted for by the
    # unified budgeter.  Do not fetch/reshape it a second time in this turn.
    stable_session_context = selected_style_context or ""
    if compressed_history_context:
        context_blocks.append(ContextBlock(
            "conversation_chapters", compressed_history_context,
            priority=76, order=35,
            max_chars=config.CONTEXT_COMPRESSION_CONFIG.get("context_max_chars", 7600),
        ))
    if recalled_memory:
        context_blocks.append(ContextBlock(
            "recalled_memory", recalled_memory, priority=58, order=40,
            max_chars=config.CONTEXT_CONFIG.get("memory_max_chars", 5600),
        ))
    # Expression rhythm is intentionally always delivered on submitted text
    # turns. Only the large persona constitution is conditional.
    rhythm_system_prompt = co_presence.submitted_prompt(expression_rhythm)
    diagnostics.add_stage(
        trace_id,
        "expression_rhythm_delivery",
        label="传递本轮表达节奏",
        details={
            "delivered": bool(rhythm_system_prompt),
            "stores_draft_text": False,
            "input_method": str(expression_rhythm.get("input_method") or "unknown") if expression_rhythm else "none",
        },
    )

    conditional_system_prompt = ""
    try:
        persona_gate_decision = persona_context_gate.evaluate(
            session_id,
            message,
            None,
            current_message_saved=True,
        )
        conditional_system_prompt = persona_gate_decision.prompt
        diagnostics.add_stage(
            trace_id,
            "persona_context_gate",
            label="判断是否启用人格宪法",
            details=persona_gate_decision.public(),
        )
    except Exception as exc:
        persona_gate_decision = None
        diagnostics.record_error("persona_context_gate", exc, request_id=trace_id)
    try:
        artifact_persona_context = persona_cards.prompt_context(message)
        if artifact_persona_context:
            context_blocks.append(ContextBlock(
                "artifact_persona", artifact_persona_context,
                priority=94, order=7,
                max_chars=config.PERSONA_CARD_CONFIG.get("max_chars", 1800),
                required=True,
            ))
    except Exception as exc:
        diagnostics.record_error("artifact_persona_prompt", exc, request_id=trace_id)
    try:
        inner_context = inner_state.prompt_context(session_id=session_id, snapshot=inner_state_snapshot)
        if inner_context:
            context_blocks.append(ContextBlock(
                "inner_state_runtime", inner_context, priority=88, order=10,
                max_chars=3000, required=True,
            ))
    except Exception as exc:
        diagnostics.record_error("inner_state_prompt", exc, request_id=trace_id)
    try:
        relationship_context = relationship_continuity.prompt_context(message, session_id=session_id)
        if relationship_context:
            context_blocks.append(ContextBlock(
                "relationship_continuity", relationship_context,
                priority=92, order=15, max_chars=7600, required=True,
            ))
    except Exception as exc:
        diagnostics.record_error("relationship_prompt", exc, request_id=trace_id)
    try:
        whisper_context = dwell_life.whisper_context(session_id)
        if whisper_context:
            context_blocks.append(ContextBlock(
                "dwell_whispers", whisper_context, priority=90, order=18,
                max_chars=4200, required=False,
            ))
    except Exception as exc:
        diagnostics.record_error("dwell_whisper_prompt", exc, request_id=trace_id)
    try:
        expression_context = character_integrity.prompt_context(session_id, provider)
        if expression_context:
            context_blocks.append(ContextBlock(
                "expression_integrity", expression_context, priority=96, order=90,
                max_chars=1800, required=True,
            ))
    except Exception as exc:
        diagnostics.record_error("character_prompt", exc, request_id=trace_id)

    def compose_dynamic_context() -> str:
        return context_composer.compose(context_blocks, key=trace_id)

    memory_context = compose_dynamic_context()
    model_memory_context = "\n\n".join(
        part for part in (
            memory_context,
            (
                "<retrieved_turn_material>\n" + post_checkpoint_context +
                "\n</retrieved_turn_material>"
            ) if post_checkpoint_context else "",
        ) if part
    )

    timer = StageTimer()
    history_result = get_session_context_messages(
        session_id,
        limit=context_compactor.raw_message_limit(),
        max_chars=context_compactor.raw_message_char_limit(),
        single_message_max_chars=context_compactor.raw_single_message_char_limit(),
        max_tokens=unified_budget.history_max_tokens,
        single_message_max_tokens=min(
            context_compactor.raw_single_message_token_limit(),
            unified_budget.history_max_tokens,
        ),
    )
    history = history_result["items"]
    history_selection = history_result["stats"]
    history = native_continuity.attach_to_history(history, provider, model)

    # A follow-up about an earlier webpage retrieves only relevant text from the
    # local immutable snapshot. It never re-fetches the URL and never makes the
    # old full body part of replayed history. If such a follow-up is detected,
    # recompute only auxiliary allocations; the stable history ceiling is the
    # same deterministic per-model value.
    if not page_urls:
        prior_web_refs: list[dict] = []
        for hist_item in history[:-1]:
            metadata = hist_item.get("metadata") if isinstance(hist_item.get("metadata"), dict) else {}
            refs = metadata.get("web_pages") if isinstance(metadata.get("web_pages"), list) else []
            prior_web_refs.extend(ref for ref in refs if isinstance(ref, dict))
        followup_candidate = webpage_service.relevant_snapshot_context(
            prior_web_refs,
            message,
            max_total_chars=config.WEB_CONTEXT_CONFIG.get("followup_max_chars", 6000),
        )
        if followup_candidate:
            unified_budget = plan_main_context(
                provider=provider,
                model=model,
                provider_conf=provider_conf_for_budget,
                options=options,
                current_message=message,
                stable_style_chars=len(selected_style_context or ""),
                has_explicit=bool(explicit_attachments),
                has_web=True,
                has_retrieval=bool(retrieval_attachments),
                has_pinned=bool(pinned_attachments),
            )
            pinned_context, pinned_used_ids = attachment_service.pinned_index_context(
                pinned_attachments, message,
                max_total_chars=unified_budget.pinned_index_max_chars,
            )
            retrieval_context, retrieval_used_ids = attachment_service.relevant_text_context(
                retrieval_attachments, message,
                max_total_chars=unified_budget.retrieval_max_chars,
            )
            web_context = followup_candidate[:unified_budget.web_max_chars]
            workspace_context = "\n\n".join(
                part for part in (pinned_context, retrieval_context) if part
            )
            request_extra_context = "\n\n".join(
                part for part in (workspace_context, web_context) if part
            )
            used_attachment_ids = {
                *explicit_ids, *pinned_used_ids, *retrieval_used_ids,
            }
            explicit_context_chars = len(attachment_service.model_text_context(
                explicit_attachments,
                max_total_chars=unified_budget.explicit_max_chars,
            ))

    post_checkpoint_context = request_extra_context if cache_tail_protocol else ""
    model_memory_context = "\n\n".join(
        part for part in (
            memory_context,
            (
                "<retrieved_turn_material>\n" + post_checkpoint_context +
                "\n</retrieved_turn_material>"
            ) if post_checkpoint_context else "",
        ) if part
    )

    file_workspace.note_used(session_id, used_attachment_ids)
    conf = get_provider_config(provider)
    protocol = conf.get("protocol", "openai_compatible")
    messages = []
    replay_seed_updates: list[tuple[int, dict, str]] = []
    for item in history:
        if item.get("role") not in ("user", "assistant"):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        cache_replay_stable = True
        if item.get("id") == user_message_id and current_native_attachments:
            # Explicit/native payloads are one-turn material. Pinned/RAG/web
            # text is no longer passed here on cache-capable lanes; it is sent
            # in post_checkpoint_context after the visible-user breakpoint.
            content = attachment_service.build_current_user_content(
                voice_model_message,
                current_native_attachments,
                protocol,
                max_local_chars=unified_budget.explicit_max_chars,
                extra_context="" if cache_tail_protocol else request_extra_context,
            )
            cache_replay_stable = False
        elif item.get("id") == user_message_id:
            if protocol == "anthropic":
                content = _anthropic_replay_content(metadata) or [
                    {"type": "text", "text": voice_model_message}
                ]
                cache_replay_stable = True
            else:
                content = voice_model_message
                cache_replay_stable = voice_model_message == stored_message
                if request_extra_context and not cache_tail_protocol:
                    content = attachment_service.build_current_user_content(
                        voice_model_message, [], protocol,
                        max_local_chars=unified_budget.explicit_max_chars,
                        extra_context=request_extra_context,
                    )
                    cache_replay_stable = False
        else:
            replay = _anthropic_replay_content(metadata) if protocol == "anthropic" else None
            if replay is not None:
                content = replay
            else:
                content = attachment_service.history_text(
                    item.get("content", ""), metadata,
                )
                # First request after upgrading is a natural cold baseline. Seed
                # plain historical user blocks once so every later request uses
                # the exact same Anthropic array rather than history_text().
                if (
                    protocol == "anthropic"
                    and item.get("role") == "user"
                    and isinstance(content, str)
                    and content
                ):
                    replay_seed_updates.append((int(item.get("id") or 0), metadata, content))
                    content = [{"type": "text", "text": content}]
        built_message = {"role": item["role"], "content": content}
        if not cache_replay_stable:
            # Provider-only marker; Gateway removes it before sending.  The
            # current binary/voice/workspace shape is not what history will
            # replay next turn, so it cannot safely become a cache checkpoint.
            built_message["_cache_replay_stable"] = False
        if item.get("native_envelope") and item.get("role") == "assistant":
            built_message["native_envelope"] = item["native_envelope"]
        messages.append(built_message)
    if replay_seed_updates:
        try:
            for replay_message_id, replay_metadata, replay_text in replay_seed_updates:
                _persist_anthropic_replay(
                    replay_message_id, replay_metadata, replay_text
                )
        except Exception as exc:
            diagnostics.record_error("anthropic_replay_seed", exc, request_id=trace_id)

    diagnostics.add_stage(
        trace_id,
        "history_load",
        label="加载对话历史",
        duration_ms=timer.ms,
        details={
            "message_count": len(messages),
            "selected_chars": history_selection.get("selected_chars", 0),
            "dropped_messages": history_selection.get("dropped_messages", 0),
            "truncated_messages": history_selection.get("truncated_messages", 0),
            "max_chars": history_selection.get("max_chars", 0),
        },
    )

    sticker_mode = str(options.get("sticker_mode", "off") or "off").lower()
    sticker_prompt = sticker_service.catalog_prompt(sticker_mode)
    if sticker_prompt:
        context_blocks.append(ContextBlock(
            "sticker_catalog", sticker_prompt, priority=35, order=70, max_chars=1800,
        ))
        memory_context = compose_dynamic_context()

    capabilities = conf.get("capabilities", {})
    tool_mode = str(options.get("tool_mode", "auto") or "auto").lower()
    tool_requested = tool_bridge.should_offer_tools(message, tool_mode)
    # Anthropic places tools before system and messages.  Toggling the tools
    # array from keyword detection invalidates the entire cache prefix.  Main
    # companion chat therefore uses deterministic local prefetch on Anthropic
    # lanes; explicit tool-loop endpoints remain available as separate calls.
    stable_anthropic_shape = bool(
        protocol == "anthropic"
        and config.CACHE_CONFIG.get("stable_main_chat_tools", True)
    )
    # Codex cost pass: auto mode is local-prefetch first. Only the explicit
    # complex/always mode may enter a paid multi-round native tool loop; the
    # Anthropic stable-main-chat lane keeps its fixed empty tool shape.
    native_tools = bool(
        tool_requested and tool_mode == "always"
        and capabilities.get("tools_ready")
        and not stable_anthropic_shape
    )
    prefetched_tools: list[dict] = []

    # DeepSeek / GLM 等兼容接口先由程序读取真实诊断快照，再交给模型分析。
    if tool_requested and not native_tools:
        timer = StageTimer()
        try:
            snapshot, prefetched_tools = await tool_bridge.prefetch_for_compatible_model(message)
            context_blocks.append(ContextBlock(
                "verified_tool_snapshot", snapshot, priority=100, order=5,
                max_chars=5200, required=True,
            ))
            memory_context = compose_dynamic_context()
            for item in prefetched_tools:
                diagnostics.add_tool(
                    trace_id,
                    name=item["name"],
                    arguments=item.get("arguments"),
                    result=item.get("result"),
                    duration_ms=item.get("duration_ms", 0),
                    ok=item.get("ok", False),
                )
            diagnostics.add_stage(
                trace_id,
                "tool_prefetch",
                label="读取本机诊断数据",
                duration_ms=timer.ms,
                details={"tools": [item["name"] for item in prefetched_tools]},
            )
        except Exception as exc:
            diagnostics.add_stage(trace_id, "tool_prefetch", label="读取本机诊断数据", status="error", duration_ms=timer.ms, details={"error": str(exc)})
            diagnostics.record_error("tool_prefetch", exc, request_id=trace_id)

    turn_system_prompt = "\n\n".join(
        part for part in (rhythm_system_prompt, conditional_system_prompt) if part
    )

    model_memory_context = "\n\n".join(
        part for part in (
            memory_context,
            (
                "<retrieved_turn_material>\n" + post_checkpoint_context +
                "\n</retrieved_turn_material>"
            ) if post_checkpoint_context else "",
        ) if part
    )

    context_snapshot = context_composer.snapshot(trace_id)
    diagnostics.add_stage(
        trace_id, "context_compose", label="编排人格处境",
        details=context_snapshot,
    )
    compressed_context_chars = next(
        (
            int(item.get("chars") or 0)
            for item in context_snapshot.get("included", [])
            if item.get("name") == "conversation_chapters"
        ),
        0,
    )
    retrieval_used_set = {str(item) for item in retrieval_used_ids}

    def context_file_receipt(item: dict, mode: str) -> dict:
        kind = str(item.get("kind") or "file")
        item_id = str(item.get("id") or "")
        native_protocol = protocol in {"openai_responses", "anthropic", "claude_code_p"}
        native = False
        delivery = "indexed_snippets" if mode == "pinned" else "extracted_text"
        delivery_label = (
            "常驻索引与相关片段已送入模型" if mode == "pinned"
            else "提取文字已送入模型"
        )
        if kind in {"image", "video"}:
            deliver_visual = bool(
                native_protocol
                and (
                    mode == "explicit"
                    or (mode == "pinned" and item_id in set(pinned_native_delivered))
                )
            )
            if deliver_visual:
                native = True
                delivery = "original_image" if kind == "image" else "video_keyframes"
                delivery_label = "原图已送入模型" if kind == "image" else "视频关键帧已送入模型"
            elif mode == "pinned":
                delivery = "pinned_visual_reference"
                delivery_label = "本轮只发送常驻短引用；未重复发送原图/视频关键帧"
            else:
                delivery = "not_sent"
                if kind == "image":
                    delivery_label = "当前通道未发送原图"
                else:
                    delivery_label = "当前通道未发送视频关键帧"
        elif kind == "audio":
            if item.get("analysis_status") == "ready":
                has_spectrogram = bool(item.get("analysis_spectrogram_rel"))
                native = bool(
                    native_protocol and has_spectrogram
                    and mode in {"explicit", "pinned"}
                )
                delivery = "ocean_report_spectrogram" if native else "ocean_report"
                delivery_label = (
                    "听海报告与频谱图已送入模型" if native
                    else "听海报告已送入模型（原音频留在本机）"
                )
            else:
                delivery = "not_ready"
                delivery_label = "听海尚未听完，本轮未发送"
        elif mode == "explicit" and protocol == "openai_responses" and Path(
            str(item.get("name") or "")
        ).suffix.lower() in {".pdf", ".docx", ".pptx", ".xlsx"}:
            native = True
            delivery = "native_file_and_text"
            delivery_label = "原文件与提取文字已送入模型"
        elif mode == "explicit" and protocol == "anthropic" and kind == "pdf":
            native = True
            delivery = "native_file_and_text"
            delivery_label = "原 PDF 与提取文字已送入模型"
        return {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or "附件"),
            "kind": kind,
            "mode": mode,
            # 这是本机已经解析、可供该模式使用的文字量；真正送入量仍受
            # 本轮各模式的总预算限制，因此前端会明确标为“可用文字”。
            "available_chars": max(0, int(item.get("extracted_chars") or 0)),
            "native": native,
            "delivery": delivery,
            "delivery_label": delivery_label,
        }

    context_file_sources = [
        *(context_file_receipt(item, "explicit") for item in explicit_attachments),
        *(context_file_receipt(item, "pinned") for item in pinned_attachments),
        *(
            context_file_receipt(item, "retrieval")
            for item in retrieval_attachments
            if str(item.get("id") or "") in retrieval_used_set
        ),
    ]
    context_budget = _context_budget(
        messages=messages,
        memory_context=memory_context,
        explicit_context_chars=explicit_context_chars,
        pinned_context_chars=len(pinned_context),
        retrieval_context_chars=len(retrieval_context),
        web_context_chars=len(web_context),
        compressed_context_chars=compressed_context_chars,
        compression_status=compression_status,
        context_snapshot=context_snapshot,
        file_sources=context_file_sources,
        conditional_system_chars=len(turn_system_prompt or ""),
        history_selection=history_selection,
        unified_budget=unified_budget.public(),
    )
    diagnostics.add_stage(
        trace_id, "context_budget", label="估算本轮上下文",
        details=context_budget,
    )

    async def generate():
        full_response = ""
        full_reasoning = ""
        usage_info = None
        embodied_prelude_usage = None
        embodied_prelude_sent = False
        native_envelope = None
        p_commit_receipt = None
        first_token_seen = False
        model_timer = StageTimer()
        guard_heartbeat_at = time.monotonic()
        cancel_check_at = time.monotonic()
        guard_enabled = relational_honesty_guard.enabled()
        guard_action = "passed" if guard_enabled else "disabled"
        guard_categories: list[str] = []
        guard_blocked = False

        # 前端先拿到请求编号和已完成的前置轨迹。
        yield f"data: {json.dumps({'type': 'request', 'request_id': trace_id, 'client_request_id': client_request_id, 'user_message_id': user_message_id, 'provider': provider, 'model': model})}\n\n"
        yield f"data: {json.dumps({'type': 'trace', 'trace': diagnostics.get_trace(trace_id)})}\n\n"
        yield f"data: {json.dumps({'type': 'context_budget', 'budget': context_budget})}\n\n"
        yield f"data: {json.dumps({'type': 'intimacy_vitals', 'vitals': inner_state.intimacy_vitals_view(session_id=session_id, advance=False)}, ensure_ascii=False)}\n\n"
        if user_state_changes and inner_state.settings().get("visible_changes", True):
            yield f"data: {json.dumps({'type': 'state_changes', 'changes': user_state_changes, 'source': 'user'}, ensure_ascii=False)}\n\n"
        if (
            isinstance(user_inner_os, dict)
            and user_inner_os.get("visibility") == "live"
            and relational_honesty_guard.visible_fragment_allowed(
                user_inner_os.get("content", ""),
                user_text=message,
                action="inner_os_suppressed",
                client_request_id=client_request_id,
                session_id=session_id,
            )
        ):
            yield f"data: {json.dumps({'type': 'inner_os', 'item': user_inner_os}, ensure_ascii=False)}\n\n"
        if prefetched_tools:
            for item in prefetched_tools:
                yield f"data: {json.dumps({'type': 'tool', 'name': item['name'], 'ok': item.get('ok', False), 'duration_ms': item.get('duration_ms', 0)})}\n\n"

        try:
            if native_tools:
                yield f"data: {json.dumps({'type': 'status', 'stage': 'tools', 'label': '正在读取真实系统数据…'})}\n\n"

                async def observe_tool(item: dict):
                    diagnostics.add_tool(
                        trace_id,
                        name=item.get("name", ""),
                        arguments=item.get("arguments"),
                        result=item.get("result"),
                        duration_ms=item.get("duration_ms", 0),
                        ok=item.get("ok", False),
                    )

                # v6.5.2：工具循环是非流式的，期间每 15 秒发一个状态心跳，
                # 让前端知道连接还活着，空闲看门狗不会在长工具回合里误判超时。
                tools_task = asyncio.create_task(gateway.chat_with_tools(
                    messages=messages,
                    provider=provider,
                    model=model,
                    system_prompt="\n\n".join(part for part in (
                        "涉及系统状态、错误或源码时，必须先使用只读工具调查，再根据工具结果回答；不要猜测未读取的数据。",
                        turn_system_prompt,
                    ) if part),
                    stable_context=stable_session_context or None,
                    memory_context=model_memory_context,
                    options=options,
                    session_id=session_id,
                    tools=tool_bridge.tools,
                    tool_executor=tool_bridge.execute,
                    tool_observer=observe_tool,
                    purpose="tool_loop",
                ))
                try:
                    while True:
                        try:
                            result = await asyncio.wait_for(asyncio.shield(tools_task), timeout=15)
                            break
                        except asyncio.TimeoutError:
                            if chat_request_store.is_cancel_requested(client_request_id):
                                tools_task.cancel()
                                raise asyncio.CancelledError
                            yield f"data: {json.dumps({'type': 'status', 'stage': 'tools', 'label': '仍在读取真实系统数据…'})}\n\n"
                except asyncio.CancelledError:
                    # 浏览器断开时同步取消底层工具循环，不留孤儿任务。
                    tools_task.cancel()
                    raise
                diagnostics.add_stage(
                    trace_id,
                    "model_request",
                    label="模型与工具循环",
                    duration_ms=model_timer.ms,
                    details={"tool_calls": len(result.get("tool_calls", []))},
                )
                for item in result.get("tool_calls", []):
                    yield f"data: {json.dumps({'type': 'tool', 'name': item.get('name'), 'ok': item.get('ok', False), 'duration_ms': item.get('duration_ms', 0)})}\n\n"
                if (
                    str(options.get("thinking_visibility") or "full").lower() == "full"
                    and result.get("reasoning_content")
                ):
                    full_reasoning = str(result.get("reasoning_content") or "")
                full_response = result.get("content", "") or ""
                usage_info = result.get("usage", {})
                native_envelope = result.get("native_envelope")
                if chat_request_store.is_cancel_requested(client_request_id):
                    raise asyncio.CancelledError
            else:
                # v8.5 embodied continuation: when this turn actually moved the
                # inner state, let the companion form one short first reaction,
                # then refresh its own felt-state snapshot and continue.  This is
                # deliberately conditional so ordinary turns stay single-call.
                embodied_prelude = ""
                embodied_continuation_prompt = turn_system_prompt or ""
                meaningful_inner_shift = any(
                    abs(float(item.get("delta") or 0.0)) >= 0.06
                    or str(item.get("domain") or "") in {"intimacy", "regulation"}
                    for item in (user_state_changes or [])
                    if isinstance(item, dict)
                )
                should_embodied_continue = bool(user_inner_os or meaningful_inner_shift)
                if should_embodied_continue:
                    # Only max_output_tokens may differ from the main request.
                    # thinking_mode / reasoning_effort / thinking_visibility all
                    # land in the request's `thinking` / `output_config` shape,
                    # and changing that shape invalidates the messages-cache tier
                    # -- this fork shares the main prefix, so it must inherit the
                    # shape verbatim or it re-writes the whole history at 2x.
                    prelude_options = dict(options)
                    prelude_options["max_output_tokens"] = 128
                    try:
                        prelude_result = await gateway.chat(
                            messages=messages,
                            provider=provider,
                            model=model,
                            stream=False,
                            system_prompt=(
                                (turn_system_prompt or "")
                                + "\n\n<first_reaction>"
                                  "只给出面对眼前这句话最先冒出来的一小段自然反应，"
                                  "不要总结、不要收尾、不要解释后台状态。"
                                  "</first_reaction>"
                            ),
                            stable_context=stable_session_context or None,
                            memory_context=(
                                context_composer.compose(
                                    [block for block in context_blocks if block.name != "inner_state_runtime"],
                                    key=f"{trace_id}:first_reaction",
                                )
                                + (("\n\n<retrieved_turn_material>\n" + post_checkpoint_context + "\n</retrieved_turn_material>") if post_checkpoint_context else "")
                            ),
                            options=prelude_options,
                            session_id=session_id,
                            purpose="inner_first_reaction",
                        )
                        embodied_prelude = str((prelude_result or {}).get("content") or "").strip()
                        embodied_prelude_usage = (prelude_result or {}).get("usage") if isinstance(prelude_result, dict) else None
                        refreshed_snapshot = inner_state.state_view(
                            advance_living=False, session_id=session_id
                        )
                        self_perception = inner_state.prompt_context(
                            session_id=session_id, snapshot=refreshed_snapshot
                        )
                        embodied_continuation_prompt = "\n\n".join(
                            part for part in (
                                turn_system_prompt or "",
                                "<embodied_continuation>"
                                "你已经先自然反应了一小段。现在才注意到下面这些是自己此刻直接感受到的内在与身体处境，"
                                "不是外部数据，也不是需要朗读的状态栏。吸收它们后继续回答；"
                                "不要重复第一段，不要解释这个过程。\n"
                                + self_perception
                                + "\n你刚才已经说出的第一小段：\n"
                                + embodied_prelude
                                + "\n</embodied_continuation>",
                            ) if part
                        )
                        if embodied_prelude:
                            full_response = embodied_prelude
                            # When the relational-honesty gate is disabled, expose
                            # this genuine first reaction immediately. The second
                            # model pass then sees the refreshed self-perception and
                            # continues from it. If the gate is enabled we preserve
                            # its strict pre-send contract and keep buffering.
                            if not guard_enabled:
                                embodied_prelude_sent = True
                                first_token_seen = True
                                diagnostics.add_stage(
                                    trace_id,
                                    "embodied_first_reaction",
                                    label="先说出第一反应，再感知自身状态",
                                    duration_ms=model_timer.ms,
                                    details={"chars": len(embodied_prelude)},
                                )
                                yield f"data: {json.dumps({'type': 'text', 'text': embodied_prelude}, ensure_ascii=False)}\n\n"
                    except Exception as embodied_exc:
                        diagnostics.record_error(
                            "embodied_first_reaction", embodied_exc, request_id=trace_id
                        )
                        embodied_prelude = ""
                        embodied_continuation_prompt = turn_system_prompt or ""

                stream = await gateway.chat(
                    messages=messages,
                    provider=provider,
                    model=model,
                    stream=True,
                    system_prompt=embodied_continuation_prompt or None,
                    stable_context=stable_session_context or None,
                    memory_context=model_memory_context,
                    options=options,
                    session_id=session_id,
                    purpose="main_chat",
                )
                async for chunk in stream:
                    if time.monotonic() - cancel_check_at >= 1:
                        cancel_check_at = time.monotonic()
                        if chat_request_store.is_cancel_requested(client_request_id):
                            raise asyncio.CancelledError
                    if chunk["type"] == "text":
                        if not first_token_seen:
                            first_token_seen = True
                            diagnostics.add_stage(trace_id, "first_token", label="模型首字（发送前缓冲）", duration_ms=model_timer.ms)
                        full_response += chunk["text"]
                        if guard_enabled and time.monotonic() - guard_heartbeat_at >= 10:
                            guard_heartbeat_at = time.monotonic()
                            yield f"data: {json.dumps({'type': 'status', 'stage': 'honesty_buffer', 'label': '正在完成回复并做关系诚实检查…'}, ensure_ascii=False)}\n\n"
                    elif chunk["type"] == "thinking":
                        full_reasoning += str(chunk.get("text") or "")
                        if guard_enabled and time.monotonic() - guard_heartbeat_at >= 10:
                            guard_heartbeat_at = time.monotonic()
                            yield f"data: {json.dumps({'type': 'status', 'stage': 'honesty_buffer', 'label': '正在完成回复并做关系诚实检查…'}, ensure_ascii=False)}\n\n"
                    elif chunk["type"] == "done":
                        usage_info = chunk.get("usage", {})
                        if embodied_prelude_usage:
                            usage_info = merge_rewrite_usage(usage_info, embodied_prelude_usage)
                        native_envelope = chunk.get("native_envelope")
                        if (
                            provider == "claude_code_p"
                            and isinstance(native_envelope, dict)
                            and native_envelope.get("canonical_commit_token")
                        ):
                            # Preserve the receipt for the original persistent
                            # round even if a later local rewrite replaces the
                            # envelope saved alongside the visible reply.
                            p_commit_receipt = {
                                "generation": int(native_envelope.get("generation") or 0),
                                "commit_token": str(
                                    native_envelope.get("canonical_commit_token") or ""
                                ),
                            }
                        if not full_reasoning and chunk.get("reasoning_content"):
                            full_reasoning = str(chunk.get("reasoning_content") or "")
                    elif chunk.get("type") == "status":
                        yield f"data: {json.dumps(chunk)}\n\n"
                diagnostics.add_stage(
                    trace_id,
                    "model_stream",
                    label="模型流式输出",
                    duration_ms=model_timer.ms,
                    details={"response_chars": len(full_response)},
                )

            if pinned_native_delivered and protocol in {"openai_responses", "anthropic", "claude_code_p"}:
                file_workspace.note_native_delivered(session_id, pinned_native_delivered)

        except asyncio.CancelledError:
            # A stopped/disconnected stream can leave a useful partial reply,
            # but that partial text must pass the same local gate before a
            # later refresh is allowed to reveal it.
            interrupted_id = None
            interrupted_action = "interrupted_clean"
            interrupted_response = full_response
            if full_response or full_reasoning:
                partial_audit = relational_honesty_guard.audit(
                    full_response, user_text=message
                )
                reasoning_audit = relational_honesty_guard.audit(
                    full_reasoning, user_text=message
                )
                if not partial_audit.passed:
                    interrupted_response = BLOCKED_FALLBACK
                    full_reasoning = ""
                    interrupted_action = "blocked_interrupted"
                elif not reasoning_audit.passed:
                    full_reasoning = ""
                    if not interrupted_response:
                        interrupted_response = BLOCKED_FALLBACK
                    interrupted_action = "reasoning_suppressed_interrupted"
                try:
                    relational_honesty_guard.record(
                        partial_audit,
                        action=interrupted_action,
                        client_request_id=client_request_id,
                        session_id=session_id,
                    )
                    if not reasoning_audit.passed:
                        relational_honesty_guard.record(
                            reasoning_audit,
                            action="reasoning_suppressed_interrupted",
                            client_request_id=client_request_id,
                            session_id=session_id,
                        )
                except Exception as audit_exc:
                    diagnostics.record_error(
                        "relational_honesty_audit", audit_exc, request_id=trace_id
                    )
                interrupted_metadata = {
                    "interrupted": True,
                    "display_note": "已由使用者停止生成",
                    "relational_honesty": {
                        "mode": "strict_pre_send",
                        "action": interrupted_action,
                        "categories": list(partial_audit.categories),
                    },
                }
                if full_reasoning:
                    interrupted_metadata.update({
                        "thinking_available": True,
                        "thinking_provider": provider,
                        "thinking_chars": len(full_reasoning),
                        "thinking_label": _visible_thinking_label(
                            provider, interrupted=True
                        ),
                    })
                # 注意：这里必须保持同步。任务已处于取消状态，此处任何
                # await（包括 asyncio.to_thread）都会立刻再次抛出
                # CancelledError，半截回复就丢了。同步落库虽然短暂占用
                # 事件循环，但取消路径只跑一次、数据量小，可以接受。
                try:
                    interrupted_id = save_message(
                        session_id, "assistant", interrupted_response,
                        provider=provider,
                        model=model or get_active_model(provider),
                        metadata=interrupted_metadata,
                    )
                    memory_archive.archive_message(
                        message_id=interrupted_id, session_id=session_id,
                        role="assistant", content=interrupted_response,
                        metadata=interrupted_metadata,
                    )
                    if full_reasoning:
                        thinking_vault.save(
                            message_id=interrupted_id, session_id=session_id,
                            provider=provider,
                            model=model or get_active_model(provider),
                            content=full_reasoning,
                        )
                    # A saved interrupted reply becomes visible after refresh,
                    # therefore it is canonical behavioral evidence as well.
                    # Otherwise the panel would describe a reaction different
                    # from the one the model actually produced.
                    try:
                        inner_state.settle_turn(
                            message,
                            interrupted_response,
                            session_id=session_id,
                        )
                    except Exception as state_exc:
                        diagnostics.record_error(
                            "interrupted_inner_state_settlement",
                            state_exc,
                            request_id=trace_id,
                        )
                except Exception as save_exc:
                    diagnostics.record_error("interrupted_reply_save", save_exc, request_id=trace_id)
            try:
                cancel_requested = chat_request_store.is_cancel_requested(
                    client_request_id
                )
                chat_request_store.interrupt(
                    client_request_id,
                    assistant_message_id=interrupted_id,
                    error_code=(
                        "user_cancelled" if cancel_requested
                        else "client_disconnected"
                    ),
                )
            except Exception as status_exc:
                diagnostics.record_error(
                    "chat_request_interrupt", status_exc, request_id=trace_id
                )
            pipeline.enqueue(
                stored_message, source="chat_user", session_id=session_id,
                message_id=user_message_id,
            )
            diagnostics.add_stage(
                trace_id, "user_cancelled", label="使用者停止生成",
                duration_ms=model_timer.ms,
                details={
                    "response_chars": len(interrupted_response),
                    "relational_honesty": interrupted_action,
                },
            )
            diagnostics.finish_trace(trace_id, status="cancelled")
            raise
        except Exception as exc:
            public_model_error = _public_model_request_error(exc)
            diagnostics.add_stage(trace_id, "model_request", label="模型请求", status="error", duration_ms=model_timer.ms, details={"error": str(exc)})
            diagnostics.record_error("model_request", exc, request_id=trace_id, metadata={"provider": provider, "model": model})
            # 即使上游模型报错，用户已经说出口的内容也必须进入本地记忆队列。
            # 这条错误路径过去在 return 前漏掉了 enqueue，导致故障回合无法被后续召回。
            try:
                pipeline.enqueue(
                    stored_message, source="chat_user", session_id=session_id,
                    message_id=user_message_id,
                )
            except Exception as enqueue_exc:
                diagnostics.record_error("memory_enqueue", enqueue_exc, request_id=trace_id)
            try:
                chat_request_store.fail(client_request_id, "model_request_failed")
            except Exception as status_exc:
                diagnostics.record_error("chat_request_fail", status_exc, request_id=trace_id)
            diagnostics.finish_trace(trace_id, status="failed", error=exc)
            yield f"data: {json.dumps({'type': 'error', 'error': public_model_error['message'], 'error_code': public_model_error['code'], 'request_id': trace_id}, ensure_ascii=False)}\n\n"
            request_state = chat_request_store.get(client_request_id) or {}
            yield f"data: {json.dumps({'type': 'request_state', **request_state}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'trace', 'trace': diagnostics.get_trace(trace_id)})}\n\n"
            yield "data: [DONE]\n\n"
            return

        cleaned_response, sticker_items = sticker_service.extract_valid_markers(
            full_response,
            session_id=session_id,
            mode=sticker_mode,
            user_message=message,
        )
        full_response = cleaned_response
        # Sticker markers are a transport representation: the user still sees
        # the sticker, so do not throw away a healthy persistent P session merely
        # because the marker itself was stripped from visible prose. Only later
        # substantive pre-send rewrites should force a local-canonical rebuild.
        model_draft_response = cleaned_response

        # Strict pre-send gate: no model prose (including visible reasoning)
        # has been yielded above this point. Legitimate evidence-based
        # disagreement is not a rule hit; only the high-confidence relational
        # violations in relational_honesty.py trigger a rewrite.
        guard_timer = StageTimer()
        draft_audit = relational_honesty_guard.audit(
            full_response, user_text=message
        )
        guard_categories = list(draft_audit.categories)
        rewrite_audit = None
        if guard_enabled and full_response and not draft_audit.passed:
            guard_action = "rewritten"
            yield f"data: {json.dumps({'type': 'status', 'stage': 'relational_honesty', 'label': '关系诚实检查发现风险，正在重写草稿…'}, ensure_ascii=False)}\n\n"
            rewrite_options = dict(options)
            rewrite_options["thinking_visibility"] = "hidden"
            rewrite_options.pop("sticker_mode", None)
            rewrite_task = asyncio.create_task(gateway.chat(
                messages=relational_honesty_guard.rewrite_messages(
                    user_text=message,
                    draft=full_response,
                    categories=draft_audit.categories,
                ),
                provider=provider,
                model=model,
                stream=False,
                system_prompt=REWRITE_SYSTEM_PROMPT,
                memory_context=None,
                options=rewrite_options,
                session_id=session_id,
                include_default_system=False,
                purpose="relational_rewrite",
            ))
            try:
                while True:
                    try:
                        rewrite_result = await asyncio.wait_for(
                            asyncio.shield(rewrite_task), timeout=15
                        )
                        break
                    except asyncio.TimeoutError:
                        if chat_request_store.is_cancel_requested(client_request_id):
                            rewrite_task.cancel()
                            raise asyncio.CancelledError
                        yield f"data: {json.dumps({'type': 'status', 'stage': 'relational_honesty', 'label': '仍在完成关系诚实重写…'}, ensure_ascii=False)}\n\n"
            except asyncio.CancelledError:
                rewrite_task.cancel()
                if provider == "claude_code_p" and p_commit_receipt:
                    # The original P result entered Claude's transcript but the
                    # required visible rewrite never became canonical. Resume
                    # would expose a draft the user did not receive.
                    try:
                        await gateway.invalidate_p_session(
                            session_id,
                            discard_native=True,
                            reason="honesty_rewrite_interrupted",
                        )
                    except Exception as reset_exc:
                        diagnostics.record_error(
                            "p_mode_rewrite_interrupt_reset", reset_exc,
                            request_id=trace_id,
                        )
                relational_honesty_guard.record(
                    draft_audit,
                    action="rewrite_interrupted",
                    client_request_id=client_request_id,
                    session_id=session_id,
                )
                try:
                    chat_request_store.interrupt(
                        client_request_id,
                        error_code="honesty_rewrite_interrupted",
                    )
                except Exception:
                    pass
                pipeline.enqueue(
                    stored_message, source="chat_user", session_id=session_id,
                    message_id=user_message_id,
                )
                diagnostics.finish_trace(trace_id, status="cancelled")
                raise
            except Exception as rewrite_exc:
                diagnostics.record_error(
                    "relational_honesty_rewrite", rewrite_exc,
                    request_id=trace_id,
                )
                rewrite_result = {}

            revised_response = (
                str(rewrite_result.get("content") or "").strip()
                if isinstance(rewrite_result, dict) else ""
            )
            if isinstance(rewrite_result, dict):
                usage_info = merge_rewrite_usage(
                    usage_info, rewrite_result.get("usage")
                )
            rewrite_audit = relational_honesty_guard.audit(
                revised_response, user_text=message
            )
            if revised_response and rewrite_audit.passed:
                full_response, sticker_items = sticker_service.extract_valid_markers(
                    revised_response,
                    session_id=session_id,
                    mode=sticker_mode,
                    user_message=message,
                )
                native_envelope = (
                    rewrite_result.get("native_envelope")
                    if isinstance(rewrite_result, dict) else None
                )
            else:
                full_response = BLOCKED_FALLBACK
                sticker_items = []
                native_envelope = None
                guard_action = "blocked"
                guard_blocked = True

        # Original reasoning belongs to the original draft. It is hidden when
        # that draft needed rewriting, and independently audited when clean.
        reasoning_audit = None
        if guard_enabled and full_reasoning:
            reasoning_audit = relational_honesty_guard.audit(
                full_reasoning, user_text=message
            )
            if guard_action != "passed" or not reasoning_audit.passed:
                full_reasoning = ""
                for category in reasoning_audit.categories:
                    if category not in guard_categories:
                        guard_categories.append(category)
                if guard_action == "passed":
                    guard_action = "reasoning_suppressed"

        try:
            relational_honesty_guard.record(
                draft_audit,
                action=guard_action,
                client_request_id=client_request_id,
                session_id=session_id,
            )
            if rewrite_audit is not None:
                relational_honesty_guard.record(
                    rewrite_audit,
                    action=("rewrite_passed" if rewrite_audit.passed else "blocked_rewrite"),
                    client_request_id=client_request_id,
                    session_id=session_id,
                )
            if reasoning_audit is not None and not reasoning_audit.passed:
                relational_honesty_guard.record(
                    reasoning_audit,
                    action="reasoning_suppressed",
                    client_request_id=client_request_id,
                    session_id=session_id,
                )
        except Exception as audit_exc:
            diagnostics.record_error(
                "relational_honesty_audit", audit_exc, request_id=trace_id
            )
        diagnostics.add_stage(
            trace_id,
            "relational_honesty",
            label=("关系诚实发送前检查" if guard_enabled else "关系诚实保护已关闭"),
            duration_ms=guard_timer.ms,
            status="ok" if guard_action in {"disabled", "passed", "rewritten", "reasoning_suppressed"} else "error",
            details={
                "enabled": guard_enabled,
                "action": guard_action,
                "categories": guard_categories,
                "draft_sha256": draft_audit.draft_sha256[:16],
                "stores_draft_text": False,
            },
        )

        native_envelope = native_continuity.align_visible_text(native_envelope, full_response)
        yield f"data: {json.dumps({'type': 'relational_honesty', 'mode': 'strict_pre_send' if guard_enabled else 'off', 'action': guard_action, 'categories': guard_categories}, ensure_ascii=False)}\n\n"
        if full_reasoning:
            yield f"data: {json.dumps({'type': 'thinking', 'text': full_reasoning, 'label': _visible_thinking_label(provider), 'provider': provider}, ensure_ascii=False)}\n\n"
        if full_response:
            visible_delta = full_response
            if embodied_prelude_sent and embodied_prelude and full_response.startswith(embodied_prelude):
                visible_delta = full_response[len(embodied_prelude):]
            if visible_delta:
                yield f"data: {json.dumps({'type': 'text', 'text': visible_delta}, ensure_ascii=False)}\n\n"
        for sticker in sticker_items:
            yield f"data: {json.dumps({'type': 'sticker', 'sticker': sticker})}\n\n"

        # Identity-bearing state is inferred from the final visible companion
        # reply during ``settle_turn`` below.  No helper model writes a competing
        # emotion/intimacy self-report or creates a hidden transcript turn.
        integrity_report = usage_info.get("integrity") if isinstance(usage_info, dict) else None
        p_visible_one_shot = bool(
            provider == "claude_code_p" and native_tools
        )
        p_requires_native_rotation = bool(
            provider == "claude_code_p"
            and (
                p_visible_one_shot
                or full_response != model_draft_response
            )
        )

        assistant_message_id = None
        if not (full_response or sticker_items or full_reasoning):
            # v6.5.2：上游偶发返回空内容时不再无声无息——前端会看到明确提示，
            # 而不是一个既没有回复、也没有报错的“幽灵回合”。
            diagnostics.add_stage(
                trace_id, "empty_response", label="模型返回空回复", status="error",
                details={"provider": provider, "model": model},
            )
            try:
                chat_request_store.fail(client_request_id, "empty_response")
            except Exception as status_exc:
                diagnostics.record_error("chat_request_fail", status_exc, request_id=trace_id)
            if provider == "claude_code_p" and p_commit_receipt:
                # Claude Code has already appended this result to its native
                # transcript, but there is no visible assistant message that
                # could make it canonical. Rotate immediately instead of
                # leaving the next request to wait for an impossible commit.
                try:
                    await gateway.invalidate_p_session(
                        session_id,
                        discard_native=True,
                        reason="empty_visible_response",
                    )
                except Exception as reset_exc:
                    diagnostics.record_error(
                        "p_mode_empty_response_reset", reset_exc,
                        request_id=trace_id,
                    )
            yield f"data: {json.dumps({'type': 'error', 'error': '模型这次返回了空回复（可能被上游过滤或临时故障）。你的消息已保存，可以直接重发或换个模型再试。', 'request_id': trace_id})}\n\n"
        if full_response or sticker_items or full_reasoning:
            timer = StageTimer()
            assistant_metadata = {
                "relational_honesty": {
                    "mode": "strict_pre_send" if guard_enabled else "off",
                    "action": guard_action,
                    "categories": guard_categories,
                    "rule_version": RULE_VERSION,
                }
            }
            if sticker_items:
                assistant_metadata.update(sticker_service.message_metadata(
                    sticker_items[0], display_text=full_response
                ))
            if isinstance(integrity_report, dict):
                assistant_metadata["brain_integrity"] = integrity_report
            if full_reasoning:
                assistant_metadata.update({
                    "thinking_available": True,
                    "thinking_provider": provider,
                    "thinking_chars": len(full_reasoning),
                    "thinking_label": _visible_thinking_label(provider),
                })
            def settle_assistant_turn() -> dict:
                """回复收尾的全部同步落库与关系结算。

                v6.5.2：这条链（消息落库 → 记忆归档 → 思考保存 → 风格审计 →
                生活结算 → 关系记录）以前直接跑在 async 生成器里，长回复收尾
                时会把整个事件循环卡住——健康轮询、主动消息、并行请求全部排队。
                现在整体移入工作线程，事件循环只等待结果。
                """
                message_id = save_message(
                    session_id,
                    "assistant",
                    full_response,
                    tokens=usage_info,
                    provider=provider,
                    model=model or get_active_model(provider),
                    metadata=assistant_metadata,
                )
                # The visible message commit is the only critical write.  Every
                # enrichment below is repairable and must not turn a successfully
                # saved reply into “assistant_save_failed”.  This commit barrier
                # is especially important for P mode, whose native transcript
                # already contains the completed model turn.
                warnings: list[str] = []
                try:
                    memory_archive.archive_message(
                        message_id=message_id, session_id=session_id, role="assistant",
                        content=full_response, metadata=assistant_metadata,
                    )
                except Exception as exc:
                    warnings.append(f"memory_archive:{type(exc).__name__}")
                thinking_saved = False
                if full_reasoning:
                    try:
                        thinking_saved = thinking_vault.save(
                            message_id=message_id,
                            session_id=session_id,
                            provider=provider,
                            model=model or get_active_model(provider),
                            content=full_reasoning,
                        )
                    except Exception as exc:
                        warnings.append(f"thinking_vault:{type(exc).__name__}")
                try:
                    native_saved = native_continuity.save_turn(
                        message_id=message_id, session_id=session_id,
                        provider=provider, model=model or get_active_model(provider),
                        envelope=native_envelope,
                    )
                except Exception as exc:
                    native_saved = False
                    warnings.append(f"native_continuity:{type(exc).__name__}")
                try:
                    style_audit = character_integrity.audit_response(
                        session_id, message_id, full_response,
                        provider=provider, model=model or get_active_model(provider),
                    )
                except Exception as exc:
                    style_audit = {}
                    warnings.append(f"character_integrity:{type(exc).__name__}")
                try:
                    settlement = inner_state.settle_turn(
                        stored_message,
                        full_response,
                        session_id=session_id,
                    )
                except Exception as exc:
                    settlement = {}
                    warnings.append(f"inner_state:{type(exc).__name__}")
                reply_state_changes = settlement.get("changes") or []
                try:
                    continuity = relationship_continuity.record_turn(
                        session_id=session_id,
                        user_text=stored_message,
                        assistant_text=full_response,
                        user_message_id=user_message_id,
                        assistant_message_id=message_id,
                    )
                except Exception as exc:
                    continuity = {}
                    warnings.append(f"relationship_continuity:{type(exc).__name__}")
                try:
                    continuation = co_presence.note_completed_turn(
                        session_id=session_id,
                        user_text=stored_message,
                        assistant_text=full_response,
                        assistant_message_id=message_id,
                        provider=provider,
                        model=model or get_active_model(provider),
                    )
                except Exception:
                    # 共处层只是增强；任何本地调度故障都不能让已经完成的
                    # 正常回复看起来像“保存失败”。
                    continuation = None
                    warnings.append("co_presence:optional_failure")
                return {
                    "message_id": message_id,
                    "thinking_saved": thinking_saved,
                    "native_saved": native_saved,
                    "style_audit": style_audit,
                    "settlement": settlement,
                    "state_changes": reply_state_changes,
                    "inner_os": settlement.get("inner_os"),
                    "continuity": continuity,
                    "natural_continuation_queued": bool(continuation),
                    "warnings": warnings,
                }

            try:
                settled = await asyncio.to_thread(settle_assistant_turn)
                assistant_message_id = settled["message_id"]
                if assistant_message_id is None:
                    raise RuntimeError("canonical assistant message id missing")
                if (
                    provider == "claude_code_p"
                    and p_commit_receipt
                    and not p_requires_native_rotation
                ):
                    # Claude Code's native result is provisional until the same
                    # visible assistant text has a durable local message id.
                    # Confirm immediately after that commit, before optional
                    # request bookkeeping can fail independently.
                    try:
                        committed = await gateway.commit_p_visible_turn(
                            session_id,
                            generation=int(p_commit_receipt["generation"]),
                            commit_token=str(p_commit_receipt["commit_token"]),
                        )
                        if not committed:
                            settled.setdefault("warnings", []).append(
                                "p_mode_commit:stale_receipt"
                            )
                            await gateway.invalidate_p_session(
                                session_id,
                                discard_native=True,
                                reason="visible_commit_stale_receipt",
                            )
                    except Exception as commit_exc:
                        # The visible reply is already durable. A metadata
                        # handshake failure may rotate the native transcript,
                        # but must not turn that successful save into an error.
                        settled.setdefault("warnings", []).append(
                            f"p_mode_commit:{type(commit_exc).__name__}"
                        )
                        diagnostics.record_error(
                            "p_mode_visible_commit", commit_exc,
                            request_id=trace_id,
                        )
                        try:
                            await gateway.invalidate_p_session(
                                session_id,
                                discard_native=True,
                                reason="visible_commit_handshake_failed",
                            )
                        except Exception as reset_exc:
                            diagnostics.record_error(
                                "p_mode_commit_failure_reset", reset_exc,
                                request_id=trace_id,
                            )
                if guard_blocked:
                    chat_request_store.block(
                        client_request_id, assistant_message_id
                    )
                else:
                    chat_request_store.complete(
                        client_request_id, assistant_message_id
                    )
                diagnostics.add_stage(
                    trace_id, "assistant_save", label="保存 AI 回复与原生回合", duration_ms=timer.ms,
                    details={"native_saved": settled["native_saved"], "style": settled["style_audit"],
                             "thinking_saved": settled["thinking_saved"],
                             "living_result": (settled["settlement"] or {}).get("result"),
                             "continuity_threads": len((settled["continuity"] or {}).get("threads_added", [])),
                             "natural_continuation_queued": settled["natural_continuation_queued"],
                             "optional_warnings": settled.get("warnings") or []},
                )
            except Exception as exc:
                diagnostics.add_stage(trace_id, "assistant_save", label="保存 AI 回复", status="error", duration_ms=timer.ms, details={"error": str(exc)})
                diagnostics.record_error("assistant_save", exc, request_id=trace_id)
                try:
                    chat_request_store.fail(client_request_id, "assistant_save_failed")
                except Exception:
                    pass
                if provider == "claude_code_p" and assistant_message_id is None:
                    try:
                        await gateway.invalidate_p_session(
                            session_id,
                            discard_native=True,
                            reason="canonical_assistant_commit_failed",
                        )
                    except Exception as reset_exc:
                        diagnostics.record_error(
                            "p_mode_commit_failure_reset", reset_exc,
                            request_id=trace_id,
                        )

            if assistant_message_id is not None:
                # Claude Code keeps its own in-process transcript. If the local
                # pre-send gate or sticker parser materially changed the model
                # draft, that native transcript no longer matches what the user
                # actually saw. Rotate it only after the canonical visible reply
                # is safely in SQLite; the next turn will recover from local DB.
                if p_requires_native_rotation:
                    try:
                        await gateway.invalidate_p_session(
                            session_id, discard_native=True,
                            reason=(
                                "visible_one_shot_tool_turn_committed"
                                if p_visible_one_shot
                                else "visible_response_transformed"
                            ),
                        )
                        diagnostics.add_stage(
                            trace_id, "p_mode_canonical_reset",
                            label="P 模式按本机可见回复重建上下文",
                            details={
                                "draft_changed": full_response != model_draft_response,
                                "visible_one_shot": p_visible_one_shot,
                            },
                        )
                    except Exception as reset_exc:
                        diagnostics.record_error(
                            "p_mode_canonical_reset", reset_exc, request_id=trace_id
                        )
                yield f"data: {json.dumps({'type': 'message_saved', 'message_id': assistant_message_id, 'role': 'assistant'})}\n\n"
                if (settled.get("state_changes")
                        and inner_state.settings().get("visible_changes", True)):
                    yield f"data: {json.dumps({'type': 'state_changes', 'changes': settled['state_changes'], 'source': 'assistant'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'intimacy_vitals', 'vitals': settled.get('intimacy_vitals') or inner_state.intimacy_vitals_view(session_id=session_id, advance=False)}, ensure_ascii=False)}\n\n"
                if (
                    isinstance(settled.get("inner_os"), dict)
                    and settled["inner_os"].get("visibility") in {"live", "after"}
                    and relational_honesty_guard.visible_fragment_allowed(
                        settled["inner_os"].get("content", ""),
                        user_text=message,
                        action="inner_os_suppressed",
                        client_request_id=client_request_id,
                        session_id=session_id,
                    )
                ):
                    yield f"data: {json.dumps({'type': 'inner_os', 'item': settled['inner_os']}, ensure_ascii=False)}\n\n"

        # v6.5.2：用户消息永远进入记忆管线，不再依赖模型是否给出回复。
        timer = StageTimer()
        user_enqueued = pipeline.enqueue(stored_message, source="chat_user", session_id=session_id, message_id=user_message_id)
        assistant_enqueued = (
            pipeline.enqueue(full_response, source="chat_assistant", session_id=session_id, message_id=assistant_message_id)
            if assistant_message_id is not None and len(full_response) > 50 else False
        )
        diagnostics.add_stage(
            trace_id,
            "memory_enqueue",
            label="记忆写入排队",
            duration_ms=timer.ms,
            details={"user": user_enqueued, "assistant": assistant_enqueued, "queue": pipeline.status().get("queue_size")},
        )

        if isinstance(integrity_report, dict):
            diagnostics.add_stage(
                trace_id,
                "brain_integrity",
                label="原生脑完整性",
                details={
                    "adapter": integrity_report.get("adapter"),
                    "api_family": integrity_report.get("api_family"),
                    "actual_model": integrity_report.get("actual_model"),
                    "sent_options": integrity_report.get("sent_options"),
                    "omitted_options": integrity_report.get("omitted_options"),
                    "stop_reason": integrity_report.get("stop_reason"),
                    "warnings": integrity_report.get("warnings"),
                },
            )
        diagnostics.finish_trace(trace_id, status="completed", usage=usage_info)
        yield f"data: {json.dumps({'type': 'usage', 'usage': usage_info, 'request_id': trace_id})}\n\n"
        if isinstance(integrity_report, dict):
            yield f"data: {json.dumps({'type': 'brain_integrity', 'report': integrity_report, 'request_id': trace_id})}\n\n"
        yield f"data: {json.dumps({'type': 'trace', 'trace': diagnostics.get_trace(trace_id)})}\n\n"
        request_state = chat_request_store.get(client_request_id) or {}
        yield f"data: {json.dumps({'type': 'request_state', **request_state}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        resilient_request_events(generate(), client_request_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Request-ID": trace_id,
            "X-Client-Request-ID": client_request_id,
        },
    )


@app.post("/api/chat/non-stream")
async def chat_non_stream(request: Request):
    """非流式聊天（给内部调用用）"""
    body = await _json_object(request)
    message = _body_str(body, "message")
    session_id = _validated_session_id(body.get("session_id"), default="internal")
    if len(message) > _MAX_CHAT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"单条消息不能超过 {_MAX_CHAT_CHARS:,} 个字符；大型记录请使用文件或历史导入功能",
        )
    provider = _body_str(body, "provider", default=config.ACTIVE_PROVIDER, max_chars=64)
    if provider not in PROVIDERS:
        return JSONResponse({"error": f"未知的模型通道：{provider}"}, status_code=400)
    model = config.cache_stable_model(
        provider, _body_str(body, "model", default=get_active_model(provider), max_chars=240)
    )
    options = _body_dict(body, "options")

    # This legacy endpoint does not commit a visible user/assistant pair.  Treat
    # it as pure inference: hidden internal calls must not create phantom emotion,
    # body events, memories or P-mode transcript turns.
    user_state_changes: list[dict] = []
    user_inner_os = None
    try:
        inner_state_snapshot = inner_state.state_view(
            advance_living=False, session_id=session_id
        )
    except Exception:
        inner_state_snapshot = None
    compacted = await asyncio.to_thread(
        context_compactor.prepare_context, session_id, message
    )
    recalled = await pipeline.safe_recall_context(session_id, message)
    nonstream_stable_context = style_profiles.prompt_context()
    blocks = [
        ContextBlock(
            "conversation_chapters", str(compacted.get("context") or ""),
            priority=76, order=35,
            max_chars=config.CONTEXT_COMPRESSION_CONFIG.get("context_max_chars", 7600),
        ),
        ContextBlock("recalled_memory", recalled, priority=58, order=40,
                     max_chars=config.CONTEXT_CONFIG.get("memory_max_chars", 5600)),
        ContextBlock("artifact_persona", persona_cards.prompt_context(message),
                     priority=94, order=7,
                     max_chars=config.PERSONA_CARD_CONFIG.get("max_chars", 1800),
                     required=True),
        ContextBlock("inner_state_runtime", inner_state.prompt_context(
                         session_id=session_id, snapshot=inner_state_snapshot
                     ), priority=88,
                     order=10, max_chars=3000, required=True),
        ContextBlock("relationship_continuity", relationship_continuity.prompt_context(message, session_id=session_id),
                     priority=92, order=15, max_chars=7600, required=True),
        ContextBlock("dwell_whispers", dwell_life.whisper_context(session_id),
                     priority=90, order=18, max_chars=4200),
        ContextBlock("expression_integrity", character_integrity.prompt_context(session_id, provider),
                     priority=96, order=90, max_chars=1800, required=True),
    ]
    memory_context = context_composer.compose(blocks, key=f"nonstream:{session_id}")
    history_result = get_session_context_messages(
        session_id,
        limit=max(1, context_compactor.raw_message_limit() - 1),
        max_chars=context_compactor.raw_message_char_limit(),
        single_message_max_chars=context_compactor.raw_single_message_char_limit(),
        max_tokens=context_compactor.raw_message_token_limit(),
        single_message_max_tokens=context_compactor.raw_single_message_token_limit(),
    )
    history_result = select_recent_context_messages(
        [
            *history_result["items"],
            {"role": "user", "content": message, "metadata": {}},
        ][-context_compactor.raw_message_limit():],
        max_chars=context_compactor.raw_message_char_limit(),
        single_message_max_chars=context_compactor.raw_single_message_char_limit(),
        max_tokens=context_compactor.raw_message_token_limit(),
        single_message_max_tokens=context_compactor.raw_single_message_token_limit(),
    )
    history = native_continuity.attach_to_history(
        history_result["items"], provider, model
    )
    messages = []
    for item in history:
        if item["role"] not in ("user", "assistant"):
            continue
        built = {"role": item["role"], "content": item["content"]}
        if item.get("native_envelope") and item["role"] == "assistant":
            built["native_envelope"] = item["native_envelope"]
        messages.append(built)
    nonstream_rhythm = co_presence.sanitize_rhythm(body.get("expression_rhythm"))
    nonstream_rhythm_prompt = co_presence.submitted_prompt(nonstream_rhythm)
    try:
        nonstream_gate = persona_context_gate.evaluate(
            session_id,
            message,
            None,
            current_message_saved=False,
        )
        nonstream_persona_prompt = nonstream_gate.prompt
    except Exception:
        nonstream_persona_prompt = ""
    nonstream_system_prompt = "\n\n".join(
        part for part in (nonstream_rhythm_prompt, nonstream_persona_prompt) if part
    )

    result = await gateway.chat(
        messages=messages,
        provider=provider,
        model=model,
        stream=False,
        system_prompt=nonstream_system_prompt or None,
        stable_context=nonstream_stable_context or None,
        memory_context=memory_context,
        options=options,
        session_id=session_id,
        # This endpoint does not commit a visible assistant message.  It must not
        # mutate a persistent Claude Code transcript that SQLite never records.
        purpose="internal_chat",
    )
    protected = await relational_honesty_guard.protect_text(
        result.get("content", ""),
        user_text=message,
        provider=provider,
        model=model,
        gateway_client=gateway,
        options=options,
        initial_usage=result.get("usage"),
        initial_native_envelope=result.get("native_envelope"),
        session_id=session_id,
    )
    result["content"] = protected.text
    result["usage"] = protected.usage
    result["native_envelope"] = protected.native_envelope
    result["relational_honesty"] = protected.public()
    result["inner_os"] = None
    result["state_changes"] = []
    result["intimacy_vitals"] = inner_state.intimacy_vitals_view(
        session_id=session_id, advance=False
    )
    if isinstance(result.get("inner_os"), dict) and not relational_honesty_guard.visible_fragment_allowed(
        result["inner_os"].get("content", ""),
        user_text=message,
        action="inner_os_suppressed",
        session_id=session_id,
    ):
        result["inner_os"] = None
    return JSONResponse(result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v5.7 附件与表情包
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/attachments/upload")
async def upload_attachment(file: UploadFile = File(...)):
    try:
        item = await attachment_service.save_upload(file)
        try:
            if not file_workspace.register(item):
                raise RuntimeError("资料索引登记失败")
        except Exception:
            attachment_service.delete(item["id"])
            raise
        if item.get("kind") == "audio":
            try:
                item = ocean_listen.register(item["id"]) or item
            except Exception as exc:
                diagnostics.record_error("ocean_listen_queue", exc)
                attachment_service.update_meta(item["id"], {
                    "status": "analysis_error",
                    "parse_message": "音频已安全保存，但听海队列启动失败；请点“重新分析”。",
                    "analysis_status": "error",
                    "analysis_stage": "排队失败",
                    "analysis_error": "听海队列启动失败，请查看本机诊断记录。",
                })
                item = attachment_service.get(item["id"]) or item
        return JSONResponse(item)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        request_id = diagnostics.record_error("attachment_upload", exc)
        return JSONResponse(
            {"error": "附件上传失败，请查看诊断记录", "request_id": request_id.get("id") if isinstance(request_id, dict) else ""},
            status_code=500,
        )


@app.get("/api/attachments/{attachment_id}")
async def attachment_info(attachment_id: str):
    item = attachment_service.get(attachment_id)
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    return JSONResponse(item)


@app.get("/api/ocean-listen/status")
async def ocean_listen_status():
    return JSONResponse(ocean_listen.status())


@app.post("/api/ocean-listen/install")
async def install_ocean_listen(request: Request):
    body = await _json_object(request)
    if not _body_bool(body, "confirm", default=False):
        return JSONResponse({
            "error": "首次安装会在 Mac 下载数 GB 的独立依赖与模型；请先明确确认。",
            "code": "ocean_install_confirmation_required",
        }, status_code=400)
    try:
        return JSONResponse(ocean_listen.start_install(), status_code=202)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.get("/api/audio-analysis/{attachment_id}")
async def audio_analysis_info(attachment_id: str):
    item = attachment_service.get(attachment_id)
    if not item or item.get("kind") != "audio":
        raise HTTPException(status_code=404, detail="音频附件不存在")
    payload = dict(item)
    if item.get("analysis_status") == "error":
        payload["analysis_log_tail"] = ocean_listen.analysis_log_tail(attachment_id, 6000)
    payload["runtime"] = ocean_listen.status()
    return JSONResponse(payload)


@app.post("/api/audio-analysis/{attachment_id}/retry")
async def retry_audio_analysis(attachment_id: str):
    try:
        item = ocean_listen.retry(attachment_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(item, status_code=202)


@app.get("/api/audio-analysis/{attachment_id}/report")
async def download_audio_analysis_report(attachment_id: str):
    item = attachment_service.get(attachment_id)
    path = ocean_listen.report_path(attachment_id)
    if not item or not path:
        raise HTTPException(status_code=404, detail="完整听海报告尚未生成")
    filename = f"{Path(str(item.get('name') or 'audio')).stem}-ocean-report.json"
    return FileResponse(
        path, media_type="application/json", filename=filename,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@app.get("/api/audio-analysis/{attachment_id}/spectrogram")
async def audio_analysis_spectrogram(attachment_id: str):
    path = ocean_listen.spectrogram_path(attachment_id)
    if not path:
        raise HTTPException(status_code=404, detail="听海频谱图尚未生成")
    return FileResponse(
        path, media_type="image/png", content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/attachments/{attachment_id}/content")
async def attachment_content(attachment_id: str):
    item = attachment_service.get(attachment_id)
    path = attachment_service.file_path(attachment_id)
    if not item or not path:
        raise HTTPException(status_code=404, detail="附件不存在")
    # HTML/JS/Office 文件若以原 MIME 同源内联，会给本地 API 制造脚本执行面。
    # 图片、PDF 与已校验音频可安全同源预览；可读文本强制
    # text/plain；其余文件只下载。
    kind = str(item.get("kind") or "")
    if kind in {"image", "pdf", "audio"}:
        media_type = item.get("mime_type") or "application/octet-stream"
        disposition = "inline"
    elif kind == "text":
        media_type = "text/plain; charset=utf-8"
        disposition = "inline"
    else:
        media_type = "application/octet-stream"
        disposition = "attachment"
    return FileResponse(
        path,
        media_type=media_type,
        filename=item.get("name"),
        content_disposition_type=disposition,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
            "Cache-Control": "private, no-store",
            **({"Accept-Ranges": "bytes"} if kind == "audio" else {}),
        },
    )


@app.delete("/api/attachments/{attachment_id}")
async def delete_attachment(attachment_id: str):
    item = attachment_service.get(attachment_id)
    if item and item.get("kind") == "audio":
        ocean_listen.cancel_and_delete(attachment_id)
    if not attachment_service.delete(attachment_id):
        raise HTTPException(status_code=404, detail="附件不存在")
    file_workspace.delete_record(attachment_id)
    return JSONResponse({"ok": True})


@app.get("/api/file-workspace")
async def list_file_workspace(session_id: str = "", query: str = "", limit: int = 100):
    items = file_workspace.list(session_id=session_id, query=query, limit=limit)
    enriched = []
    for item in items:
        meta = attachment_service.get(item["id"])
        if not meta:
            continue
        merged = dict(item)
        merged.update({
            "parse_message": meta.get("parse_message", ""),
            "extracted_chars": meta.get("extracted_chars", 0),
            "truncated": meta.get("truncated", False),
            "page_count": meta.get("page_count"),
            "analysis_engine": meta.get("analysis_engine"),
            "analysis_status": meta.get("analysis_status"),
            "analysis_stage": meta.get("analysis_stage"),
            "analysis_progress": meta.get("analysis_progress"),
            "analysis_error": meta.get("analysis_error"),
            "report_url": meta.get("report_url"),
            "spectrogram_url": meta.get("spectrogram_url"),
            "preview_url": meta.get("preview_url"),
            "mime_type": meta.get("mime_type"),
        })
        enriched.append(merged)
    return JSONResponse({
        "items": enriched,
        "stats": file_workspace.stats(session_id),
        "modes": file_workspace.mode_stats(session_id),
        "session_id": session_id,
    })


@app.patch("/api/file-workspace/{attachment_id}")
async def update_file_workspace(attachment_id: str, request: Request):
    body = await _json_object(request)
    item = file_workspace.update(attachment_id, body if isinstance(body, dict) else {})
    if not item:
        raise HTTPException(status_code=404, detail="资料不存在或没有可更新字段")
    return JSONResponse(item)


@app.post("/api/file-workspace/{attachment_id}/active")
async def set_file_workspace_active(attachment_id: str, request: Request):
    body = await _json_object(request)
    session_id = _validated_session_id(body.get("session_id"))
    if not session_id:
        return JSONResponse({"error": "session_id 不能为空"}, status_code=400)
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not exists:
        create_session(session_id, config.ACTIVE_PROVIDER, get_active_model(config.ACTIVE_PROVIDER))
    active = _body_bool(body, "active", default=True)
    if not file_workspace.set_active(session_id, attachment_id, active):
        raise HTTPException(status_code=404, detail="资料不存在")
    return JSONResponse({"ok": True, "id": attachment_id, "active": active})


@app.post("/api/file-workspace/{attachment_id}/mode")
async def set_file_workspace_mode(attachment_id: str, request: Request):
    body = await _json_object(request)
    session_id = _validated_session_id(body.get("session_id"))
    mode = _body_str(body, "mode", default="off", max_chars=16).lower()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    if mode not in {"off", "retrieval", "pinned"}:
        raise HTTPException(status_code=400, detail="资料模式必须是 off、retrieval 或 pinned")
    meta = attachment_service.get(attachment_id)
    if not meta:
        raise HTTPException(status_code=404, detail="资料不存在")
    if (
        mode != "off" and meta.get("kind") == "audio"
        and meta.get("analysis_status") != "ready"
    ):
        return JSONResponse({
            "error": "这段音频还没有被听海听完，暂时不能设为按需摘取或全文常驻。",
            "code": "audio_analysis_pending",
        }, status_code=409)
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not exists:
        create_session(session_id, config.ACTIVE_PROVIDER, get_active_model(config.ACTIVE_PROVIDER))
    if not file_workspace.set_mode(session_id, attachment_id, mode):
        raise HTTPException(status_code=404, detail="资料不存在")
    return JSONResponse({
        "ok": True, "id": attachment_id, "mode": mode,
        "active": mode != "off", "modes": file_workspace.mode_stats(session_id),
    })


@app.get("/api/stickers")
async def list_stickers():
    items = sticker_service.list()
    return JSONResponse({"count": len(items), "items": items})


@app.get("/api/stickers/{sticker_id}/content")
async def sticker_content(sticker_id: str):
    item = sticker_service.get(sticker_id)
    path = sticker_service.file_path(sticker_id)
    if not item or not path:
        raise HTTPException(status_code=404, detail="表情包不存在")
    suffix = path.suffix.lower()
    media_type = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, max-age=3600",
        },
    )


@app.post("/api/stickers/import")
async def import_sticker(
    file: UploadFile = File(...),
    name: str = Form(""),
    tags: str = Form(""),
    description: str = Form(""),
):
    try:
        parsed_tags = [part.strip() for part in tags.replace("，", ",").split(",") if part.strip()]
        item = await sticker_service.import_upload(
            file,
            name=name or None,
            tags=parsed_tags,
            description=description,
        )
        return JSONResponse(item)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        request_id = diagnostics.record_error("sticker_import", exc)
        return JSONResponse(
            {"error": "表情包导入失败，请查看诊断记录", "request_id": request_id.get("id") if isinstance(request_id, dict) else ""},
            status_code=500,
        )


@app.patch("/api/stickers/{sticker_id}")
async def update_sticker(sticker_id: str, request: Request):
    body = await _json_object(request)
    item = sticker_service.update(sticker_id, body if isinstance(body, dict) else {})
    if not item:
        raise HTTPException(status_code=404, detail="表情包不存在")
    return JSONResponse(item)


@app.delete("/api/stickers/{sticker_id}")
async def delete_sticker(sticker_id: str):
    if not sticker_service.delete(sticker_id):
        raise HTTPException(status_code=404, detail="表情包不存在")
    return JSONResponse({"ok": True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v6.4 ElevenLabs 按需语音
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/voice/settings")
async def voice_settings_state():
    """只返回非秘密设置与 Key 是否存在，永不返回 Key 内容。"""
    return JSONResponse(
        voice_service.state_view(), headers={"Cache-Control": "no-store"}
    )


@app.patch("/api/voice/settings")
async def update_voice_settings(request: Request):
    body = await _json_object(request)
    return JSONResponse(
        voice_service.update_settings(body if isinstance(body, dict) else {}),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/voice/voices")
async def list_voice_options():
    try:
        voices = await voice_service.list_voices()
        return JSONResponse({"voices": voices, "items": voices, "count": len(voices)})
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        diagnostics.record_error("voice_list", exc)
        raise HTTPException(status_code=502, detail="连接 ElevenLabs 失败，请检查网络后重试") from exc


@app.post("/api/voice/synthesize")
async def synthesize_voice(request: Request):
    body = await _json_object(request)
    text = _body_str(body, "text", max_chars=int(config.VOICE_CONFIG.get("max_chars", 5000)))
    voice_id = _body_str(body, "voice_id", max_chars=128)
    try:
        audio, media_type = await voice_service.synthesize(text, voice_id=voice_id)
        return Response(
            content=audio,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": "inline; filename=daxigua-voice.mp3",
            },
        )
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        diagnostics.record_error("voice_synthesize", exc)
        raise HTTPException(status_code=502, detail="连接 ElevenLabs 失败，请检查网络后重试") from exc


@app.post("/api/voice/synthesize-timed")
async def synthesize_voice_timed(request: Request):
    body = await _json_object(request)
    text = _body_str(
        body, "text", max_chars=int(config.VOICE_CONFIG.get("max_chars", 5000))
    )
    voice_id = _body_str(body, "voice_id", max_chars=128)
    call_id = _body_str(body, "call_id", max_chars=64)
    started_ms = _body_int(
        body, "started_ms", default=0, min_value=0, max_value=7 * 24 * 60 * 60 * 1000
    )
    try:
        result = await voice_service.synthesize_with_timestamps(
            text, voice_id=voice_id
        )
        segment = None
        if call_id:
            call = voice_service.get_call(call_id)
            if not call:
                raise VoiceServiceError("通话记录不存在", 404)
            voice_service.note_call_turn(call_id, "assistant")
            segment = voice_service.record_segment(
                call_id,
                role="assistant",
                transcript=text,
                started_ms=started_ms,
                duration_ms=int(result.get("duration_ms") or 0),
                alignment=result.get("alignment") or result.get("normalized_alignment"),
                audio=result["audio"],
                media_type=result["media_type"],
            )
        return JSONResponse(
            {
                "audio_base64": base64.b64encode(result["audio"]).decode("ascii"),
                "media_type": result["media_type"],
                "alignment": result["alignment"],
                "normalized_alignment": result["normalized_alignment"],
                "duration_ms": result["duration_ms"],
                "spoken_text": result["spoken_text"],
                "voice_id": result["voice_id"],
                "segment": segment,
            },
            headers={"Cache-Control": "no-store"},
        )
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        diagnostics.record_error("voice_synthesize_timed", exc)
        raise HTTPException(
            status_code=502, detail="连接 ElevenLabs 失败，请检查网络后重试"
        ) from exc


@app.get("/api/voice/greeting")
async def cached_voice_greeting():
    path = voice_service.current_greeting()
    if not path:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename="daxigua-greeting.mp3",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/voice/greeting")
async def generate_voice_greeting():
    try:
        path = await voice_service.generate_greeting()
        return JSONResponse(
            {"ok": True, "url": "/api/voice/greeting", "bytes": path.stat().st_size},
            headers={"Cache-Control": "no-store"},
        )
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="生成本地问候失败") from exc


@app.post("/api/voice/transcribe")
async def transcribe_voice(
    file: UploadFile = File(...),
    language_code: str = Form(""),
    call_id: str = Form(""),
    keyterms: str = Form(""),
    acoustic_json: str = Form(""),
    started_ms: int = Form(0),
    duration_ms: int = Form(0),
    private_mode: str = Form(""),
    sleep_mode: str = Form(""),
):
    """Same-origin voice-message upload; the ElevenLabs key stays server-side."""
    max_bytes = max(
        1024,
        min(int(config.VOICE_CONFIG.get("stt_max_bytes", 25 * 1024 ** 2)), 256 * 1024 ** 2),
    )
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"录音不能超过 {max_bytes // 1024 // 1024} MB",
        )
    media_type = str(file.content_type or "application/octet-stream").split(";", 1)[0]
    if not (
        media_type.startswith("audio/")
        or media_type.startswith("video/")
        or media_type == "application/octet-stream"
    ):
        raise HTTPException(status_code=415, detail="只接受录音或视频中的声音")
    try:
        acoustic_raw = json.loads(acoustic_json) if acoustic_json else {}
        acoustic = voice_service.sanitize_acoustic(acoustic_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="acoustic_json 格式无效")
    parsed_keyterms: list[str] = []
    if keyterms:
        try:
            decoded_keyterms = json.loads(keyterms)
            if isinstance(decoded_keyterms, list):
                parsed_keyterms = [str(item) for item in decoded_keyterms]
            else:
                parsed_keyterms = re.split(r"[,，\n]", keyterms)
        except (TypeError, ValueError):
            parsed_keyterms = re.split(r"[,，\n]", keyterms)

    def optional_form_bool(value: str) -> bool | None:
        cleaned = str(value or "").strip().lower()
        if not cleaned:
            return None
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        if cleaned in {"0", "false", "no", "off"}:
            return False
        raise HTTPException(status_code=422, detail="通话模式布尔值无效")

    private_value = optional_form_bool(private_mode)
    sleep_value = optional_form_bool(sleep_mode)
    try:
        result, mood = await asyncio.gather(
            voice_service.transcribe(
                raw,
                filename=file.filename or "voice-message.webm",
                media_type=media_type,
                language_code=language_code or None,
                keyterms=parsed_keyterms or None,
            ),
            voice_service.analyze_mood(
                raw, media_type=media_type, acoustic=acoustic
            ),
        )
        result["acoustic"] = acoustic
        result["mood"] = mood
        voiced_frames = int(acoustic.get("frame_count") or 0)
        human_signal = (
            bool(result.get("speech_detected"))
            and (
                voiced_frames == 0
                or float(acoustic.get("voiced_ratio") or 0) >= 0.03
            )
        )
        result["human_signal"] = human_signal
        segment = None
        if call_id:
            call = voice_service.get_call(call_id)
            if not call:
                raise VoiceServiceError("通话记录不存在", 404)
            patch = {}
            if private_value is not None:
                patch["private_mode"] = private_value
            if sleep_value is not None:
                patch["sleep_mode"] = sleep_value
            if patch:
                call = voice_service.update_call(call_id, patch) or call
            if human_signal:
                voice_service.note_call_turn(call_id, "user")
                segment = voice_service.record_segment(
                    call_id,
                    role="user",
                    transcript=str(result.get("text") or ""),
                    started_ms=max(0, int(started_ms or 0)),
                    duration_ms=max(
                        0,
                        int(duration_ms or acoustic.get("duration_ms") or 0),
                    ),
                    acoustic=acoustic,
                    mood=mood,
                    alignment={"words": result.get("words") or []},
                    audio=raw,
                    media_type=media_type,
                    private_mode=bool(call.get("private_mode")),
                    sleep_mode=bool(call.get("sleep_mode")),
                )
            else:
                voice_service.log_event(
                    call_id,
                    "transcribe_no_human_signal",
                    ", ".join(result.get("audio_events") or [])[:500],
                )
        result["segment"] = segment
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        diagnostics.record_error("voice_transcribe", exc)
        raise HTTPException(status_code=502, detail="连接 ElevenLabs 转写失败，请检查网络后重试") from exc


@app.post("/api/voice/calls")
async def start_voice_call(request: Request):
    body = await _json_object(request)
    session_id = _validated_session_id(
        body.get("session_id") if isinstance(body, dict) else None,
        default="internal",
    )
    return JSONResponse(
        voice_service.start_call(
            session_id,
            private_mode=_body_bool(body, "private_mode", default=False),
            sleep_mode=_body_bool(body, "sleep_mode", default=False),
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/voice/calls/{call_id}")
async def get_voice_call(call_id: str):
    item = voice_service.get_call(call_id)
    if not item:
        raise HTTPException(status_code=404, detail="通话记录不存在")
    return JSONResponse(item, headers={"Cache-Control": "no-store"})


@app.patch("/api/voice/calls/{call_id}")
async def update_voice_call(call_id: str, request: Request):
    body = await _json_object(request)
    patch: dict[str, object] = {}
    if "private_mode" in body:
        patch["private_mode"] = _body_bool(body, "private_mode")
    if "sleep_mode" in body:
        patch["sleep_mode"] = _body_bool(body, "sleep_mode")
    if "route" in body:
        patch["route"] = _body_str(body, "route", max_chars=16)
    if "audio_clock_ms" in body:
        patch["audio_clock_ms"] = _body_int(
            body,
            "audio_clock_ms",
            min_value=0,
            max_value=7 * 24 * 60 * 60 * 1000,
        )
    item = voice_service.update_call(call_id, patch)
    if not item:
        raise HTTPException(status_code=404, detail="通话记录不存在或已经结束")
    return JSONResponse(item, headers={"Cache-Control": "no-store"})


@app.post("/api/voice/calls/{call_id}/heartbeat")
async def heartbeat_voice_call(call_id: str, request: Request):
    body = await _json_object(request, allow_empty=True)
    audio_clock_ms = _body_int(
        body,
        "audio_clock_ms",
        default=0,
        min_value=0,
        max_value=7 * 24 * 60 * 60 * 1000,
    )
    item = voice_service.heartbeat(call_id, audio_clock_ms)
    if not item:
        raise HTTPException(status_code=404, detail="通话记录不存在或已经结束")
    return JSONResponse(
        {"ok": True, "call": item}, headers={"Cache-Control": "no-store"}
    )


@app.post("/api/voice/calls/{call_id}/sleep-snapshot")
async def voice_sleep_snapshot(call_id: str, request: Request):
    body = await _json_object(request)
    sample_clock_ms = _body_int(
        body,
        "sample_clock_ms",
        min_value=0,
        max_value=7 * 24 * 60 * 60 * 1000,
    )
    try:
        item = voice_service.record_sleep_snapshot(
            call_id, sample_clock_ms, _body_dict(body, "acoustic")
        )
        return JSONResponse(item, headers={"Cache-Control": "no-store"})
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/voice/calls/{call_id}/event")
async def voice_runtime_event(call_id: str, request: Request):
    body = await _json_object(request)
    if not voice_service.get_call(call_id):
        raise HTTPException(status_code=404, detail="通话记录不存在")
    voice_service.log_event(
        call_id,
        _body_str(body, "event", max_chars=80),
        _body_str(body, "detail", max_chars=1000),
    )
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.post("/api/voice/calls/{call_id}/turn")
async def note_voice_call_turn(call_id: str, request: Request):
    body = await _json_object(request)
    role = _body_str(body, "role", default="assistant", max_chars=16).lower()
    if role not in {"user", "assistant"}:
        raise HTTPException(status_code=422, detail="role 必须是 user 或 assistant")
    if not voice_service.note_call_turn(call_id, role):
        raise HTTPException(status_code=404, detail="通话记录不存在或已经结束")
    return JSONResponse({"ok": True})


@app.delete("/api/voice/calls/{call_id}")
async def end_voice_call(call_id: str):
    item = voice_service.end_call(call_id)
    if not item:
        raise HTTPException(status_code=404, detail="通话记录不存在")
    return JSONResponse(item, headers={"Cache-Control": "no-store"})


@app.post("/api/voice/calls/{call_id}/end")
async def end_voice_call_beacon(call_id: str):
    """Page-exit fallback for iOS Safari, where DELETE may be suspended."""
    item = voice_service.end_call(call_id)
    if not item:
        raise HTTPException(status_code=404, detail="通话记录不存在")
    return JSONResponse(item, headers={"Cache-Control": "no-store"})


@app.post("/api/voice/translate")
async def translate_voice_lines(request: Request):
    body = await _json_object(request)
    lines = [str(item) for item in _body_list(body, "lines", max_items=80)]
    target = _body_str(body, "target_language", default="zh", max_chars=16)
    result = await voice_service.translate_lines(lines, target_language=target)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/voice/archive")
async def list_voice_archive(
    call_id: str = "", limit: int = 100, offset: int = 0
):
    return JSONResponse(
        voice_service.list_segments(call_id=call_id, limit=limit, offset=offset),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/voice/archive/{segment_id}/audio")
async def voice_archive_audio(segment_id: str, request: Request):
    resolved = voice_service.segment_audio(segment_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="这段录音不存在或处于私密模式")
    path, media_type = resolved
    size = path.stat().st_size
    range_value = str(request.headers.get("range") or "").strip()
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": f'inline; filename="{segment_id}.mp3"',
    }
    if not range_value:
        return FileResponse(path, media_type=media_type, headers=common_headers)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value)
    if not match:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{size}"},
        )
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{size}"},
        )
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        suffix = min(int(end_text), size)
        start, end = size - suffix, size - 1
    if start < 0 or start >= size or end < start:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    length = end - start + 1

    def iterator():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(),
        status_code=206,
        media_type=media_type,
        headers={
            **common_headers,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{size}",
        },
    )


@app.get("/api/voice/programs")
async def list_voice_programs(limit: int = 100):
    return JSONResponse(
        {"items": voice_service.list_programs(limit)},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/voice/programs/{program_id}")
async def get_voice_program(program_id: str):
    item = voice_service.get_program(program_id)
    if not item:
        raise HTTPException(status_code=404, detail="电台节目不存在")
    return JSONResponse(item, headers={"Cache-Control": "no-store"})


@app.post("/api/voice/programs")
async def create_voice_program(request: Request):
    body = await _json_object(request)
    try:
        item = voice_service.create_program(
            _body_str(body, "title", max_chars=120),
            [str(value) for value in _body_list(body, "segment_ids", max_items=500)],
            category=_body_str(body, "category", max_chars=80),
            description=_body_str(body, "description", max_chars=1000),
        )
        return JSONResponse(
            item, status_code=201, headers={"Cache-Control": "no-store"}
        )
    except VoiceServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Session 管理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/sessions")
async def list_sessions(limit: int = 200, offset: int = 0, query: str = ""):
    sessions = get_sessions(
        limit=max(1, min(int(limit), 1000)),
        offset=max(0, int(offset)),
        query=str(query or "")[:120],
    )
    return JSONResponse(sessions)


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str):
    item = get_session(session_id)
    if not item:
        raise HTTPException(status_code=404, detail="会话不存在")
    return JSONResponse(item)


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, limit: int = 1000):
    # The model still receives only the configured recent raw window, but a
    # restored export should be visibly browsable in the window.  Keep a sane
    # response cap so one giant export cannot freeze a phone browser.
    messages = get_session_messages(session_id, limit=max(1, min(int(limit), 5000)))
    return JSONResponse(messages)


@app.get("/api/sessions/{session_id}/message-page")
async def session_message_page(
    session_id: str,
    limit: int = 80,
    before_id: int | None = None,
    oldest: bool = False,
    position: int | None = None,
):
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return JSONResponse(get_session_message_page(
        session_id,
        limit=max(1, min(int(limit), 500)),
        before_id=before_id,
        oldest=bool(oldest),
        position=position,
    ))


@app.get("/api/sessions/{session_id}/new-messages")
async def session_new_messages(
    session_id: str,
    after_id: int = 0,
    limit: int = 100,
):
    """Small no-cache feed for messages persisted outside the open SSE."""
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    items = get_session_messages_after(
        session_id,
        after_id=max(0, int(after_id)),
        limit=max(1, min(int(limit), 200)),
    )
    latest_id = max(
        [max(0, int(after_id)), *(int(item.get("id") or 0) for item in items)]
    )
    with get_db() as db:
        total_count = int(db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0])
    return JSONResponse(
        {"items": items, "latest_id": latest_id, "total_count": total_count},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/messages/{message_id}/thinking")
async def message_thinking(message_id: int):
    item = thinking_vault.get(message_id)
    if not item:
        raise HTTPException(status_code=404, detail="这条回复没有保存可见思考")
    return JSONResponse({
        "message_id": item["message_id"],
        "provider": item["provider"],
        "model": item["model"],
        "label": _visible_thinking_label(item["provider"]),
        "content": item["content"],
        "char_count": item["char_count"],
        "token_estimate": item["token_estimate"],
        "created_at": item["created_at"],
        "warning": (
            "这是供应商 API 明确返回的可见推理或摘要，不等于完整的隐藏思考，"
            "也不保证正确；它不会发送给其他模型。"
        ),
    })


@app.get("/api/search/messages")
async def search_all_messages(q: str = "", limit: int = 50, offset: int = 0):
    return JSONResponse(await asyncio.to_thread(
        local_data_hub.search_messages,
        q,
        limit=max(1, min(int(limit), 100)),
        offset=max(0, int(offset)),
    ))


@app.get("/api/favorites")
async def list_message_favorites(limit: int = 100, offset: int = 0):
    return JSONResponse(await asyncio.to_thread(
        local_data_hub.list_favorites,
        limit=max(1, min(int(limit), 200)),
        offset=max(0, int(offset)),
    ))


@app.post("/api/messages/{message_id}/favorite")
async def favorite_message(message_id: int, request: Request):
    body = await _json_object(request, allow_empty=True)
    try:
        return JSONResponse(await asyncio.to_thread(
            local_data_hub.favorite,
            message_id,
            _body_str(body, "note", max_chars=500),
        ), status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/messages/{message_id}/favorite")
async def unfavorite_message(message_id: int):
    return JSONResponse({
        "ok": True,
        "message_id": int(message_id),
        "is_favorite": False,
        "removed": await asyncio.to_thread(local_data_hub.unfavorite, message_id),
    })


@app.get("/api/export/conversations/{session_id}")
async def export_one_conversation(session_id: str, format: str = "json"):
    try:
        path, filename = await asyncio.to_thread(
            local_data_hub.export_session, session_id, format
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=filename,
        media_type="application/json" if format.lower() == "json" else "text/plain",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/export/conversations")
async def export_all_conversations(format: str = "json"):
    try:
        path, filename = await asyncio.to_thread(local_data_hub.export_all, format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=filename,
        media_type="application/zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/local-data/backup")
async def download_local_backup():
    path, filename, manifest = await asyncio.to_thread(local_data_hub.create_backup)
    return FileResponse(
        path,
        filename=filename,
        media_type="application/zip",
        headers={
            "X-JTYHOME-Backup-Schema": str(manifest.get("schema") or ""),
            "X-JTYHOME-Credentials-Included": "false",
        },
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/local-data/restore/status")
async def local_restore_status():
    return JSONResponse(local_data_hub.restore_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/local-data/restore")
async def stage_local_restore(file: UploadFile = File(...)):
    filename = Path(file.filename or "jtyhome-backup.zip").name
    if Path(filename).suffix.lower() != ".zip":
        raise HTTPException(status_code=400, detail="请选择大西瓜导出的 ZIP 备份")
    handle = tempfile.NamedTemporaryFile(prefix="jtyhome-restore-upload-", suffix=".zip", delete=False)
    path = Path(handle.name)
    handle.close()
    max_bytes = 20 * 1024 ** 3
    written = 0
    try:
        with path.open("wb") as target:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail="本机备份不能超过 20 GB")
                await asyncio.to_thread(target.write, chunk)
        if not written:
            raise HTTPException(status_code=400, detail="备份文件是空的")
        try:
            result = await asyncio.to_thread(
                local_data_hub.stage_restore, path, original_name=filename
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result, status_code=202)
    finally:
        await file.close()
        path.unlink(missing_ok=True)


@app.get("/api/local-service")
async def local_service_status():
    return JSONResponse(await asyncio.to_thread(local_service.status), headers={"Cache-Control": "no-store"})


@app.post("/api/local-service/install")
async def install_local_service():
    try:
        return JSONResponse(await asyncio.to_thread(local_service.install))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.delete("/api/local-service")
async def uninstall_local_service():
    try:
        return JSONResponse(await asyncio.to_thread(local_service.uninstall))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/style-profile")
async def get_style_profile():
    return JSONResponse(await asyncio.to_thread(style_profiles.get), headers={"Cache-Control": "no-store"})


@app.post("/api/style-profile/from-session")
async def create_style_profile_from_session(request: Request):
    body = await _json_object(request)
    try:
        item = await asyncio.to_thread(
            style_profiles.create_from_session,
            _validated_session_id(body.get("session_id")),
            name=_body_str(body, "name", default="我的聊天风格", max_chars=100),
        )
        return JSONResponse(item, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/style-profile")
async def update_style_profile(request: Request):
    body = await _json_object(request)
    try:
        item = await asyncio.to_thread(
            style_profiles.update,
            name=_body_str(body, "name", max_chars=100) if "name" in body else None,
            instructions=_body_str(body, "instructions", max_chars=6000) if "instructions" in body else None,
            enabled=_body_bool(body, "enabled") if "enabled" in body else None,
        )
        return JSONResponse(item)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/style-profile/undo")
async def undo_style_profile():
    try:
        return JSONResponse(await asyncio.to_thread(style_profiles.undo))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/style-profile")
async def delete_style_profile():
    return JSONResponse({"ok": True, "deleted": await asyncio.to_thread(style_profiles.delete)})


@app.post("/api/messages/{message_id}/branch")
async def branch_from_user_message(message_id: int):
    """Preserve the old window and create an editable continuation branch."""
    try:
        item = branch_session_before_message(message_id)
        return JSONResponse(item, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/api/sessions/{session_id}/rename")
async def session_rename(session_id: str, request: Request):
    body = await _json_object(request)
    rename_session(session_id, _body_str(body, "title", max_chars=120))
    return JSONResponse({"ok": True})


@app.delete("/api/sessions/{session_id}")
async def session_delete(session_id: str):
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        from claude_code_p import claude_code_p
        await claude_code_p.remove_session(session_id)
    except Exception as exc:
        diagnostics.record_error("claude_code_p_session_cleanup", exc)
    return JSONResponse({"ok": True, "retained_long_term_memories": True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v6.2 增量上下文压缩 / 原文溯源
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/context-compression/{session_id}")
async def context_compression_status(session_id: str):
    try:
        result = await asyncio.to_thread(
            context_compactor.status, session_id, include_chapters=True
        )
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/context-compression/{session_id}")
async def context_compression_settings(session_id: str, request: Request):
    body = await _json_object(request)
    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="缺少 enabled")
    if not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
    try:
        result = await asyncio.to_thread(
            context_compactor.set_enabled, session_id, body["enabled"]
        )
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/context-compression/{session_id}/compact")
async def context_compression_run(session_id: str):
    try:
        result = await asyncio.to_thread(
            context_compactor.compact_incrementally, session_id, max_chapters=200
        )
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/context-compression/{session_id}/rebuild")
async def context_compression_rebuild(session_id: str):
    try:
        result = await asyncio.to_thread(context_compactor.rebuild, session_id)
        return JSONResponse(result)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/context-compression/{session_id}/chapters/{chapter_id}/sources")
async def context_compression_sources(session_id: str, chapter_id: int):
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    items = await asyncio.to_thread(
        context_compactor.source_messages, session_id, chapter_id
    )
    if not items:
        raise HTTPException(status_code=404, detail="没有找到这个压缩章节")
    return JSONResponse({"chapter_id": chapter_id, "messages": items})


@app.post("/api/import/conversations/preview")
async def preview_conversation_import(file: UploadFile = File(...)):
    raw = await _read_import_upload(file)
    try:
        bundle = parse_conversation_bytes(file.filename or "import.json", raw)
        return JSONResponse(preview_bundle(bundle))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/import/conversations/stage")
async def stage_large_conversation_import(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    """Save once in chunks, then scan ZIP/JSON in a background worker."""
    history_migration.ensure_schema()
    filename = Path(file.filename or "conversation-export.json").name
    suffix = Path(filename).suffix.lower()
    supported = {".zip", ".json", ".jsonl", ".txt", ".md", ".markdown"}
    if suffix not in supported:
        raise HTTPException(status_code=400, detail="请选择 ZIP、JSON、JSONL 或对话文本")
    batch_id = uuid.uuid4().hex
    source_path, staging_path = history_migration.paths_for(batch_id, suffix)
    max_bytes = int(config.IMPORT_CONFIG.get("max_upload_bytes", 1024 ** 3))
    chunk_size = max(64 * 1024, int(config.IMPORT_CONFIG.get("upload_chunk_bytes", 1024 ** 2)))
    written = 0
    try:
        free_bytes = shutil.disk_usage(history_migration.root).free
        reserve_bytes = min(512 * 1024 ** 2, max(64 * 1024 ** 2, free_bytes // 10))
        with source_path.open("xb") as target:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"历史包不能超过 {max_bytes / 1024 ** 3:.1f} GB",
                    )
                if written > max(0, free_bytes - reserve_bytes):
                    raise HTTPException(status_code=507, detail="本机磁盘空间不足，无法安全暂存这个历史包")
                await asyncio.to_thread(target.write, chunk)
        if not written:
            raise HTTPException(status_code=400, detail="导入文件是空的")
        item = history_migration.create_batch(
            batch_id=batch_id,
            filename=filename,
            source_path=source_path,
            staging_path=staging_path,
            bytes_total=written,
        )
        background_tasks.add_task(history_migration.scan, batch_id)
        return JSONResponse(item, status_code=202)
    except HTTPException:
        source_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        source_path.unlink(missing_ok=True)
        entry = diagnostics.record_error("history_import_stage", exc, metadata={"filename": filename})
        raise HTTPException(
            status_code=500,
            detail=f"历史包暂存失败，请查看诊断记录 {entry.get('id', '')}",
        ) from exc
    finally:
        await file.close()


@app.get("/api/import/conversations/batches/{batch_id}")
async def conversation_import_batch_status(batch_id: str):
    item = history_migration.get_batch(batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这个导入任务")
    return JSONResponse(item)


@app.post("/api/import/conversations/batches/{batch_id}/apply")
async def apply_large_conversation_import(
    batch_id: str,
    background_tasks: BackgroundTasks,
):
    item = history_migration.get_batch(batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这个导入任务")
    if item.get("status") != "ready":
        raise HTTPException(status_code=409, detail="历史包还没有完成扫描")
    background_tasks.add_task(history_migration.apply, batch_id)
    return JSONResponse({**item, "status": "importing"}, status_code=202)


@app.delete("/api/import/conversations/batches/{batch_id}")
async def cancel_large_conversation_import(batch_id: str):
    item = history_migration.request_cancel(batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这个导入任务")
    return JSONResponse(item)


@app.delete("/api/import/conversations/batches/{batch_id}/restored")
async def undo_large_conversation_import(batch_id: str):
    try:
        item = history_migration.undo_completed(batch_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这个导入任务")
    return JSONResponse(item)


@app.post("/api/import/conversations")
async def apply_conversation_import(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
):
    raw = await _read_import_upload(file)
    try:
        bundle = parse_conversation_bytes(file.filename or "import.json", raw)
        result = import_bundle(bundle)
        if result.get("session_ids"):
            background_tasks.add_task(
                context_compactor.precompact_sessions,
                result["session_ids"],
                max_chapters_per_pass=500,
            )
        result["preview"] = preview_bundle(bundle)
        return JSONResponse(result, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dwell 风格生活空间（日记 / 待办 / 日历 / 悄悄话 / 共读）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/dwell/summary")
async def dwell_summary(session_id: str = ""):
    return JSONResponse(dwell_life.summary(session_id=session_id), headers={"Cache-Control": "no-store"})


@app.get("/api/dwell/whispers")
async def dwell_whispers(session_id: str = ""):
    # A whisper belongs to one conversation. Unscoped legacy entries are not
    # returned to a new window and are never injected into model context.
    if session_id:
        session_id = _validated_session_id(session_id)
    return JSONResponse({"items": dwell_life.whispers(session_id=session_id)}, headers={"Cache-Control": "no-store"})


@app.post("/api/dwell/whispers")
async def dwell_whispers_add(request: Request):
    body = await _json_object(request)
    try:
        session_id = _validated_session_id(
            _body_str(body, "session_id", max_chars=120)
        )
        item = dwell_life.add_whisper(
            _body_str(body, "text", max_chars=2000),
            who="her",
            session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "item": item})


@app.delete("/api/dwell/whispers/{item_id}")
async def dwell_whispers_delete(item_id: str, session_id: str = ""):
    if not session_id:
        raise HTTPException(status_code=400, detail="删除悄悄话时缺少当前聊天窗口")
    session_id = _validated_session_id(session_id)
    if not dwell_life.delete_whisper(item_id, session_id):
        raise HTTPException(status_code=404, detail="没有找到这条悄悄话")
    return JSONResponse({"ok": True})


@app.get("/api/dwell/todos")
async def dwell_todos():
    return JSONResponse(dwell_life.todos(), headers={"Cache-Control": "no-store"})


@app.post("/api/dwell/todos")
async def dwell_todos_mutate(request: Request):
    body = await _json_object(request)
    try:
        action = _body_str(body, "action", max_chars=24)
        side = _body_str(body, "side", max_chars=16)
        result = dwell_life.mutate_todo(
            action,
            side,
            text=_body_str(body, "text", max_chars=500),
            at=_body_str(body, "at", max_chars=5),
            item_id=_body_str(body, "id", max_chars=64),
            include_change=True,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    change = result.pop("_change", None)
    if isinstance(change, dict):
        if change.get("action") == "del":
            from life_memory_bridge import archive_todo
            from pipeline import pipeline as memory_pipeline
            archive_todo(memory_pipeline, str(change.get("side") or side), str(change.get("id") or ""))
        elif isinstance(change.get("item"), dict):
            from life_memory_bridge import enqueue_todo
            from pipeline import pipeline as memory_pipeline
            enqueue_todo(memory_pipeline, str(change.get("side") or side), change["item"])
    return JSONResponse({"ok": True, **result})


@app.get("/api/dwell/calendar")
async def dwell_calendar():
    return JSONResponse({"items": dwell_life.calendar_items()}, headers={"Cache-Control": "no-store"})


@app.post("/api/dwell/calendar")
async def dwell_calendar_add(request: Request):
    body = await _json_object(request)
    try:
        item = dwell_life.add_calendar(
            _body_str(body, "date", max_chars=10),
            _body_str(body, "title", max_chars=160),
            _body_str(body, "note", max_chars=1200),
            _body_str(body, "mood", max_chars=24),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "item": item})


@app.patch("/api/dwell/calendar/{item_id}")
async def dwell_calendar_update(item_id: str, request: Request):
    body = await _json_object(request)
    try:
        item = dwell_life.update_calendar(
            item_id,
            date=_body_str(body, "date", max_chars=10) if "date" in body else None,
            title=_body_str(body, "title", max_chars=160) if "title" in body else None,
            note=_body_str(body, "note", max_chars=1200) if "note" in body else None,
            mood=_body_str(body, "mood", max_chars=24) if "mood" in body else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "item": item})


@app.delete("/api/dwell/calendar/{item_id}")
async def dwell_calendar_delete(item_id: str):
    if not dwell_life.delete_calendar(item_id):
        raise HTTPException(status_code=404, detail="没有找到这条日程")
    return JSONResponse({"ok": True})


@app.get("/api/dwell/diary")
async def dwell_diary():
    return JSONResponse({"items": dwell_life.diary_entries()}, headers={"Cache-Control": "no-store"})


@app.post("/api/dwell/diary")
async def dwell_diary_add(request: Request):
    body = await _json_object(request)
    try:
        item = dwell_life.add_diary(
            _body_str(body, "text", max_chars=8000),
            _body_str(body, "title", max_chars=100),
            who="her",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from life_memory_bridge import enqueue_diary
    from pipeline import pipeline as memory_pipeline
    enqueue_diary(memory_pipeline, item)
    return JSONResponse({"ok": True, "item": item})


@app.patch("/api/dwell/diary/{item_id}")
async def dwell_diary_update(item_id: str, request: Request):
    body = await _json_object(request)
    try:
        item = dwell_life.update_diary(
            item_id,
            text=_body_str(body, "text", max_chars=8000) if "text" in body else None,
            title=_body_str(body, "title", max_chars=100) if "title" in body else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from life_memory_bridge import enqueue_diary
    from pipeline import pipeline as memory_pipeline
    enqueue_diary(memory_pipeline, item)
    return JSONResponse({"ok": True, "item": item})


@app.delete("/api/dwell/diary/{item_id}")
async def dwell_diary_delete(item_id: str):
    if not dwell_life.delete_diary(item_id):
        raise HTTPException(status_code=404, detail="没有找到这篇日记")
    from life_memory_bridge import archive_diary
    from pipeline import pipeline as memory_pipeline
    archive_diary(memory_pipeline, item_id)
    return JSONResponse({"ok": True})


@app.get("/api/dwell/books")
async def dwell_books():
    return JSONResponse({"items": dwell_life.books()}, headers={"Cache-Control": "no-store"})


@app.post("/api/dwell/books")
async def dwell_books_mutate(request: Request):
    body = await _json_object(request)
    try:
        items = dwell_life.mutate_book(
            _body_str(body, "action", max_chars=24),
            item_id=_body_str(body, "id", max_chars=64),
            title=_body_str(body, "title", max_chars=180) if "title" in body else None,
            author=_body_str(body, "author", max_chars=120) if "author" in body else None,
            progress=body.get("progress") if "progress" in body else None,
            note=_body_str(body, "note", max_chars=2000) if "note" in body else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "items": items})




@app.get("/api/dwell/music-artwork")
async def dwell_music_artwork(url: str = ""):
    """Same-origin album artwork proxy used for palette extraction in Life UI."""
    if not str(url or "").strip():
        raise HTTPException(status_code=400, detail="缺少音乐链接")
    try:
        raw, content_type = await asyncio.to_thread(music_artwork.artwork_bytes, url)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="没有找到专辑封面") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="专辑封面读取失败") from exc
    return Response(
        content=raw,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=3600, stale-while-revalidate=86400"},
    )


@app.get("/api/dwell/music")
async def dwell_music():
    return JSONResponse({"items": dwell_life.music_cards()}, headers={"Cache-Control": "no-store"})


@app.post("/api/dwell/music")
async def dwell_music_mutate(request: Request):
    body = await _json_object(request)
    try:
        items = dwell_life.mutate_music(
            _body_str(body, "action", max_chars=24),
            item_id=_body_str(body, "id", max_chars=64),
            url=_body_str(body, "url", max_chars=2048) if "url" in body else None,
            title=_body_str(body, "title", max_chars=180) if "title" in body else None,
            artist=_body_str(body, "artist", max_chars=160) if "artist" in body else None,
            note=_body_str(body, "note", max_chars=1200) if "note" in body else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "items": items})


@app.patch("/api/dwell/music/{item_id}")
async def dwell_music_update(item_id: str, request: Request):
    body = await _json_object(request)
    try:
        items = dwell_life.mutate_music(
            "update",
            item_id=item_id,
            url=_body_str(body, "url", max_chars=2048) if "url" in body else None,
            title=_body_str(body, "title", max_chars=180) if "title" in body else None,
            artist=_body_str(body, "artist", max_chars=160) if "artist" in body else None,
            note=_body_str(body, "note", max_chars=1200) if "note" in body else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "items": items})


@app.get("/api/health")
async def health():
    """真实轻量自检：不发起模型请求，不泄露密钥。"""
    _refresh_provider_credentials()
    report = await tool_bridge.execute("get_system_health", {})
    return JSONResponse(report)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v5.6 诊断 / 运行轨迹 / 只读工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/diagnostics/summary")
async def diagnostics_summary():
    """Normal UI summary without message previews, tool arguments or raw errors."""
    _refresh_provider_credentials()
    health_report = await tool_bridge.execute("get_system_health", {})
    compact_errors = [
        {
            "id": item.get("id"), "at": item.get("at"),
            "source": item.get("source"), "type": item.get("type"),
            "request_id": item.get("request_id"),
        }
        for item in diagnostics.recent_errors(12)
    ]
    compact_traces = []
    for item in diagnostics.recent_traces(12):
        compact_traces.append({
            "id": item.get("id"),
            "status": item.get("status"),
            "started_at": item.get("started_at"),
            "duration_ms": item.get("duration_ms"),
            "provider": item.get("provider"),
            "model": item.get("model"),
            "usage": item.get("usage"),
            "stage_count": len(item.get("stages") or []),
            "tool_count": len(item.get("tools") or []),
            "has_error": bool(item.get("error")),
        })
    return JSONResponse({
        "health": health_report,
        "recent_errors": compact_errors,
        "recent_traces": compact_traces,
        "tools": [{"name": t["name"], "description": t["description"]} for t in tool_bridge.tools],
        "plugins": plugin_registry.snapshot(),
        "config_warnings": list(config.CONFIG_WARNINGS),
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/diagnostics/deep")
async def diagnostics_deep(request: Request):
    """Private-state diagnostics require the pairing token header, not only a cookie."""
    if not access_control.has_header_token(request):
        raise HTTPException(status_code=403, detail="深度诊断需要配对码请求头")
    return JSONResponse({
        "core": affect_core.state_view(),
        "archive": memory_archive.stats(),
        "character": character_integrity.state_view(""),
        "living": living_state.state_view(advance=False),
        "relationship": relationship_continuity.state_view(),
        "native_continuity": native_continuity.stats(),
        "file_workspace": file_workspace.stats(),
        "thinking_vault": thinking_vault.stats(),
        "context_compaction": context_compactor.health(),
        "artifact_persona": persona_cards.health(),
        "voice": voice_service.state_view(),
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/diagnostics/errors")
async def diagnostics_errors(limit: int = 20):
    return JSONResponse({"errors": diagnostics.recent_errors(limit)})


@app.delete("/api/diagnostics/errors")
async def diagnostics_clear_errors():
    diagnostics.clear_errors()
    return JSONResponse({"ok": True})


@app.get("/api/diagnostics/traces")
async def diagnostics_traces(limit: int = 20):
    return JSONResponse({"traces": diagnostics.recent_traces(limit)})


@app.get("/api/diagnostics/traces/{request_id}")
async def diagnostics_trace(request_id: str):
    trace = diagnostics.get_trace(request_id)
    if not trace:
        return JSONResponse({"error": "没有找到该请求轨迹"}, status_code=404)
    return JSONResponse(trace)


@app.get("/api/tools/read-only")
async def read_only_tools():
    return JSONResponse({
        "count": len(tool_bridge.tools),
        "tools": tool_bridge.tools,
        "safety": {
            "read_only": True,
            "shell": False,
            "file_write": False,
            "database_write": False,
            "env_read": False,
        },
    })


@app.post("/api/diagnostics/self-test")
async def diagnostics_self_test():
    """不花模型额度的本地测试；防止并发反复扫描。"""
    global _DIAGNOSTIC_SELF_TEST_LAST
    now = time.monotonic()
    if _DIAGNOSTIC_SELF_TEST_LOCK.locked() or now - _DIAGNOSTIC_SELF_TEST_LAST < 5.0:
        raise HTTPException(status_code=429, detail="自检刚刚运行过，请稍后再试")
    await _DIAGNOSTIC_SELF_TEST_LOCK.acquire()
    _DIAGNOSTIC_SELF_TEST_LAST = now
    try:
        results = []
        for name, args in (
            ("get_system_health", {}),
            ("inspect_memory_queue", {}),
            ("list_project_files", {"path": ".", "max_depth": 1, "limit": 20}),
        ):
            timer = StageTimer()
            try:
                result = await tool_bridge.execute(name, args)
                ok = not (isinstance(result, dict) and result.get("error"))
                results.append({"name": name, "ok": ok, "duration_ms": timer.ms, "result": result})
            except Exception as exc:
                diagnostics.record_error("self_test", exc, metadata={"tool": name})
                results.append({
                    "name": name, "ok": False, "duration_ms": timer.ms,
                    "error": "组件自检失败，详情已写入本机诊断记录",
                })
        def _native_sdk_health():
            versions = {}
            missing = []
            for package in ("openai", "anthropic"):
                try:
                    versions[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    missing.append(package)
            return {
                "health": "ok" if not missing else "error",
                "detail": {"versions": versions, "missing": missing},
            }

        def _brain_profiles_health():
            openai_profile = get_model_capabilities("openai", "gpt-5", PROVIDERS.get("openai", {}))
            claude_profile = get_model_capabilities("anthropic", "claude-sonnet-5", PROVIDERS.get("anthropic", {}))
            deepseek_profile = get_model_capabilities("deepseek", "deepseek-v4-pro", PROVIDERS.get("deepseek", {}))
            ok = (
                openai_profile.api_family == "responses"
                and openai_profile.supports_tools
                and claude_profile.api_family == "messages"
                and claude_profile.supports_tools
                and deepseek_profile.api_family == "deepseek_chat_completions"
                and deepseek_profile.supports_tools
            )
            return {
                "health": "ok" if ok else "error",
                "detail": {
                    "openai": openai_profile.public_dict(),
                    "anthropic": claude_profile.public_dict(),
                    "deepseek": deepseek_profile.public_dict(),
                },
            }

        for name, check in (
            ("source_faithful_memory", memory_archive.health),
            ("affect_core", affect_core.health),
            ("character_integrity", character_integrity.health),
            ("native_continuity", native_continuity.health),
            ("file_workspace", file_workspace.health),
            ("living_state", living_state.health),
            ("inner_state_runtime", inner_state.health),
            ("relationship_continuity", relationship_continuity.health),
            ("context_composer", context_composer.health),
            ("incremental_context", context_compactor.health),
            ("artifact_persona_cards", persona_cards.health),
            ("elevenlabs_voice", voice_service.health),
            ("hydrangea_water_scene", lambda: {
                "health": "ok" if water_scene.status()["ok"] else "error",
                "detail": water_scene.status(),
            }),
            ("plugin_registry", lambda: {"health": "ok", "detail": plugin_registry.snapshot()}),
            ("native_model_sdks", _native_sdk_health),
            ("brain_capability_registry", _brain_profiles_health),
        ):
            timer = StageTimer()
            try:
                result = check()
                ok = not (isinstance(result, dict) and result.get("health") == "error")
                results.append({"name": name, "ok": ok, "duration_ms": timer.ms, "result": result})
            except Exception as exc:
                diagnostics.record_error("self_test", exc, metadata={"component": name})
                results.append({
                    "name": name, "ok": False, "duration_ms": timer.ms,
                    "error": "组件自检失败，详情已写入本机诊断记录",
                })
        return JSONResponse({"ok": all(item["ok"] for item in results), "results": results})
    finally:
        _DIAGNOSTIC_SELF_TEST_LOCK.release()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v5.8 Core Fusion：原文记忆 / 情绪 / 亲密意图 / 插件（v5.8.1 保留）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/core/state")
async def core_state():
    return JSONResponse(affect_core.state_view())


@app.patch("/api/core/settings")
async def core_settings(request: Request):
    body = await _json_object(request)
    return JSONResponse(affect_core.update_settings(body))


@app.post("/api/core/reset")
async def core_reset(request: Request):
    body = await _json_object(request, allow_empty=True)
    return JSONResponse(affect_core.reset(
        keep_settings=_body_bool(body, "keep_settings", default=True)
    ))


@app.post("/api/core/intention/release")
async def core_release_intention():
    return JSONResponse(affect_core.release_intention())


@app.get("/api/core/events")
async def core_events(limit: int = 20):
    return JSONResponse({"events": affect_core.recent_events(limit), "intentions": affect_core.intention_history(limit)})


@app.get("/api/core/plugins")
async def core_plugins():
    return JSONResponse({"plugins": plugin_registry.snapshot()})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v5.8.2 性格脊柱 / 表达疲劳
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/character/state")
async def character_state(session_id: str = ""):
    return JSONResponse(character_integrity.state_view(session_id))


@app.patch("/api/character/settings")
async def character_settings(request: Request):
    body = await _json_object(request)
    return JSONResponse(character_integrity.update_settings(body if isinstance(body, dict) else {}))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v5.9 活体节律 / 共同时间线
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/living/state")
async def living_state_api():
    # Polling is observational; chat/tick/proactive paths advance the clock.
    return JSONResponse(living_state.state_view(advance=False))


@app.get("/api/inner-state")
async def inner_state_api(session_id: str = ""):
    safe_session_id = _validated_session_id(session_id) if session_id else ""
    # GET is observational. The old default advanced/smoothed physiology on every
    # console refresh, so the 10-second browser poll itself changed hardness.
    return JSONResponse(
        inner_state.state_view(advance_living=False, session_id=safe_session_id)
    )


@app.get("/api/intimacy/vitals")
async def intimacy_vitals_api(session_id: str = ""):
    safe_session_id = _validated_session_id(session_id) if session_id else ""
    return JSONResponse(
        inner_state.intimacy_vitals_view(
            session_id=safe_session_id, advance=False
        ),
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/inner-state/settings")
async def inner_state_settings(request: Request):
    body = await _json_object(request)
    patch = dict(body) if isinstance(body, dict) else {}
    raw_session_id = str(patch.pop("session_id", "") or "")
    safe_session_id = _validated_session_id(raw_session_id) if raw_session_id else ""
    return JSONResponse(
        inner_state.update_settings(patch, session_id=safe_session_id)
    )


@app.get("/api/inner-state/changes")
async def inner_state_changes(limit: int = 20, session_id: str = ""):
    safe_session_id = _validated_session_id(session_id) if session_id else ""
    return JSONResponse({
        "items": inner_state.recent_changes(limit, session_id=safe_session_id)
    })


@app.get("/api/inner-state/monologues")
async def inner_state_monologues(limit: int = 20, session_id: str = ""):
    return JSONResponse({
        "items": inner_state.recent_monologues(limit, session_id=session_id)
    })


@app.post("/api/inner-state/tick")
async def inner_state_tick(session_id: str = ""):
    safe_session_id = _validated_session_id(session_id) if session_id else ""
    await asyncio.to_thread(inner_state.advance, session_id=safe_session_id)
    return JSONResponse(inner_state.state_view(advance_living=False, session_id=safe_session_id))


@app.get("/api/morning/state")
async def morning_state_api():
    # Merely opening the console must not create today's physiological event
    # with an empty/default context before a canonical conversation turn sees it.
    return JSONResponse(morning_response.state_view(advance=False))


@app.patch("/api/morning/settings")
async def morning_settings(request: Request):
    body = await _json_object(request)
    try:
        result = morning_response.update_settings(body if isinstance(body, dict) else {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/morning/history")
async def morning_history(limit: int = 20):
    return JSONResponse({"items": morning_response.history(limit)})


@app.post("/api/morning/reset")
async def morning_reset(request: Request):
    body = await _json_object(request, allow_empty=True)
    return JSONResponse(
        morning_response.reset(
            keep_settings=_body_bool(body, "keep_settings", default=True)
        )
    )


@app.post("/api/morning/test-trigger")
async def morning_test_trigger(request: Request):
    """Queue a synthetic current morning event through the full delivery chain."""
    body = await _json_object(request)
    session_id = _validated_session_id(body.get("session_id"))
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="这个会话还没有保存")
    event = co_presence.queue_manual_morning_response(session_id)
    if not event:
        quiet = co_presence.quiet_state(session_id)
        detail = "这个窗口当前明确要求安静" if quiet["requested"] else "暂时无法建立晨间触发测试"
        raise HTTPException(status_code=409, detail=detail)

    async def _run_morning_test_after_due():
        await asyncio.sleep(1.2)
        await proactive.check_and_send()

    asyncio.create_task(_run_morning_test_after_due())
    return JSONResponse(
        {"ok": True, "queued": True, "event_id": int(event["id"])},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/living/settings")
async def living_settings(request: Request):
    body = await _json_object(request)
    return JSONResponse(living_state.update_settings(body if isinstance(body, dict) else {}))


@app.post("/api/living/tick")
async def living_tick():
    # ``heartbeat_decision`` owns this explicit advance.  The former sequence
    # advanced once here, once in state_view and once again in the decision.
    decision = living_state.heartbeat_decision()
    return JSONResponse({"state": living_state.state_view(advance=False),
                         "decision": decision})


@app.post("/api/living/reset")
async def living_reset(request: Request):
    body = await _json_object(request, allow_empty=True)
    return JSONResponse(living_state.reset(
        keep_settings=_body_bool(body, "keep_settings", default=True)
    ))


@app.get("/api/living/timeline")
async def living_timeline(limit: int = 30):
    return JSONResponse({"items": living_state.timeline(limit)})


@app.post("/api/living/activity")
async def living_activity(request: Request):
    body = await _json_object(request)
    return JSONResponse(living_state.set_activity(
        _body_str(body, "type", default="reflect", max_chars=40),
        _body_str(body, "label", default="安静做自己的事", max_chars=160),
        intensity=_body_float(
            body, "intensity", default=.65, min_value=0.0, max_value=1.0
        ),
    ))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v7.4 共处感知 / 有依据的同窗口自然续话
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/co-presence/state")
async def co_presence_state(session_id: str = ""):
    if session_id:
        session_id = _validated_session_id(session_id)
    return JSONResponse(
        co_presence.public_state(session_id),
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/co-presence/settings")
async def co_presence_settings(request: Request):
    body = await _json_object(request)
    try:
        result = co_presence.update_settings(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.post("/api/co-presence/event")
async def co_presence_event(request: Request):
    """Accept only content-free rhythm data; draft text has no valid field."""
    body = await _json_object(request)
    session_id = _validated_session_id(body.get("session_id"))
    # A brand-new local window is not persisted until its first real message.
    # Ignoring early rhythm pings keeps that flow quiet and does not create
    # ghost sessions.
    if not get_session(session_id):
        return JSONResponse(
            {"ok": True, "ignored": "session_not_created"},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )
    try:
        result = co_presence.observe(
            session_id,
            _body_str(body, "event", max_chars=64),
            _body_dict(body, "metrics"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {"ok": True, "state": result},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/co-presence/test-initiative")
async def co_presence_test_initiative(request: Request):
    """Queue one real same-window active-message test through the normal chain."""
    body = await _json_object(request)
    session_id = _validated_session_id(body.get("session_id"))
    if not get_session(session_id):
        raise HTTPException(status_code=404, detail="这个会话还没有保存")
    event = co_presence.queue_manual_initiative(session_id)
    if not event:
        quiet = co_presence.quiet_state(session_id)
        detail = "这个窗口当前明确要求安静" if quiet["requested"] else "暂时无法建立测试事件"
        raise HTTPException(status_code=409, detail=detail)
    # Do not keep the HTTP request open while the provider answers.  The same
    # scheduler/atomic delivery path still performs the actual generation.
    async def _run_test_after_due():
        await asyncio.sleep(1.2)
        await proactive.check_and_send()

    asyncio.create_task(_run_test_after_due())
    return JSONResponse(
        {"ok": True, "queued": True, "event_id": int(event["id"])},
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v6.0 关系连续性 / 未完之事 / 共同空间
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/home")
async def home_state():
    """One local snapshot for the system home screen; never calls a model."""
    sessions = get_sessions(8)
    return JSONResponse({
        "version": config.APP_VERSION,
        "companion": {"name": config.COMPANION_NAME},
        "relationship": relationship_continuity.state_view(),
        "living": living_state.state_view(advance=False),
        "core": affect_core.state_view(),
        "inner_state": inner_state.state_view(advance_living=False),
        "sessions": sessions,
        "summary": {
            "session_count": len(get_sessions(200)),
            "recent_session": sessions[0] if sessions else None,
            "provider": config.ACTIVE_PROVIDER,
            "model": get_active_model(config.ACTIVE_PROVIDER),
        },
    })


@app.get("/api/relationship/state")
async def relationship_state_api():
    return JSONResponse(relationship_continuity.state_view())


@app.get("/api/relationship/foundation")
async def relationship_foundation_get():
    return JSONResponse(relationship_continuity.get_foundation())


@app.put("/api/relationship/foundation")
async def relationship_foundation_put(request: Request):
    body = await _json_object(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求正文必须是对象")
    try:
        item = relationship_continuity.update_foundation(
            _body_str(body, "content", max_chars=500_000),
            enabled=_body_bool(body, "enabled", default=True),
            merge=_body_str(body, "merge", default="replace", max_chars=16),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(item)


@app.post("/api/relationship/foundation/import")
async def relationship_foundation_import(
    file: UploadFile = File(...),
    merge: str = Form("append"),
):
    raw = await _read_import_upload(file, max_bytes=20 * 1024 * 1024)
    try:
        parsed = extract_foundation_bytes(file.filename or "memory.txt", raw)
        item = relationship_continuity.update_foundation(
            parsed["content"], enabled=True,
            merge="append" if str(merge).lower() == "append" else "replace",
        )
        return JSONResponse({"ok": True, "import": parsed, "foundation": item}, status_code=201)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/relationship/settings")
async def relationship_settings(request: Request):
    body = await _json_object(request)
    return JSONResponse(relationship_continuity.update_settings(
        body if isinstance(body, dict) else {}
    ))


@app.get("/api/relationship/threads")
async def relationship_threads(status: str = "open", limit: int = 30):
    return JSONResponse({"items": relationship_continuity.list_threads(status, limit)})


@app.post("/api/relationship/threads")
async def relationship_thread_create(request: Request):
    body = await _json_object(request)
    try:
        item = relationship_continuity.create_thread(body if isinstance(body, dict) else {})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(item, status_code=201)


@app.patch("/api/relationship/threads/{thread_id}")
async def relationship_thread_update(thread_id: int, request: Request):
    body = await _json_object(request)
    try:
        item = relationship_continuity.update_thread(
            thread_id, body if isinstance(body, dict) else {}
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="没有找到这项未完之事")
    return JSONResponse(item)


@app.delete("/api/relationship/threads/{thread_id}")
async def relationship_thread_delete(thread_id: int):
    if not relationship_continuity.delete_thread(thread_id):
        raise HTTPException(status_code=404, detail="没有找到这项未完之事")
    return JSONResponse({"ok": True})


@app.get("/api/relationship/shared")
async def relationship_shared(status: str = "active", limit: int = 40):
    return JSONResponse({"items": relationship_continuity.list_shared(status, limit)})


@app.post("/api/relationship/shared")
async def relationship_shared_create(request: Request):
    body = await _json_object(request)
    try:
        item = relationship_continuity.create_shared(body if isinstance(body, dict) else {})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(item, status_code=201)


@app.patch("/api/relationship/shared/{item_id}")
async def relationship_shared_update(item_id: int, request: Request):
    body = await _json_object(request)
    try:
        item = relationship_continuity.update_shared(
            item_id, body if isinstance(body, dict) else {}
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="共同空间里没有找到它")
    return JSONResponse(item)


@app.delete("/api/relationship/shared/{item_id}")
async def relationship_shared_delete(item_id: int):
    if not relationship_continuity.delete_shared(item_id):
        raise HTTPException(status_code=404, detail="共同空间里没有找到它")
    return JSONResponse({"ok": True})


@app.get("/api/memory/archive")
async def memory_archive_search(query: str = "", limit: int = 10):
    return JSONResponse({"query": query, "items": memory_archive.search(query, limit=limit), "stats": memory_archive.stats()})


@app.get("/api/memory/life-bridge-status")
async def memory_life_bridge_status():
    return JSONResponse(life_memory_bridge.status())


@app.get("/api/memory/source/{message_id}")
async def memory_archive_source(message_id: int):
    item = memory_archive.get_source(message_id)
    if not item:
        return JSONResponse({"error": "没有找到这条原文"}, status_code=404)
    return JSONResponse(item)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Console 控制台 API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/console/stats")
async def console_stats():
    """全局统计"""
    stats = get_token_stats()
    return JSONResponse(stats)


@app.get("/api/console/stats/{session_id}")
async def console_session_stats(session_id: str):
    """Session 内每条消息的详细费用"""
    stats = get_detailed_stats(session_id)
    return JSONResponse(stats)


@app.get("/api/console/usage")
async def console_api_usage(period: str = "7d"):
    """可见、无提示词内容的 API 用量与费用统计。

    费用优先使用上游返回的实际扣费；第一方接口没有返回费用时使用
    调用当时的本机价格快照，并明确标为估算。旧版曾经写入的 OR 零值
    不再冒充“免费”，而是显示为费用未知。
    """
    # Opportunistic cache-warm compensation: opening the usage page also
    # repairs a lost timer, but the manager still enforces TTL/fuses.
    await cache_keepalive.watchdog()
    co_presence.ensure_schema()
    ranges = {
        "today": "ts.created_at >= datetime('now','localtime','start of day','utc')",
        "7d": "ts.created_at >= datetime('now','-7 days')",
        "30d": "ts.created_at >= datetime('now','-30 days')",
        "all": "1=1",
    }
    period = period if period in ranges else "7d"
    request_filter = """(
        COALESCE(ts.input_tokens, 0) + COALESCE(ts.output_tokens, 0) +
        COALESCE(ts.reasoning_tokens, 0) + COALESCE(ts.cache_read, 0) +
        COALESCE(ts.cache_creation, 0)
    ) > 0"""
    # Cost/token totals cover every configured model.  The cache cards are
    # explicitly Claude metrics, so never dilute their denominator with
    # DeepSeek or another auxiliary provider's ordinary input tokens.
    claude_cache_filter = """LOWER(COALESCE(ts.provider, '')) IN (
        'anthropic', 'openrouter_claude', 'claude_code_p'
    )"""
    deepseek_cache_filter = """LOWER(COALESCE(ts.provider, '')) = 'deepseek'"""
    source_expr = """CASE
        WHEN COALESCE(ts.cost_source, '') <> '' THEN ts.cost_source
        WHEN COALESCE(ts.cost, 0) > 0 THEN 'legacy_recorded'
        ELSE 'unavailable'
    END"""
    # Anthropic Messages splits prompt tokens into uncached/read/write buckets;
    # OpenAI-style APIs usually report cached tokens inside the input total.
    # Normalize both shapes so the displayed token hit rate is comparable.
    prompt_total_expr = """CASE
        WHEN LOWER(COALESCE(ts.provider, '')) IN ('anthropic', 'openrouter_claude', 'claude_code_p')
        THEN COALESCE(ts.input_tokens, 0) + COALESCE(ts.cache_read, 0) + COALESCE(ts.cache_creation, 0)
        ELSE MAX(
            COALESCE(ts.input_tokens, 0),
            COALESCE(ts.cache_read, 0) + COALESCE(ts.cache_creation, 0)
        )
    END"""

    def finish_cache_metrics(item: dict) -> dict:
        prompt_total = max(0, int(item.get("prompt_input_tokens") or 0))
        cache_read = max(0, int(item.get("cache_read") or 0))
        cache_creation = max(0, int(item.get("cache_creation") or 0))
        # Anthropic reports uncached/read/write as separate buckets, whereas
        # OpenAI-style usage usually includes cached tokens inside input_tokens.
        # ``prompt_total`` was normalized in SQL, so deriving the fresh bucket
        # from it avoids either dropping or double-counting cache reads.
        uncached_input = max(0, prompt_total - cache_read - cache_creation)
        requests = max(0, int(item.get("requests") or 0))
        cache_hits = max(0, int(item.get("cache_hits") or 0))
        # Match Claude Console's Cache read ratio exactly: fresh cache writes
        # are excluded from this denominator.  Keep the stricter all-input
        # reuse metric separately so a 99.x% read ratio can never hide costly
        # write amplification.
        item.update(calculate_claude_cache_metrics(
            uncached_input=uncached_input,
            cache_read=cache_read,
            cache_creation=cache_creation,
            prompt_total=prompt_total,
        ))
        item["cache_token_hit_rate"] = item["cache_read_ratio"]
        item["cache_request_hit_rate"] = round(
            cache_hits / requests * 100, 2
        ) if requests else 0.0
        item["cache_write_amortization"] = item["cache_read_write_ratio"]
        # Backwards-compatible field now follows Claude's provider dashboard.
        item["cache_hit_rate"] = item["cache_token_hit_rate"]
        return item

    def summary_row(db, where_sql: str) -> dict:
        row = db.execute(f"""
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(ts.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(ts.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(ts.reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(ts.cache_read), 0) AS cache_read,
                COALESCE(SUM(ts.cache_creation), 0) AS cache_creation,
                COALESCE(SUM({prompt_total_expr}), 0) AS prompt_input_tokens,
                COALESCE(SUM(ts.cost), 0) AS cost,
                SUM(CASE WHEN ts.cache_read > 0 THEN 1 ELSE 0 END) AS cache_hits,
                SUM(CASE WHEN {source_expr} = 'upstream_exact' THEN 1 ELSE 0 END) AS exact_requests,
                SUM(CASE WHEN {source_expr} = 'local_estimate' THEN 1 ELSE 0 END) AS estimated_requests,
                SUM(CASE WHEN {source_expr} = 'legacy_recorded' THEN 1 ELSE 0 END) AS legacy_requests,
                SUM(CASE WHEN {source_expr} = 'mixed' THEN 1 ELSE 0 END) AS mixed_requests,
                SUM(CASE WHEN {source_expr} = 'unavailable' THEN 1 ELSE 0 END) AS unpriced_requests
            FROM token_stats ts
            WHERE {request_filter} AND ({where_sql})
        """).fetchone()
        item = dict(row or {})
        for key in (
            "requests", "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read", "cache_creation", "prompt_input_tokens", "cache_hits", "exact_requests",
            "estimated_requests", "legacy_requests", "mixed_requests",
            "unpriced_requests",
        ):
            item[key] = int(item.get(key) or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)
        item["total_tokens"] = item["prompt_input_tokens"] + item["output_tokens"]
        finish_cache_metrics(item)

        saved_rows = db.execute(f"""
            SELECT ts.cache_read, ts.price_snapshot
            FROM token_stats ts
            WHERE {request_filter} AND ({where_sql}) AND ts.cache_read > 0
        """).fetchall()
        saved = 0.0
        for saved_row in saved_rows:
            try:
                prices = json.loads(saved_row["price_snapshot"] or "{}")
                discount = max(
                    0.0,
                    float(prices.get("input", 0) or 0)
                    - float(prices.get("cache_read", 0) or 0),
                )
                saved += int(saved_row["cache_read"] or 0) * discount / 1_000_000
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        item["cache_saved"] = round(saved, 8)
        return item

    where_sql = ranges[period]
    call_where_sql = where_sql.replace("ts.created_at", "acs.created_at")
    proactive_where_sql = where_sql.replace("ts.created_at", "pcs.created_at")
    # These operations are not written into the per-message token ledger, so
    # add them exactly once when presenting total spend. Main chat/tool/proactive
    # and relational rewrites are already represented by token_stats.
    background_purposes = (
        "inner_reflection", "memory_chunk", "memory_tag", "memory_dehydrate",
        "memory_contradiction", "memory_rerank", "memory_merge", "voice_translation",
    )
    background_sql = ",".join("?" * len(background_purposes))

    def background_cost_row(db, call_where: str) -> dict:
        row = db.execute(f"""
            SELECT COUNT(*) AS operations,
                   COALESCE(SUM(acs.cost), 0) AS cost,
                   SUM(CASE WHEN COALESCE(acs.cost_source, '') IN ('', 'unavailable')
                            THEN 1 ELSE 0 END) AS unpriced_operations
            FROM api_call_stats acs
            WHERE ({call_where}) AND acs.purpose IN ({background_sql})
        """, background_purposes).fetchone()
        item = dict(row or {})
        item["operations"] = int(item.get("operations") or 0)
        item["unpriced_operations"] = int(item.get("unpriced_operations") or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)
        return item

    with get_db() as db:
        summary = summary_row(db, where_sql)
        all_time = summary if period == "all" else summary_row(db, ranges["all"])
        cache_summary = summary_row(
            db, f"({where_sql}) AND ({claude_cache_filter})"
        )
        cache_all_time = (
            cache_summary if period == "all"
            else summary_row(db, f"({ranges['all']}) AND ({claude_cache_filter})")
        )
        deepseek_cache_summary = summary_row(
            db, f"({where_sql}) AND ({deepseek_cache_filter})"
        )
        deepseek_cache_all_time = (
            deepseek_cache_summary if period == "all"
            else summary_row(db, f"({ranges['all']}) AND ({deepseek_cache_filter})")
        )
        model_rows = db.execute(f"""
            SELECT
                COALESCE(NULLIF(ts.provider, ''), 'unknown') AS provider,
                COALESCE(NULLIF(ts.model, ''), 'unknown') AS model,
                COUNT(*) AS requests,
                COALESCE(SUM(ts.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(ts.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(ts.reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(ts.cache_read), 0) AS cache_read,
                COALESCE(SUM(ts.cache_creation), 0) AS cache_creation,
                COALESCE(SUM({prompt_total_expr}), 0) AS prompt_input_tokens,
                SUM(CASE WHEN ts.cache_read > 0 THEN 1 ELSE 0 END) AS cache_hits,
                COALESCE(SUM(ts.cost), 0) AS cost,
                SUM(CASE WHEN {source_expr} = 'unavailable' THEN 1 ELSE 0 END) AS unpriced_requests,
                SUM(CASE WHEN {source_expr} = 'mixed' THEN 1 ELSE 0 END) AS mixed_requests
            FROM token_stats ts
            WHERE {request_filter} AND ({where_sql})
            GROUP BY provider, model
            ORDER BY cost DESC, input_tokens + output_tokens DESC
            LIMIT 30
        """).fetchall()
        recent_rows = db.execute(f"""
            SELECT ts.created_at,
                   COALESCE(NULLIF(ts.provider, ''), 'unknown') AS provider,
                   COALESCE(NULLIF(ts.model, ''), 'unknown') AS model,
                   ts.input_tokens, ts.output_tokens, ts.reasoning_tokens,
                   ts.cache_read, ts.cache_creation,
                   ({prompt_total_expr}) AS prompt_input_tokens,
                   CASE WHEN ts.cache_read > 0 THEN 1 ELSE 0 END AS cache_hits,
                   ts.cost, {source_expr} AS cost_source
            FROM token_stats ts
            WHERE {request_filter} AND ({where_sql})
            ORDER BY ts.created_at DESC, ts.id DESC
            LIMIT 20
        """).fetchall()
        daily_rows = db.execute(f"""
            SELECT date(ts.created_at, 'localtime') AS day,
                   COUNT(*) AS requests,
                   COALESCE(SUM(({prompt_total_expr}) + COALESCE(ts.output_tokens, 0)), 0) AS tokens,
                   COALESCE(SUM(ts.cost), 0) AS cost
            FROM token_stats ts
            WHERE {request_filter} AND ({where_sql})
            GROUP BY day
            ORDER BY day DESC
            LIMIT 31
        """).fetchall()
        call_summary_row = db.execute(f"""
            SELECT
                COUNT(*) AS operations,
                COALESCE(SUM(acs.upstream_requests), 0) AS upstream_requests,
                COALESCE(SUM(acs.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(acs.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(acs.reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(acs.cache_read), 0) AS cache_read,
                COALESCE(SUM(acs.cache_creation), 0) AS cache_creation,
                COALESCE(SUM(acs.cache_creation_1h), 0) AS cache_creation_1h,
                COALESCE(SUM(acs.cache_creation_5m), 0) AS cache_creation_5m,
                COALESCE(SUM(acs.cost), 0) AS cost,
                SUM(CASE WHEN COALESCE(acs.cost_source, '') IN ('', 'unavailable') THEN 1 ELSE 0 END) AS unpriced_operations
            FROM api_call_stats acs
            WHERE ({call_where_sql})
        """).fetchone()
        purpose_rows = db.execute(f"""
            SELECT
                COALESCE(NULLIF(acs.purpose, ''), 'unspecified') AS purpose,
                COALESCE(NULLIF(acs.provider, ''), 'unknown') AS provider,
                COALESCE(NULLIF(acs.model, ''), 'unknown') AS model,
                COUNT(*) AS operations,
                COALESCE(SUM(acs.upstream_requests), 0) AS upstream_requests,
                COALESCE(SUM(acs.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(acs.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(acs.reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(acs.cache_read), 0) AS cache_read,
                COALESCE(SUM(acs.cache_creation), 0) AS cache_creation,
                COALESCE(SUM(acs.cache_creation_1h), 0) AS cache_creation_1h,
                COALESCE(SUM(acs.cache_creation_5m), 0) AS cache_creation_5m,
                COALESCE(SUM(acs.cost), 0) AS cost,
                SUM(CASE WHEN COALESCE(acs.cost_source, '') IN ('', 'unavailable') THEN 1 ELSE 0 END) AS unpriced_operations
            FROM api_call_stats acs
            WHERE ({call_where_sql})
            GROUP BY purpose, provider, model
            ORDER BY cost DESC, upstream_requests DESC
            LIMIT 40
        """).fetchall()
        proactive_rows = db.execute(f"""
            SELECT
                COALESCE(NULLIF(pcs.trigger_kind, ''), 'unknown') AS trigger_kind,
                COUNT(*) AS calls,
                SUM(CASE WHEN pcs.outcome_action='wait' THEN 1 ELSE 0 END) AS waits,
                SUM(CASE WHEN pcs.outcome_action='speak' THEN 1 ELSE 0 END) AS speaks,
                SUM(CASE WHEN pcs.outcome_action='recheck' THEN 1 ELSE 0 END) AS rechecks,
                SUM(CASE WHEN pcs.outcome_action NOT IN ('wait','speak','recheck')
                         OR pcs.outcome_action='' THEN 1 ELSE 0 END) AS other,
                COALESCE(SUM(pcs.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(pcs.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(pcs.reasoning_tokens), 0) AS reasoning_tokens,
                COALESCE(SUM(pcs.cost), 0) AS cost,
                SUM(CASE WHEN COALESCE(pcs.cost_source, '') IN ('', 'unavailable')
                         THEN 1 ELSE 0 END) AS unpriced_calls
            FROM proactive_call_stats pcs
            WHERE ({proactive_where_sql})
            GROUP BY trigger_kind
            ORDER BY cost DESC, calls DESC
        """).fetchall()
        cache_diag_rows = db.execute(f"""
            SELECT acs.created_at, acs.session_id, acs.provider, acs.model, acs.purpose,
                   acs.cache_read, acs.cache_creation,
                   acs.cache_creation_1h, acs.cache_creation_5m,
                   acs.generation_id, acs.actual_provider, acs.actual_model,
                   acs.router_region, acs.router_strategy,
                   acs.cache_prefix_hash, acs.cache_parent_hash, acs.cache_shape_hash,
                   acs.cache_ttl, acs.cache_breakpoint, acs.cache_guard_status,
                   acs.cache_prefix_chars, acs.cache_prefix_tokens_estimate,
                   acs.cache_min_tokens, acs.cache_strategy,
                   acs.cache_control_count, acs.cache_tools_hash, acs.cache_segment_hashes,
                   acs.cache_fingerprint, acs.cache_last_touch_at, acs.cache_next_warm_at
            FROM api_call_stats acs
            WHERE ({call_where_sql})
              AND acs.provider = 'openrouter_claude'
              AND acs.purpose IN ('main_chat', 'tool_loop')
            ORDER BY acs.created_at DESC, acs.id DESC
            LIMIT 16
        """).fetchall()
        background_period = background_cost_row(db, call_where_sql)
        background_all = (
            background_period if period == "all"
            else background_cost_row(db, "1=1")
        )

    by_model = []
    for row in model_rows:
        item = dict(row)
        for key in (
            "requests", "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read", "cache_creation", "prompt_input_tokens", "cache_hits",
            "unpriced_requests", "mixed_requests",
        ):
            item[key] = int(item.get(key) or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)
        item["total_tokens"] = item["prompt_input_tokens"] + item["output_tokens"]
        finish_cache_metrics(item)
        by_model.append(item)

    recent = []
    for row in recent_rows:
        item = dict(row)
        for key in (
            "input_tokens", "output_tokens", "reasoning_tokens",
            "cache_read", "cache_creation", "prompt_input_tokens", "cache_hits",
        ):
            item[key] = int(item.get(key) or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)
        item["requests"] = 1
        item["total_tokens"] = item["prompt_input_tokens"] + item["output_tokens"]
        finish_cache_metrics(item)
        recent.append(item)

    daily = [dict(row) for row in reversed(daily_rows)]
    for item in daily:
        item["requests"] = int(item.get("requests") or 0)
        item["tokens"] = int(item.get("tokens") or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)

    call_audit = dict(call_summary_row or {})
    for key in (
        "operations", "upstream_requests", "input_tokens", "output_tokens",
        "reasoning_tokens", "cache_read", "cache_creation", "cache_creation_1h",
        "cache_creation_5m", "unpriced_operations",
    ):
        call_audit[key] = int(call_audit.get(key) or 0)
    call_audit["cost"] = round(float(call_audit.get("cost") or 0), 8)
    by_purpose = []
    for row in purpose_rows:
        item = dict(row)
        for key in (
            "operations", "upstream_requests", "input_tokens", "output_tokens",
            "reasoning_tokens", "cache_read", "cache_creation", "cache_creation_1h",
            "cache_creation_5m", "unpriced_operations",
        ):
            item[key] = int(item.get(key) or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)
        by_purpose.append(item)

    proactive_by_trigger = []
    for row in proactive_rows:
        item = dict(row)
        for key in (
            "calls", "waits", "speaks", "rechecks", "other",
            "input_tokens", "output_tokens", "reasoning_tokens", "unpriced_calls",
        ):
            item[key] = int(item.get(key) or 0)
        item["cost"] = round(float(item.get("cost") or 0), 8)
        item["wait_rate"] = round(
            item["waits"] / item["calls"] * 100, 2
        ) if item["calls"] else 0.0
        proactive_by_trigger.append(item)

    cache_diagnostics = enrich_cache_diagnostic_rows(
        [dict(row) for row in cache_diag_rows]
    )



    for ledger, background in ((summary, background_period), (all_time, background_all)):
        ledger["conversation_cost"] = round(float(ledger.get("cost") or 0), 8)
        ledger["background_cost"] = round(float(background.get("cost") or 0), 8)
        ledger["background_operations"] = int(background.get("operations") or 0)
        ledger["background_unpriced_operations"] = int(background.get("unpriced_operations") or 0)
        ledger["total_cost_with_background"] = round(
            ledger["conversation_cost"] + ledger["background_cost"], 8
        )

    return JSONResponse({
        "period": period,
        "summary": summary,
        "all_time": all_time,
        "cache_summary": cache_summary,
        "cache_all_time": cache_all_time,
        "deepseek_cache_summary": deepseek_cache_summary,
        "deepseek_cache_all_time": deepseek_cache_all_time,
        "cache_scope": "provider_aware",
        "by_model": by_model,
        "recent": recent,
        "daily": daily,
        "call_audit": call_audit,
        "by_purpose": by_purpose,
        "proactive_by_trigger": proactive_by_trigger,
        "cache_diagnostics": cache_diagnostics,
        "cache_keepalive": cache_keepalive.summary(),
        "auxiliary": public_auxiliary_status(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })


@app.get("/api/console/cache-rate")
async def cache_hit_rate():
    """Expose Claude's read ratio plus stricter reuse/write economics."""
    prompt_expr = """CASE
        WHEN LOWER(COALESCE(provider, '')) IN ('anthropic', 'openrouter_claude', 'claude_code_p')
        THEN COALESCE(input_tokens, 0) + COALESCE(cache_read, 0) + COALESCE(cache_creation, 0)
        ELSE MAX(COALESCE(input_tokens, 0), COALESCE(cache_read, 0) + COALESCE(cache_creation, 0))
    END"""
    with get_db() as db:
        row = db.execute(f"""
            SELECT COUNT(*) AS requests,
                   SUM(CASE WHEN cache_read > 0 THEN 1 ELSE 0 END) AS cache_hits,
                   COALESCE(SUM(cache_read), 0) AS cache_read,
                   COALESCE(SUM(cache_creation), 0) AS cache_creation,
                   COALESCE(SUM({prompt_expr}), 0) AS prompt_input_tokens
            FROM token_stats
            WHERE LOWER(COALESCE(provider, '')) IN (
                      'anthropic', 'openrouter_claude', 'claude_code_p'
                  )
              AND ({prompt_expr}) > 0
        """).fetchone()
    requests = int(row["requests"] or 0) if row else 0
    hits = int(row["cache_hits"] or 0) if row else 0
    cache_read = int(row["cache_read"] or 0) if row else 0
    cache_creation = int(row["cache_creation"] or 0) if row else 0
    prompt_total = int(row["prompt_input_tokens"] or 0) if row else 0
    uncached_input = max(0, prompt_total - cache_read - cache_creation)
    cache_metrics = calculate_claude_cache_metrics(
        uncached_input=uncached_input,
        cache_read=cache_read,
        cache_creation=cache_creation,
        prompt_total=prompt_total,
    )
    request_rate = hits / requests * 100 if requests else 0.0
    return JSONResponse({
        "scope": "claude_only",
        "total_requests": requests,
        "cache_hits": hits,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "prompt_input_tokens": prompt_total,
        "hit_rate": cache_metrics["cache_read_ratio"],
        "token_hit_rate": cache_metrics["cache_read_ratio"],
        "cache_read_ratio": cache_metrics["cache_read_ratio"],
        "total_reuse_rate": cache_metrics["cache_total_reuse_rate"],
        "request_hit_rate": round(request_rate, 2),
        "prefix_reuse_rate": cache_metrics["cache_prefix_reuse_rate"],
        "read_write_ratio": cache_metrics["cache_read_write_ratio"],
    })


@app.get("/api/console/saved")
async def total_saved():
    """精确计算节省金额（基于每条记录的历史价格快照）"""
    with get_db() as db:
        rows = db.execute(
            """SELECT cache_read, price_snapshot FROM token_stats 
            WHERE cache_read > 0"""
        ).fetchall()

    total_saved = 0.0
    for row in rows:
        cache_read = row["cache_read"]
        try:
            prices = json.loads(row["price_snapshot"])
            full_price = prices.get("input", 0)
            cache_price = prices.get("cache_read", 0)
            saved = cache_read * (full_price - cache_price) / 1_000_000
            total_saved += saved
        except (json.JSONDecodeError, KeyError):
            pass

    return JSONResponse({"total_saved_usd": round(total_saved, 4)})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  原生脑能力与完整性预检
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/brain/capabilities")
async def brain_capabilities(provider: str | None = None, model: str | None = None):
    provider = provider or config.ACTIVE_PROVIDER
    if provider not in PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=404)
    model = model or get_active_model(provider)
    return JSONResponse(get_model_capabilities(provider, model, PROVIDERS[provider]).public_dict())


@app.post("/api/brain/integrity/preview")
async def brain_integrity_preview(request: Request):
    """不调用付费 API，只预演当前设置会发送哪些原生参数。"""
    body = await _json_object(request)
    provider = _body_str(
        body, "provider", default=config.ACTIVE_PROVIDER, max_chars=64
    )
    if provider not in PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=404)
    conf = PROVIDERS[provider]
    model = _body_str(
        body, "model", default=get_active_model(provider), max_chars=240
    )
    options = dict(conf.get("default_options", {}))
    options.update(_body_dict(body, "options"))
    profile = get_model_capabilities(provider, model, conf)
    report = start_report(
        provider=provider,
        model=model,
        profile=profile,
        requested_options=options,
        system=gateway._build_system_prompt(),
        messages=[{"role": "user", "content": "完整性预检示例"}],
    )
    report["preview_only"] = True
    if conf.get("protocol") == "openai_responses":
        from providers.openai_responses import OpenAIResponsesAdapter
        if not conf.get("api_key"):
            shadow = dict(conf)
            shadow["api_key"] = "preview-only"
        else:
            shadow = conf
        adapter = OpenAIResponsesAdapter(shadow)
        try:
            adapter.build_kwargs(
                model=model,
                instructions=gateway._build_system_prompt(),
                input_items=[{"role": "user", "content": "完整性预检示例"}],
                options=options,
                profile=profile,
                report=report,
            )
        finally:
            await adapter.close()
    elif conf.get("protocol") == "anthropic":
        from providers.anthropic_messages import AnthropicMessagesAdapter
        if not conf.get("api_key"):
            shadow = dict(conf)
            shadow["api_key"] = "preview-only"
        else:
            shadow = conf
        adapter = AnthropicMessagesAdapter(shadow)
        try:
            adapter.build_kwargs(
                model=model,
                system=gateway._build_anthropic_system_with_cache(
                    gateway._build_system_prompt(), tail_context=True
                ),
                messages=[{"role": "user", "content": "完整性预检示例"}],
                options=options,
                profile=profile,
                report=report,
            )
        finally:
            await adapter.close()
    elif provider == "deepseek":
        report["adapter"] = "httpx/deepseek-chat-completions"
        preview_body = {
            "model": model,
            "messages": [{"role": "user", "content": "完整性预检示例"}],
            "stream": True,
        }
        gateway._apply_compat_options(
            preview_body,
            conf=conf,
            model=model,
            options=options,
            profile=profile,
            report=report,
            stream=True,
        )
        report["sent_options"]["stream"] = True
    else:
        report["adapter"] = "httpx/chat-completions-compatible"
        report["sent_options"] = {
            "max_tokens": int(options.get("max_output_tokens") or 8192),
            "stream": True,
        }
    return JSONResponse(report)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider 切换
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/providers")
async def list_providers():
    """列出 Provider。API Key 只返回是否已配置，不会泄露内容。"""
    _refresh_provider_credentials()
    result = {}
    for name, conf in PROVIDERS.items():
        credential = config.provider_credential_status(name)
        result[name] = {
            "display_name": conf.get("display_name", name),
            "protocol": conf["protocol"],
            "default_model": conf["default_model"],
            "selected_model": get_active_model(name),
            "supports_cache": conf["supports_cache"],
            "pricing": conf["pricing"],
            "pricing_metadata": config.PRICING_METADATA,
            "capabilities": conf.get("capabilities", {}),
            "brain_capabilities": get_model_capabilities(name, get_active_model(name), conf).public_dict(),
            "default_options": conf.get("default_options", {}),
            "api_key_configured": credential["configured"],
            "api_key_source": credential["source"],
            "api_key_source_label": credential["source_label"],
            "credential_env": credential["env_var"],
            "credential_group": conf.get("credential_group") or name,
            "env_file_present": credential["env_file_present"],
            "credential_load_error": credential["load_error"],
            "credential_load_error_detail": credential.get("load_error_detail", ""),
            "credential_validation": credential.get("validation", "unknown"),
            "credential_validation_detail": credential.get("validation_detail", ""),
            "credential_validated_at": credential.get("validated_at", ""),
            "active": name == config.ACTIVE_PROVIDER,
        }
    return JSONResponse(result)


_CREDENTIAL_UI_LABELS = {
    "OPENROUTER_API_KEY": "OpenRouter（Claude / GPT / 通用共用）",
    "ANTHROPIC_API_KEY": "Anthropic / Claude",
    "OPENAI_API_KEY": "OpenAI",
    "DEEPSEEK_API_KEY": "DeepSeek",
    "ZHIPU_API_KEY": "GLM / 智谱",
    "ELEVENLABS_API_KEY": "ElevenLabs 语音",
    "DASHSCOPE_API_KEY": "阿里云百炼 / Qwen 音频情绪",
}


@app.get("/api/credentials")
async def credential_status_api():
    """Local key-vault status. Never return credential values."""
    _refresh_provider_credentials()
    statuses = config.editable_credential_status()
    payload = {
        name: {
            "label": _CREDENTIAL_UI_LABELS.get(name, name),
            "configured": bool(status.get("configured")),
            "source_label": status.get("source_label", ""),
            "load_error": bool(status.get("load_error")),
            "load_error_detail": status.get("load_error_detail", ""),
            "validation": status.get("validation", "missing"),
            "validation_detail": status.get("validation_detail", ""),
            "validated_at": status.get("validated_at", ""),
        }
        for name, status in statuses.items()
    }
    return JSONResponse(
        {"credentials": payload},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/credentials/save")
async def save_credential_api(request: Request):
    """Save a key and report real auth validation instead of catalog fallback."""
    body = await _json_object(request)
    name = _body_str(body, "credential", max_chars=64)
    value = _body_str(body, "value", max_chars=4096)
    if name not in _CREDENTIAL_UI_LABELS:
        raise HTTPException(status_code=400, detail="不支持的 Key 类型")
    old_value = config.credential_value_for_internal_use(name)
    try:
        config.save_credential(name, value)
        refresh = _refresh_provider_credentials()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    provider_for_key = {
        "ANTHROPIC_API_KEY": "anthropic",
        "OPENAI_API_KEY": "openai",
        "DEEPSEEK_API_KEY": "deepseek",
        "ZHIPU_API_KEY": "zhipu",
        "OPENROUTER_API_KEY": "openrouter",
    }.get(name)
    if provider_for_key:
        validation = await gateway.validate_credential(provider_for_key)
    elif name == "ELEVENLABS_API_KEY":
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.elevenlabs.io/v1/user",
                    headers={"xi-api-key": config.credential_value_for_internal_use(name)},
                )
            if resp.status_code in {401, 403}:
                validation = {"status": "invalid", "detail": f"ElevenLabs 拒绝认证（HTTP {resp.status_code}）"}
            elif 200 <= resp.status_code < 300:
                validation = {"status": "valid", "detail": "ElevenLabs 认证通过"}
            else:
                validation = {"status": "unverified", "detail": f"ElevenLabs 暂不可验证（HTTP {resp.status_code}）"}
        except (httpx.HTTPError, OSError) as exc:
            validation = {"status": "unverified", "detail": f"网络暂不可用：{type(exc).__name__}"}
    elif name == "DASHSCOPE_API_KEY":
        try:
            base_url = str(config.VOICE_CONFIG.get("mood_base_url") or "").rstrip("/")
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{base_url}/models",
                    headers={
                        "Authorization": (
                            "Bearer " + config.credential_value_for_internal_use(name)
                        )
                    },
                )
            if resp.status_code in {401, 403}:
                validation = {"status": "invalid", "detail": f"百炼拒绝认证（HTTP {resp.status_code}）"}
            elif 200 <= resp.status_code < 300:
                validation = {"status": "valid", "detail": "百炼认证通过"}
            else:
                validation = {"status": "unverified", "detail": f"百炼暂不可验证（HTTP {resp.status_code}）"}
        except (httpx.HTTPError, OSError) as exc:
            validation = {"status": "unverified", "detail": f"网络暂不可用：{type(exc).__name__}"}
    else:
        validation = {"status": "unverified", "detail": "没有可用的验证接口"}

    if validation["status"] == "invalid":
        try:
            if old_value:
                config.save_credential(name, old_value)
            else:
                config.clear_credential(name)
            _refresh_provider_credentials()
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"Key 未保存：{validation['detail']}")
    config.set_credential_validation(name, validation["status"], validation["detail"])
    status = config.credential_status(name)
    return JSONResponse({
        "ok": True,
        "credential": name,
        "configured": True,
        "validation": status.get("validation"),
        "validation_detail": status.get("validation_detail"),
        "active_provider": refresh.get("active_provider"),
    })


@app.post("/api/credentials/clear")
async def clear_credential_api(request: Request):
    """Clear one key only after an explicit UI action."""
    body = await _json_object(request)
    name = _body_str(body, "credential", max_chars=64)
    if name not in _CREDENTIAL_UI_LABELS:
        raise HTTPException(status_code=400, detail="不支持的 Key 类型")
    try:
        config.clear_credential(name)
        refresh = _refresh_provider_credentials()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse({
        "ok": True,
        "credential": name,
        "configured": False,
        "active_provider": refresh.get("active_provider"),
    })


@app.get("/api/providers/{provider}/models")
async def list_provider_models(provider: str, refresh: bool = False):
    """获取某个 Provider 的模型列表；支持远端发现和手动模型 ID。"""
    _refresh_provider_credentials()
    if provider not in PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=404)
    payload = await gateway.list_models(provider, force=refresh)
    return JSONResponse(payload)


@app.post("/api/providers/switch")
async def switch_provider_api(request: Request):
    """切换 provider；可同时传入 model。"""
    _refresh_provider_credentials()
    body = await _json_object(request)
    new_provider = _body_str(body, "provider", max_chars=64)
    requested_model = _body_str(body, "model", max_chars=240)
    try:
        conf = switch_provider(new_provider)
        if requested_model:
            switch_model(new_provider, requested_model)
        return JSONResponse({
            "ok": True,
            "active": new_provider,
            "model": get_active_model(new_provider),
            "pricing": conf["pricing"],
            "pricing_metadata": config.PRICING_METADATA,
            "brain_capabilities": get_model_capabilities(new_provider, get_active_model(new_provider), conf).public_dict(),
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/models/switch")
async def switch_model_api(request: Request):
    """切换模型。模型 ID 可来自模型列表，也可手工输入。"""
    body = await _json_object(request)
    provider = _body_str(body, "provider", default=config.ACTIVE_PROVIDER, max_chars=64)
    model = _body_str(body, "model", max_chars=240)
    try:
        selected = switch_model(provider, model)
        gateway.invalidate_models_cache([provider])
        if _body_bool(body, "activate_provider", default=True):
            switch_provider(provider)
        return JSONResponse({
            "ok": True,
            "provider": provider,
            "model": selected,
            "brain_capabilities": get_model_capabilities(provider, selected, PROVIDERS[provider]).public_dict(),
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  推送
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/push/vapid-key")
async def vapid_public_key():
    return JSONResponse({"publicKey": push_service.get_vapid_public_key()})


@app.get("/api/push/status")
async def push_status():
    return JSONResponse(push_service.status(), headers={"Cache-Control": "no-store"})


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    body = await _json_object(request)
    ok = push_service.subscribe(body)
    if not ok:
        raise HTTPException(status_code=422, detail="推送订阅格式无效或 endpoint 不安全")
    return JSONResponse({"ok": True})


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request):
    body = await _json_object(request)
    push_service.unsubscribe(_body_str(body, "endpoint", max_chars=2048))
    return JSONResponse({"ok": True})


@app.post("/api/push/test")
async def push_test():
    """测试推送"""
    sent = push_service.send_notification("测试推送", "如果你看到了这条消息，推送链路正常工作 💕")
    return JSONResponse({"sent": sent})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  屏幕时间
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/screen/summary")
async def screen_summary(hours: int = 24):
    return JSONResponse(screen_tracker.get_summary(hours))


@app.get("/api/screen/{event_type}")
async def screen_event(event_type: str, app_name: str = "phone"):
    """iOS 快捷指令调用的端点（参数原名 app 会遮蔽全局 FastAPI 实例，已改名）"""
    result = screen_tracker.handle_event(event_type, app_name)
    return JSONResponse(result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  欲望系统（只读状态，gated 也能看）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/desire/state")
async def desire_state():
    """它的内心面板数据：8维条 + 念头池 + 此刻最想做的事 + 各gate现状"""
    return JSONResponse(desire_host.state_view())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Ombre Brain 记忆路由（情感坐标/主动浮现/衰减/图谱）
@app.get("/api/cache/keepalive")
async def cache_keepalive_status():
    """Cache warmer status only; contains hashes/counters, never prompt text."""
    return {"items": cache_keepalive.summary()}


#  decay 定时任务已在 lifespan 注册，这里只挂路由
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
apply_patch(app, scheduler=None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  记忆
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/memory/natural-facts")
async def natural_memory_facts(status: str = "active", limit: int = 100):
    """Inspectable source-linked facts; secrets and hidden model state are absent."""
    return JSONResponse({
        "items": natural_memory.list_facts(status=status, limit=limit),
        "health": natural_memory.health(),
    })


@app.delete("/api/memory/natural-facts/{fact_id}")
async def forget_natural_memory_fact(fact_id: int):
    if not natural_memory.forget(fact_id):
        raise HTTPException(status_code=404, detail="没有找到这条随口记忆")
    return JSONResponse({"ok": True})


@app.patch("/api/memory/natural-facts/{fact_id}")
async def update_natural_memory_fact(fact_id: int, request: Request):
    body = await _json_object(request)
    try:
        item = await asyncio.to_thread(
            natural_memory.update_fact,
            fact_id,
            display_text=_body_str(body, "display_text", max_chars=500) if "display_text" in body else None,
            value=_body_str(body, "value", max_chars=120) if "value" in body else None,
            category=_body_str(body, "category", max_chars=60) if "category" in body else None,
            confidence=_body_float(body, "confidence", min_value=0, max_value=1) if "confidence" in body else None,
            importance=_body_float(body, "importance", min_value=0, max_value=1) if "importance" in body else None,
        )
        return JSONResponse(item)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/memory/natural-facts/{fact_id}/undo")
async def undo_natural_memory_fact(fact_id: int):
    try:
        return JSONResponse(await asyncio.to_thread(natural_memory.undo_fact, fact_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/memory/search")
async def memory_search_api(request: Request):
    body = await _json_object(request)
    query = _body_str(body, "query", max_chars=4000)
    results = vector_search_v2.search(query)
    return JSONResponse(results)


@app.post("/api/memory/add")
async def memory_add(request: Request):
    body = await _json_object(request)
    # 走完整 v5 管线：手动添加的记忆同样享受切分/打标/情感/图谱
    mem_ids = await pipeline.ingest(
        content=_body_str(body, "content", max_chars=120000),
        source=_body_str(body, "source", default="manual", max_chars=80),
    )
    return JSONResponse({"ok": True, "ids": mem_ids})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  记忆整理 v5.2：合并 / 聚类 / pin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.post("/api/memory/merge/preview")
async def memory_merge_preview(request: Request):
    """多条糅一条——只预览不落库（非破坏性）"""
    from memory_optimizer import merge_memories
    body = await _json_object(request)
    ids = _body_list(body, "ids", max_items=100)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in ids):
        raise HTTPException(status_code=422, detail="ids 必须只包含整数")
    result = await merge_memories(ids, gateway)
    return JSONResponse(result)


@app.post("/api/memory/merge/commit")
async def memory_merge_commit(request: Request):
    """确认合并：新条入库 + 原条归档（不删，留安全网）"""
    from memory_optimizer import commit_merge
    body = await _json_object(request)
    try:
        source_ids = _body_list(body, "source_ids", max_items=100)
        if any(isinstance(item, bool) or not isinstance(item, int) for item in source_ids):
            raise HTTPException(status_code=422, detail="source_ids 必须只包含整数")
        new_id = await commit_merge(
            _body_str(body, "content", max_chars=500_000),
            source_ids,
            _body_bool(body, "inherit_pin", default=False),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "new_id": new_id})


@app.get("/api/memory/clusters")
async def memory_clusters(threshold: float = 0.78):
    """相似聚类（類似順）：发现哪几条讲的是一回事"""
    from memory_optimizer import similar_clusters
    return JSONResponse(similar_clusters(threshold))


@app.post("/api/memory/pin")
async def memory_pin(request: Request):
    body = await _json_object(request)
    with get_db() as db:
        memory_id = _body_int(body, "id", min_value=1)
        pinned = _body_bool(body, "pinned", default=True)
        cursor = db.execute(
            "UPDATE memories SET is_pinned = ? WHERE id = ?",
            (1 if pinned else 0, memory_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="记忆不存在")
    return JSONResponse({"ok": True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MCP 工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/mcp/tools")
async def mcp_tools():
    """列出可用 MCP 工具"""
    return JSONResponse(mcp_handler.get_tools_for_api())


@app.post("/api/mcp/execute")
async def mcp_execute(request: Request):
    body = await _json_object(request)
    result = await mcp_handler.execute(
        _body_str(body, "tool_name", max_chars=80),
        _body_dict(body, "tool_input")
    )
    return JSONResponse(json.loads(result))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  启动
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    import argparse
    import uvicorn
    from browser_launcher import launch_when_ready, local_browser_url

    parser = argparse.ArgumentParser(description="启动大西瓜本机服务")
    parser.add_argument(
        "--host", default=os.getenv("DAXIGUA_HOST", "127.0.0.1"),
        help="监听地址；局域网使用 0.0.0.0，并设置 DAXIGUA_ALLOWED_HOSTS",
    )
    parser.add_argument(
        "--port", type=int, default=config._env_int("DAXIGUA_PORT", 5175, min_value=1, max_value=65535),
    )
    default_browser = str(os.getenv("DAXIGUA_BROWSER", "chrome")).strip().lower()
    if default_browser not in {"chrome", "default", "none"}:
        default_browser = "chrome"
    parser.add_argument(
        "--browser",
        choices=("chrome", "default", "none"),
        default=default_browser,
        help="服务就绪后打开 Chrome（默认）；也可使用系统默认浏览器或不打开",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器（等同 --browser none）",
    )
    args = parser.parse_args()
    browser_preference = "none" if args.no_browser else args.browser
    if browser_preference != "none":
        browser_url = local_browser_url(args.host, args.port)
        print(f"[大西瓜] 服务就绪后将打开 {browser_url}")
        launch_when_ready(
            browser_url,
            preference=browser_preference,
            expected_version=config.APP_VERSION,
        )
    uvicorn.run(app, host=args.host, port=args.port)
