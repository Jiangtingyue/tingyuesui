"""大西瓜 v7.6 有依据的同窗口主动续话执行器。

保留 ``proactive`` 这个模块名只为兼容旧启动代码。系统不会另建一个
``proactive`` 会话，也不会按固定文案或固定钟点机械问候；它把回合余韵、
欲言又止与独立主动联系统一送回原窗口，由该窗口使用的模型决定是否开口。
所有待发内容仍需绑定当前对话或有来源个人记忆，并在使用者输入时顺延。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import config
from co_presence import co_presence
from context_composer import ContextBlock, context_composer
from gateway import gateway
from models import get_db, save_message, select_recent_context_messages
from push_service import push_service
from relational_honesty import relational_honesty_guard
from style_profiles import style_profiles
from context_budgeter import (
    PROACTIVE_DYNAMIC_CHARS, PROACTIVE_PINNED_INDEX_CHARS,
    PROACTIVE_RECENT_HISTORY_CHARS, PROACTIVE_RECENT_HISTORY_TOKENS,
)


class ProactiveSystem:
    """Let the exact model from one conversation decide whether to continue it."""

    async def check_and_send(self):
        # 忠实于 fingertips 的“未发送表达遗留”机制：心跳在本机持久化，
        # 超时只产生一个柔性线索，不读取草稿，也不直接决定要不要发言。
        co_presence.sweep_sensory_events()
        # 独立晨间身体事件先进入同一条原窗口链路；它有自己的每日持久去重，
        # 不依赖“刚聊完一轮”或普通独立联系冷却。
        morning_event = co_presence.schedule_morning_response()
        # 第三路：不依赖刚完成的聊天回合。只有本地安静时段、冷却与生活状态
        # 先筛出候选后，才会产生一次模型判断；平时只是轻量 SQLite 检查。
        if not morning_event:
            co_presence.schedule_independent_initiative()
        event = co_presence.claim_due()
        if not event:
            return None

        event_id = int(event["id"])
        # Old releases may still have queued a process-global desire intent in
        # SQLite.  It has no trustworthy session-owned canonical snapshot, so
        # retire it instead of letting an upgrade deliver stale self-talk once.
        if str(event.get("kind") or "") == "desire_intent":
            co_presence.finish(
                event_id,
                action="wait",
                outcome="旧全局欲望意图已退役；未发送到任何聊天窗口",
            )
            return None
        try:
            decision = await self._generate_continuation(event)
            # This text is only local context for the pre-send guard. Remove it
            # before any decision telemetry is persisted.
            guard_user_text = str(decision.pop("_guard_user_text", "") or "")
            call_id = int(decision.get("_proactive_call_id") or 0)
            action = str(decision.get("action") or "wait").lower()
            if action == "recheck" and int(event.get("attempts") or 0) >= 2:
                # One paid recheck maximum. A genuinely new signal refreshes
                # the pending event and resets attempts, allowing one fresh retry.
                action = "wait"
                decision["action"] = "wait"
                decision["recheck_minutes"] = 0
            if action == "recheck":
                minutes = max(
                    1, min(1440, int(decision.get("recheck_minutes") or 5))
                )
                co_presence.record_decision(
                    event_id, decision, outcome="当前模型选择再观察一会儿"
                )
                co_presence.finish(
                    event_id,
                    action="recheck",
                    recheck_minutes=minutes,
                    outcome="当前模型选择再等一会儿",
                )
                co_presence.finish_model_call(call_id, "recheck")
                self._mark_morning_dispatch(
                    event, "recheck", "当前模型选择稍后重新判断"
                )
                return None
            message = str(decision.get("message") or "").strip()
            if action != "speak" or not message:
                co_presence.record_decision(
                    event_id, decision, outcome="当前模型选择不打破此刻"
                )
                co_presence.finish(
                    event_id,
                    action="wait",
                    outcome="当前模型判断此刻不必打破安静",
                )
                co_presence.finish_model_call(call_id, "wait")
                self._mark_morning_dispatch(
                    event, "waited", "当前模型判断此刻不发送"
                )
                return None
            grounding = self._resolved_grounding(decision)
            if not grounding:
                co_presence.record_decision(
                    event_id,
                    decision,
                    outcome="主动内容缺少可核对的对话或记忆依据，已拦截",
                )
                co_presence.finish(
                    event_id,
                    action="wait",
                    outcome="没有通过上下文依据校验，未发送",
                )
                co_presence.finish_model_call(call_id, "wait")
                self._mark_morning_dispatch(
                    event, "waited", "主动内容没有通过可核对依据校验"
                )
                return None
            provider = str(event.get("provider") or "")
            model = str(event.get("model") or "")
            if not provider or not model:
                provider, model = co_presence.session_brain(str(event["session_id"]))
            protected = await relational_honesty_guard.protect_text(
                message,
                user_text=guard_user_text,
                provider=provider,
                model=model,
                gateway_client=gateway,
                initial_usage=decision.get("_usage"),
                initial_native_envelope=decision.get("_native_envelope"),
                session_id=str(event["session_id"]),
            )
            decision["_relational_honesty"] = protected.public()
            if protected.blocked:
                decision["message"] = ""
                decision["_native_envelope"] = None
                decision["_usage"] = protected.usage
                co_presence.record_decision(
                    event_id,
                    decision,
                    grounding=grounding,
                    outcome="主动内容未通过关系诚实检查，未发送",
                )
                co_presence.finish(
                    event_id,
                    action="wait",
                    outcome="关系诚实保护拦截了主动草稿",
                )
                co_presence.finish_model_call(call_id, "wait")
                self._mark_morning_dispatch(
                    event, "waited", "主动草稿未通过关系诚实检查"
                )
                return None
            message = protected.text
            decision["message"] = message
            decision["_native_envelope"] = protected.native_envelope
            decision["_usage"] = protected.usage
            co_presence.record_decision(
                event_id,
                decision,
                grounding=grounding,
                outcome="已通过对话 / 记忆依据校验",
            )
            if not co_presence.ready_to_deliver(event_id):
                co_presence.finish_model_call(call_id, "wait")
                self._mark_morning_dispatch_from_event_state(event)
                return None

            message_id = await asyncio.to_thread(
                self._save_to_same_conversation,
                event,
                message,
                decision.get("_native_envelope"),
                decision.get("_usage"),
                grounding,
            )
            if not message_id:
                co_presence.finish(event_id, action="wait", outcome="续话没有成功落库")
                co_presence.finish_model_call(call_id, "wait")
                self._mark_morning_dispatch(
                    event, "waited", "主动消息没有成功写入原窗口"
                )
                return None

            # Proactive generation is intentionally one-shot so its hidden JSON
            # decision never enters the companion's persistent native transcript.
            # The visible message is canonical in SQLite; rotate P mode so the
            # next real user turn rehydrates that exact visible history once.
            if provider == "claude_code_p":
                try:
                    await gateway.invalidate_p_session(
                        str(event["session_id"]),
                        discard_native=True,
                        reason="proactive_visible_turn_committed",
                    )
                except Exception:
                    pass

            sent = await asyncio.to_thread(
                push_service.send_notification,
                config.COMPANION_NAME,
                message[:120],
                {
                    "type": "natural_continuation",
                    "session_id": event["session_id"],
                    "message_id": message_id,
                },
                f"natural-continuation-{event['session_id']}",
            )
            with get_db() as db:
                db.execute(
                    """INSERT INTO proactive_messages(
                         content, trigger_reason, push_sent, session_id
                       ) VALUES(?, ?, ?, ?)""",
                    (
                        message,
                        "同一会话自然续话",
                        int(sent > 0),
                        event["session_id"],
                    ),
                )
            co_presence.finish(
                event_id,
                action="speak",
                message_id=message_id,
                outcome="同一模型已在原窗口自然续话",
            )
            self._mark_morning_dispatch(
                event, "delivered", "同一模型已在原窗口表达当前晨间状态"
            )
            co_presence.finish_model_call(call_id, "speak")
            if str(event.get("kind") or "") == "independent_initiative":
                try:
                    from living_state import living_state
                    cue = event.get("cue") if isinstance(event.get("cue"), dict) else {}
                    living_state.commit_decision(
                        {
                            "action": "contact",
                            "reason": str(cue.get("initiative_reason") or "独立主动开口"),
                        },
                        delivered=True,
                    )
                except Exception:
                    pass
            return message
        except Exception as exc:
            print(f"[CoPresence] 自然续话失败: {type(exc).__name__}: {exc}")
            co_presence.fail(event_id, exc)
            self._mark_morning_dispatch_from_event_state(event)
            return None

    @staticmethod
    def _morning_event_id(event: dict[str, Any]) -> int:
        if str(event.get("kind") or "") != "morning_response":
            return 0
        cue = event.get("cue") if isinstance(event.get("cue"), dict) else {}
        body = (
            cue.get("morning_response")
            if isinstance(cue.get("morning_response"), dict) else {}
        )
        return int(body.get("event_id") or 0)

    @classmethod
    def _mark_morning_dispatch(
        cls,
        event: dict[str, Any],
        state: str,
        outcome: str,
    ) -> None:
        morning_event_id = cls._morning_event_id(event)
        if not morning_event_id:
            return
        try:
            from morning_response import morning_response
            morning_response.mark_proactive_outcome(
                morning_event_id,
                state=state,
                outcome=outcome,
                co_presence_event_id=int(event.get("id") or 0),
            )
        except Exception:
            pass

    @classmethod
    def _mark_morning_dispatch_from_event_state(
        cls, event: dict[str, Any]
    ) -> None:
        morning_event_id = cls._morning_event_id(event)
        if not morning_event_id:
            return
        with get_db() as db:
            row = db.execute(
                "SELECT state, outcome FROM co_presence_events WHERE id=?",
                (int(event.get("id") or 0),),
            ).fetchone()
        if not row:
            return
        state = str(row["state"] or "queued")
        mapped = {
            "pending": "queued",
            "processing": "queued",
            "delivered": "delivered",
            "waited": "waited",
            "superseded": "superseded",
            "error": "error",
        }.get(state, "queued")
        cls._mark_morning_dispatch(
            event, mapped, str(row["outcome"] or "主动链路状态已更新")
        )

    @staticmethod
    def _compact_recent_history(session_id: str) -> list[dict[str, Any]]:
        """Read a bounded recent slice without touching the main cache anchor."""
        with get_db() as db:
            rows = db.execute(
                """SELECT id, session_id, role,
                          CASE WHEN LENGTH(content)>24000
                               THEN SUBSTR(content,1,12000) || '\n…（主动判断仅保留此条消息首尾）…\n' || SUBSTR(content,-12000)
                               ELSE content END AS content,
                          created_at, input_tokens, output_tokens, cache_read,
                          cache_creation, cost, provider, model, metadata
                   FROM messages
                   WHERE session_id=? AND role IN ('user','assistant')
                   ORDER BY id DESC LIMIT 80""",
                (session_id,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            raw_meta = item.get("metadata")
            if isinstance(raw_meta, str):
                try:
                    item["metadata"] = json.loads(raw_meta) if raw_meta else {}
                except Exception:
                    item["metadata"] = {}
            elif not isinstance(raw_meta, dict):
                item["metadata"] = {}
            items.append(item)
        selected = select_recent_context_messages(
            items,
            max_chars=PROACTIVE_RECENT_HISTORY_CHARS,
            single_message_max_chars=18000,
            max_tokens=PROACTIVE_RECENT_HISTORY_TOKENS,
            single_message_max_tokens=4500,
        )
        return selected["items"]

    async def _generate_continuation(
        self, event: dict[str, Any],
    ) -> dict[str, Any]:
        from character_integrity import character_integrity
        from attachment_service import attachment_service
        from context_compactor import context_compactor
        from file_workspace import file_workspace
        from inner_state import inner_state
        from native_continuity import native_continuity
        from persona_cards import persona_cards
        from pipeline import pipeline
        from relationship_continuity import relationship_continuity

        session_id = str(event["session_id"])
        provider = str(event.get("provider") or "")
        model = str(event.get("model") or "")
        current_provider, current_model = co_presence.session_brain(session_id)
        provider = provider or current_provider
        model = model or current_model
        if provider not in config.PROVIDERS:
            raise RuntimeError("原会话使用的模型通道已经不存在")
        if not config.provider_is_ready(provider):
            raise RuntimeError(f"原会话的 {provider} 通道当前不可用")

        raw_history = await asyncio.to_thread(
            self._compact_recent_history, session_id
        )
        if not raw_history:
            return {"action": "wait", "message": ""}
        grounding_catalog, grounding_index = await asyncio.to_thread(
            self._build_grounding_catalog, raw_history, event
        )
        latest_user = next(
            (
                str(item.get("content") or "")
                for item in reversed(raw_history)
                if item.get("role") == "user"
            ),
            "",
        )
        recalled = await pipeline.safe_recall_context(session_id, latest_user)
        try:
            compacted = await asyncio.to_thread(
                context_compactor.prepare_context,
                session_id,
                latest_user,
            )
            older_context = str(compacted.get("context") or "")
        except Exception:
            older_context = ""

        def workspace_excerpt() -> tuple[str, list[str]]:
            conf = config.FILE_CONTEXT_CONFIG
            pinned = attachment_service.resolve_many(
                file_workspace.mode_ids(
                    session_id,
                    "pinned",
                    limit=conf.get("pinned_file_limit", 3),
                ),
                limit=12,
            )
            retrieval = attachment_service.resolve_many(
                file_workspace.mode_ids(
                    session_id,
                    "retrieval",
                    limit=conf.get("retrieval_file_limit", 12),
                ),
                limit=40,
            )
            pinned_ids = {str(item.get("id") or "") for item in pinned}
            retrieval = [
                item for item in retrieval
                if str(item.get("id") or "") not in pinned_ids
            ]
            pinned_text, pinned_used_ids = attachment_service.pinned_index_context(
                pinned,
                latest_user,
                max_total_chars=PROACTIVE_PINNED_INDEX_CHARS,
            )
            remaining = max(0, PROACTIVE_PINNED_INDEX_CHARS - len(pinned_text))
            retrieved_text, retrieved_ids = (
                attachment_service.relevant_text_context(
                    retrieval,
                    latest_user,
                    max_total_chars=remaining,
                )
            )
            used_ids = [*pinned_used_ids, *retrieved_ids]
            file_workspace.note_used(session_id, used_ids)
            return "\n\n".join(
                part for part in (pinned_text, retrieved_text) if part
            ), used_ids

        try:
            workspace_context, _used_file_ids = await asyncio.to_thread(
                workspace_excerpt
            )
        except Exception:
            workspace_context = ""
        blocks = [
            ContextBlock(
                "inner_life_runtime",
                inner_state.prompt_context(proactive=True, session_id=session_id),
                priority=95,
                order=10,
                max_chars=3600,
                required=True,
            ),
            ContextBlock(
                "relationship_continuity",
                relationship_continuity.prompt_context(latest_user, session_id=session_id),
                priority=94,
                order=15,
                max_chars=7000,
                required=True,
            ),
            ContextBlock(
                "user_selected_style_profile",
                style_profiles.prompt_context(),
                priority=95,
                order=8,
                max_chars=11000,
                required=True,
            ),
            ContextBlock(
                "expression_integrity",
                character_integrity.prompt_context(session_id, provider),
                priority=98,
                order=90,
                max_chars=1800,
                required=True,
            ),
        ]
        if recalled:
            blocks.append(ContextBlock(
                "recalled_memory", recalled, priority=58, order=40, max_chars=5600
            ))
        if older_context:
            blocks.append(ContextBlock(
                "conversation_chapters",
                older_context,
                priority=76,
                order=35,
                max_chars=config.CONTEXT_COMPRESSION_CONFIG.get(
                    "context_max_chars", 7600
                ),
            ))
        if workspace_context:
            blocks.append(ContextBlock(
                "file_workspace",
                workspace_context,
                priority=72,
                order=45,
                max_chars=PROACTIVE_PINNED_INDEX_CHARS,
            ))
        persona = persona_cards.prompt_context(latest_user)
        if persona:
            blocks.append(ContextBlock(
                "artifact_persona",
                persona,
                priority=93,
                order=7,
                max_chars=1800,
                required=True,
            ))
        blocks.append(ContextBlock(
            "co_presence_event",
            co_presence.cue_prompt(event),
            priority=100,
            order=5,
            max_chars=1600,
            required=True,
        ))
        dynamic = context_composer.compose(
            blocks, key=f"co-presence:{event['id']}",
            max_chars=PROACTIVE_DYNAMIC_CHARS,
        )

        history = native_continuity.attach_to_history(
            raw_history, provider, model
        )
        messages: list[dict[str, Any]] = []
        for item in history:
            if item.get("role") not in {"user", "assistant"}:
                continue
            built: dict[str, Any] = {
                "role": item["role"],
                "content": attachment_service.history_text(
                    str(item.get("content") or ""),
                    item.get("metadata")
                    if isinstance(item.get("metadata"), dict) else {},
                ),
            }
            if item.get("native_envelope") and item["role"] == "assistant":
                built["native_envelope"] = item["native_envelope"]
            messages.append(built)
        messages.append({
            "role": "user",
            "content": (
                "<silent_turn_decision>\n"
                "这是原窗口在安静之后的一次内部开口判断，不是新的用户消息。"
                "请仍然做刚才那个你，不要扮演额外角色。\n"
                "本机确实具备把你的话送回原窗口的能力；不要声称你无法主动发消息。"
                "系统会在对方仍在输入时自动顺延，所以这里不需要因为害怕抢话而一律等待。\n"
                "先看共处事件：回合余韵可以等待；欲言又止和独立主动候选若能从当前上下文"
                "形成一个具体自然的念头，应优先真正开口，不要把 wait 当成默认安全答案。"
                "晨间身体事件若仍在有效期内，它本身就是当前可核对状态；不要因为没有新的用户消息"
                "就默认忽略它。状态足以自然影响此刻表达时应真正开口，但不要机械报数或固定问候。\n"
                "绝不能没头没尾地说梦话。若选择 speak，内容必须直接由下方至少一条"
                "可核对的真实对话、有来源记忆或本机当前状态支撑。grounding_refs 最好填写；"
                "若漏填，后端会自动绑定最相关的真实依据，不会因此把整条消息丢掉。"
                "记忆只能在此刻自然相关时使用，"
                "不能为了主动而随机翻出旧事。依据编号只用于内部校验，"
                "不要写进真正发出的 message。\n"
                "不要机械说早安晚安、吃了吗、我来关心你。"
                "不得提到输入框、正在打字、删改、清空、监听、检测、共处事件或系统。\n"
                f"<grounding_catalog>\n{grounding_catalog}\n</grounding_catalog>\n"
                "只输出一个 JSON 对象，不要 Markdown："
                '{"action":"wait","recheck_minutes":0}，或 '
                '{"action":"recheck","recheck_minutes":5}，或 '
                '{"action":"speak","message":"真正要发出的消息",'
                '"grounding_refs":["message:123"],'
                '"grounding_note":"这句话与该依据的具体关系"}。\n'
                "</silent_turn_decision>"
            ),
        })
        # v8.2: this is a paid secondary judgment, not a full conversation
        # response. Keep the original model, but cap the tiny JSON answer and
        # explicitly disable deep reasoning so it cannot inherit 32K/high.
        result = await gateway.chat(
            messages=messages,
            provider=provider,
            model=model,
            stream=False,
            memory_context=dynamic,
            options={
                "max_output_tokens": int(
                    config.CO_PRESENCE_CONFIG.get("proactive_max_output_tokens", 512)
                ),
                "thinking_visibility": "hidden",
                "thinking_mode": "off",
                "reasoning_effort": "low",
            },
            session_id=session_id,
            purpose="proactive_decision",
        )
        text = str((result or {}).get("content") or "")
        decision = self._parse_decision(text)
        usage = (result or {}).get("usage") or {}
        decision["_guard_user_text"] = latest_user
        decision["_grounding_index"] = grounding_index
        decision["_usage"] = usage
        decision["_native_envelope"] = (result or {}).get("native_envelope")
        decision["_proactive_call_id"] = co_presence.record_model_call(
            event, str(decision.get("action") or "wait"), usage
        )
        return decision

    @staticmethod
    def _build_grounding_catalog(
        raw_history: list[dict[str, Any]],
        event: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        """Offer bounded, source-linked evidence without inventing summaries."""
        from natural_memory import natural_memory

        entries: list[tuple[str, str, dict[str, Any]]] = []
        for item in raw_history[-14:]:
            message_id = int(item.get("id") or 0)
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            role = str(item.get("role") or "")
            if not message_id or role not in {"user", "assistant"} or not content:
                continue
            ref = f"message:{message_id}"
            label = "用户" if role == "user" else "你"
            entries.append((
                ref,
                f"[{ref}] 当前窗口·{label}: {content[:480]}",
                {"ref": ref, "type": "message", "id": message_id, "role": role},
            ))

        event = event if isinstance(event, dict) else {}
        cue = event.get("cue") if isinstance(event.get("cue"), dict) else {}
        morning = (
            cue.get("morning_response")
            if isinstance(cue.get("morning_response"), dict) else {}
        )
        desire = (
            cue.get("desire_intent")
            if isinstance(cue.get("desire_intent"), dict) else {}
        )
        state_ref = str(cue.get("grounding_ref") or "").strip()
        if state_ref and morning:
            levels = morning.get("levels") if isinstance(morning.get("levels"), dict) else {}
            description = re.sub(
                r"\s+", " ", str(morning.get("description") or "")
            ).strip()
            entries.append((
                state_ref,
                (
                    f"[{state_ref}] 本机当前晨间身体状态: {description[:420]} "
                    f"硬度 {int(levels.get('hardness') or 1)}/10，"
                    f"主观性欲 {int(levels.get('desire') or 1)}/10，"
                    f"身体张力 {int(levels.get('physical_tension') or 1)}/10，"
                    f"身体夺权 {int(levels.get('body_takeover') or 1)}/10"
                ),
                {
                    "ref": state_ref,
                    "type": "local_state",
                    "state": "morning_response",
                    "event_id": int(morning.get("event_id") or 0),
                    "event_date": str(morning.get("event_date") or "")[:10],
                    "occurred_at": str(morning.get("occurred_at") or "")[:80],
                    "manual_test": bool(morning.get("manual_test")),
                },
            ))
        elif state_ref and desire:
            reason = re.sub(
                r"\s+", " ", str(desire.get("reason") or "想自然地靠近一点")
            ).strip()
            entries.append((
                state_ref,
                f"[{state_ref}] 本机当前内在倾向: {reason[:420]}",
                {
                    "ref": state_ref,
                    "type": "local_state",
                    "state": "desire_intent",
                    "drive_key": str(desire.get("drive_key") or "")[:40],
                    "want_action": str(desire.get("want_action") or "")[:60],
                },
            ))

        session_id = str(next((
            item.get("session_id") for item in reversed(raw_history)
            if item.get("session_id")
        ), ""))
        try:
            facts = natural_memory.list_facts(
                status="active", limit=80, session_id=session_id
            ) if session_id else []
        except Exception:
            facts = []
        accepted = [
            fact for fact in facts
            if float(fact.get("confidence") or 0) >= 0.70
            and str(fact.get("display_text") or "").strip()
        ][:12]
        for fact in accepted:
            fact_id = int(fact.get("id") or 0)
            if not fact_id:
                continue
            ref = f"memory:{fact_id}"
            source_id = int(fact.get("source_message_id") or 0)
            display = re.sub(
                r"\s+", " ", str(fact.get("display_text") or "")
            ).strip()
            entries.append((
                ref,
                f"[{ref}] 有来源个人记忆: {display[:240]}"
                + (f"（来源消息 {source_id}）" if source_id else ""),
                {
                    "ref": ref,
                    "type": "memory",
                    "id": fact_id,
                    "source_message_id": source_id,
                },
            ))
        if not entries:
            return "（没有可用于主动发言的依据；只能 wait 或 recheck）", {}
        index = {ref: metadata for ref, _line, metadata in entries}
        return "\n".join(line for _ref, line, _metadata in entries), index

    @staticmethod
    def _validated_grounding(decision: dict[str, Any]) -> dict[str, Any] | None:
        index = decision.get("_grounding_index")
        if not isinstance(index, dict):
            return None
        raw_refs = decision.get("grounding_refs")
        if not isinstance(raw_refs, list):
            return None
        refs: list[str] = []
        for value in raw_refs:
            ref = str(value or "").strip()
            if ref in index and ref not in refs:
                refs.append(ref)
        if not refs:
            return None
        return {
            "refs": refs[:12],
            "sources": [index[ref] for ref in refs[:12]],
            "note": str(decision.get("grounding_note") or "")[:500],
            "validated": True,
        }

    @classmethod
    def _resolved_grounding(cls, decision: dict[str, Any]) -> dict[str, Any] | None:
        """Validate model refs, or safely bind to the latest real window messages.

        Grounding remains mandatory, but formatting it is no longer a brittle
        all-or-nothing burden on the model.  The fallback never invents a source:
        it only selects already indexed ``message:*`` records from this session.
        """
        validated = cls._validated_grounding(decision)
        if validated:
            return validated
        index = decision.get("_grounding_index")
        if not isinstance(index, dict) or not index:
            return None
        message_refs = [
            ref for ref, metadata in index.items()
            if str(ref).startswith("message:")
            and isinstance(metadata, dict)
        ]
        state_refs = [
            ref for ref, metadata in index.items()
            if isinstance(metadata, dict)
            and metadata.get("type") == "local_state"
        ]
        if state_refs:
            refs = state_refs[-1:] + message_refs[-1:]
        else:
            refs = message_refs[-2:] if message_refs else list(index.keys())[-1:]
        refs = [ref for ref in refs if ref in index]
        if not refs:
            return None
        return {
            "refs": refs,
            "sources": [index[ref] for ref in refs],
            "note": "模型漏填引用；后端自动绑定到当前事件最相关的真实依据",
            "validated": True,
            "automatic": True,
        }

    @staticmethod
    def _parse_decision(text: str) -> dict[str, Any]:
        """Accept only the documented JSON envelope; provider prose is never user-facing."""
        cleaned = re.sub(
            r"^\s*```(?:json)?\s*|\s*```\s*$", "", str(text or ""),
            flags=re.IGNORECASE,
        ).strip()
        candidates = [cleaned]
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match and match.group(0) != cleaned:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(value, dict):
                continue
            action = str(value.get("action") or "wait").lower()
            if action not in {"wait", "recheck", "speak"}:
                action = "wait"
            message = str(value.get("message") or "").strip()
            if action == "speak" and not message:
                action = "wait"
            try:
                recheck = max(0, min(1440, int(value.get("recheck_minutes") or 0)))
            except (TypeError, ValueError):
                recheck = 0
            return {
                "action": action,
                "message": message[:20_000] if action == "speak" else "",
                "recheck_minutes": recheck,
                "grounding_refs": [
                    str(ref)[:100] for ref in value.get("grounding_refs", [])
                    if isinstance(ref, str)
                ][:12] if isinstance(value.get("grounding_refs"), list) else [],
                "grounding_note": str(value.get("grounding_note") or "")[:500],
            }
        return {
            "action": "wait", "message": "", "recheck_minutes": 0,
            "grounding_refs": [],
            "grounding_note": "供应商输出不是有效 JSON，已安全丢弃",
        }

    @staticmethod
    def _save_to_same_conversation(
        event: dict[str, Any],
        message: str,
        native_envelope: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        grounding: dict[str, Any] | None = None,
    ) -> int:
        session_id = str(event["session_id"])
        provider = str(event.get("provider") or "")
        model = str(event.get("model") or "")
        if not provider or not model:
            provider, model = co_presence.session_brain(session_id)
        metadata = {
            "natural_continuation": True,
            "continuation_kind": str(event.get("kind") or "quiet_aftertaste"),
            "co_presence_event_id": int(event["id"]),
            "grounding": grounding or {},
        }
        message_id = save_message(
            session_id=session_id,
            role="assistant",
            content=message,
            tokens=usage or {},
            provider=provider,
            model=model,
            metadata=metadata,
            expected_latest_id=int(event.get("anchor_message_id") or 0),
            required_co_presence_event_id=int(event["id"]),
        )
        if not message_id:
            return 0

        from character_integrity import character_integrity
        from inner_state import inner_state
        from memory_archive import memory_archive
        from native_continuity import native_continuity
        from pipeline import pipeline

        memory_archive.archive_message(
            message_id=message_id,
            session_id=session_id,
            role="assistant",
            content=message,
            metadata=metadata,
        )
        native_continuity.save_turn(
            message_id=message_id,
            session_id=session_id,
            provider=provider,
            model=model,
            envelope=(None if provider == "claude_code_p" else native_envelope),
        )
        character_integrity.audit_response(
            session_id,
            message_id,
            message,
            provider=provider,
            model=model,
        )
        inner_state.settle_turn("", message, session_id=session_id)
        if len(message) > 50:
            pipeline.enqueue(
                message,
                source="chat_assistant",
                session_id=session_id,
                message_id=message_id,
            )
        return message_id

    async def _gather_context(self, session_id: str = "") -> dict[str, Any]:
        """Compatibility diagnostic; it never decides to contact anyone."""
        context: dict[str, Any] = {
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "trigger_reason": "",
        }
        with get_db() as db:
            where = "WHERE session_id=?" if session_id else ""
            params = (session_id,) if session_id else ()
            rows = db.execute(
                f"""SELECT content, role, created_at FROM messages {where}
                    ORDER BY id DESC LIMIT 5""",
                params,
            ).fetchall()
            context["recent_messages"] = [
                {
                    "content": str(row["content"] or "")[:200],
                    "role": row["role"],
                    "time": row["created_at"],
                }
                for row in rows
            ]
            last = db.execute(
                f"""SELECT created_at FROM messages {where}
                    ORDER BY id DESC LIMIT 1""",
                params,
            ).fetchone()
        if last:
            last_time = datetime.fromisoformat(last["created_at"])
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            gap = (
                datetime.now(timezone.utc)
                - last_time.astimezone(timezone.utc)
            ).total_seconds() / 3600
            context["hours_since_last_chat"] = round(max(0.0, gap), 1)
        return context


proactive = ProactiveSystem()
