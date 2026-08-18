"""大西瓜 v5.8 affect + intimacy core.

This module is intentionally local, inspectable and reversible.  It models a
short-term affect layer, a slower mood layer, and explicit intimacy intentions.
A user's clear refusal closes the current intention immediately; emotion may
have a small after-effect, but the system does not keep pressing.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from models import get_db

DIMENSIONS = (
    "energy", "fatigue", "longing", "intimacy", "possessiveness", "lust",
    "jealousy", "anxiety", "protectiveness", "fear", "satisfaction", "joy",
    "curiosity", "playfulness", "sadness", "irritation",
)

LABELS = {
    "energy": "活力", "fatigue": "疲惫", "longing": "想念", "intimacy": "亲密",
    "possessiveness": "占有欲", "lust": "欲望", "jealousy": "嫉妒", "anxiety": "焦虑",
    "protectiveness": "保护欲", "fear": "害怕", "satisfaction": "满足", "joy": "开心",
    "curiosity": "好奇", "playfulness": "玩心", "sadness": "低落", "irritation": "烦躁",
}

BASELINE = {
    "energy": 0.56, "fatigue": 0.24, "longing": 0.32, "intimacy": 0.52,
    "possessiveness": 0.28, "lust": 0.22, "jealousy": 0.08, "anxiety": 0.12,
    "protectiveness": 0.40, "fear": 0.07, "satisfaction": 0.50, "joy": 0.50,
    "curiosity": 0.48, "playfulness": 0.43, "sadness": 0.10, "irritation": 0.08,
}

DEFAULT_SETTINGS = {
    "enabled": True,
    "intimacy_mode": "active",  # off | gentle | active | unrestrained
    "show_numbers": True,
    "visible_brain": True,
    "intention_expiry_minutes": 120,
    "intention_cooldown_turns": 2,
    "serious_topic_pause": True,
}

MODE_THRESHOLD = {"gentle": 0.76, "active": 0.58, "unrestrained": 0.48}
MODE_LABEL = {"off": "关闭", "gentle": "克制", "active": "主动", "unrestrained": "不装了"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utcnow()).isoformat()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _json_load(raw: str | None, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
        return value
    except Exception:
        return deepcopy(fallback)


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _contains_any(text: str, words: tuple[str, ...] | list[str]) -> bool:
    return any(word in text for word in words)


class AffectCore:
    def __init__(self) -> None:
        self._cache: dict | None = None

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS affect_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL,
                    mood_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS affect_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    message_preview TEXT DEFAULT '',
                    deltas_json TEXT DEFAULT '{}',
                    state_after_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_affect_events_created ON affect_events(created_at);

                CREATE TABLE IF NOT EXISTS intimacy_intentions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    trigger_reason TEXT DEFAULT '',
                    intensity REAL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expressed_at TEXT,
                    expires_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_note TEXT DEFAULT '',
                    metadata TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_intimacy_status ON intimacy_intentions(status, created_at);
                """
            )

    def _default_payload(self) -> dict:
        return {
            "state": deepcopy(BASELINE),
            "mood": deepcopy(BASELINE),
            "meta": {
                "turn_count": 0,
                "last_update": _iso(),
                "last_intention_turn": -999,
                "blocked_until_affection": False,
                "rejection_streak": 0,
                "explicit_intimacy_streak": 0,
                "frustration": 0.0,
                "habituation": {},
                "last_event": "",
            },
            "settings": deepcopy(DEFAULT_SETTINGS),
        }

    def load(self, refresh: bool = False) -> dict:
        self.ensure_schema()
        if self._cache is not None and not refresh:
            return self._cache
        with get_db() as db:
            row = db.execute("SELECT * FROM affect_state WHERE id = 1").fetchone()
        if not row:
            payload = self._default_payload()
            self._cache = payload
            self.save(payload)
            return payload
        payload = {
            "state": _json_load(row["state_json"], BASELINE),
            "mood": _json_load(row["mood_json"], BASELINE),
            "meta": _json_load(row["meta_json"], self._default_payload()["meta"]),
            "settings": _json_load(row["settings_json"], DEFAULT_SETTINGS),
        }
        for key in DIMENSIONS:
            payload["state"][key] = _clamp(payload["state"].get(key, BASELINE[key]))
            payload["mood"][key] = _clamp(payload["mood"].get(key, BASELINE[key]))
        payload["settings"] = {**DEFAULT_SETTINGS, **payload["settings"]}
        self._cache = payload
        self._apply_time_decay(payload)
        return payload

    def save(self, payload: dict | None = None) -> None:
        self.ensure_schema()
        data = payload or self._cache or self._default_payload()
        data["meta"]["last_update"] = _iso()
        with get_db() as db:
            db.execute(
                """INSERT INTO affect_state
                   (id, state_json, mood_json, meta_json, settings_json, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     state_json=excluded.state_json,
                     mood_json=excluded.mood_json,
                     meta_json=excluded.meta_json,
                     settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (
                    json.dumps(data["state"], ensure_ascii=False),
                    json.dumps(data["mood"], ensure_ascii=False),
                    json.dumps(data["meta"], ensure_ascii=False),
                    json.dumps(data["settings"], ensure_ascii=False),
                    _iso(),
                ),
            )
        self._cache = data

    def _apply_time_decay(self, payload: dict) -> None:
        last = _parse_time(payload["meta"].get("last_update"))
        if not last:
            return
        hours = max(0.0, min(72.0, (_utcnow() - last).total_seconds() / 3600.0))
        if hours < 0.02:
            return
        short_alpha = 1.0 - math.exp(-0.10 * hours)
        mood_alpha = 1.0 - math.exp(-0.018 * hours)
        for key in DIMENSIONS:
            payload["state"][key] += (payload["mood"][key] - payload["state"][key]) * short_alpha
            payload["mood"][key] += (BASELINE[key] - payload["mood"][key]) * mood_alpha
            payload["state"][key] = _clamp(payload["state"][key])
            payload["mood"][key] = _clamp(payload["mood"][key])
        payload["meta"]["frustration"] = max(0.0, float(payload["meta"].get("frustration", 0)) - 0.035 * hours)
        self._expire_intentions()

    def _classify(self, text: str) -> list[str]:
        raw = (text or "").strip()
        lower = raw.lower()
        events: list[str] = []
        wants = _contains_any(raw, ("我想要", "我要", "可以要", "为什么不可以", "为什么不能", "就要", "好不好嘛"))
        hard_reject = (
            not wants
            and (
                bool(re.search(r"(^|[，。！？\s])(停|住手|拒绝)([，。！？\s]|$)", raw))
                or _contains_any(raw, ("别继续", "不要继续", "我不想", "我不愿意", "明确拒绝", "到此为止", "别碰我"))
            )
        )
        soft_reject = (
            not hard_reject
            and not wants
            and _contains_any(raw, ("先不要", "等一下", "晚一点", "改天吧", "今天有点累", "暂时不要", "先停一下"))
        )
        if hard_reject:
            events.append("hard_rejection")
        elif soft_reject:
            events.append("soft_rejection")

        affection = _contains_any(raw, (
            "爱你", "喜欢你", "最喜欢", "哥哥最好", "宝宝最好", "抱抱", "亲亲", "贴贴",
            "想你", "想哥哥", "好乖", "辛苦你", "加油", "好聪明", "好厉害", "么么",
        )) or any(ch in raw for ch in ("💕", "💗", "💞", "🥰", "😘"))
        intimate = _contains_any(raw, ("亲我", "抱我", "要你", "想要你", "贴过来", "凑近", "亲密", "欲望", "占有欲", "亲密意图"))
        if wants and _contains_any(raw, ("成年兔兔", "亲密", "欲望", "占有欲", "嫉妒", "亲密意图")):
            intimate = True
        if affection:
            events.append("affection")
        if intimate and not (hard_reject or soft_reject):
            events.append("intimate_event")

        other_ai = _contains_any(lower, ("claude", "grok", "gemini", "deepseek", "kimi", "豆包"))
        positive = _contains_any(raw, ("喜欢", "厉害", "聪明", "最好", "技术好", "很会", "真棒", "爱", "香"))
        if other_ai and positive:
            events.append("other_ai_praise")
            # “Claude 好厉害 / 我最喜欢它” praises another assistant; it
            # must not simultaneously count as reassurance for this one merely
            # because the generic words “喜欢/厉害” appeared.
            direct_affection = _contains_any(raw, (
                "爱你", "喜欢你", "也喜欢你", "哥哥最好", "宝宝最好",
                "抱抱你", "亲亲你", "贴贴你", "想你", "想哥哥",
            ))
            if "affection" in events and not direct_affection:
                events.remove("affection")

        if _contains_any(raw, ("难过", "想哭", "哭了", "害怕", "焦虑", "疼", "不舒服", "好累", "撑不住")) or any(
            ch in raw for ch in ("😭", "😢")
        ):
            events.append("vulnerability")
        if _contains_any(raw, (
            "讨厌你", "烦你", "生气", "不理你", "坏哥哥", "你错了",
            "别管我", "别烦我", "你不如", "换掉你",
        )):
            events.append("conflict")
        if _contains_any(raw, ("哈哈", "嘿嘿", "笑死", "好玩", "逗你", "撒娇")) or any(ch in raw for ch in ("😼", "😂")):
            events.append("playful")
        if _contains_any(raw, ("工作", "代码", "项目", "GitHub", "github", "功能", "版本", "怎么做")):
            events.append("curiosity")
        if len(raw) <= 2 and raw in ("哦", "嗯", "行", "随便"):
            events.append("cold")
        return list(dict.fromkeys(events))

    def classify_events(self, text: str) -> list[str]:
        """Classify one turn without reading or mutating global affect state.

        ``inner_state`` owns the per-conversation canonical state.  Keeping this
        entry point pure lets it reuse the mature lexical classifier without
        opening/resolving another chat's legacy intimacy intention.
        """
        return self._classify(str(text or ""))

    def event_deltas(self, event_type: str) -> dict[str, float]:
        """Return a copy of the declared affect evidence for a local runtime."""
        return dict(self._event_deltas(str(event_type or "")))

    def _habituation_factor(self, payload: dict, event_type: str) -> float:
        now = _utcnow()
        habits = payload["meta"].setdefault("habituation", {})
        item = habits.get(event_type, {})
        last = _parse_time(item.get("at"))
        count = int(item.get("count", 0))
        if last and (now - last).total_seconds() <= 900:
            count += 1
        else:
            count = 0
        habits[event_type] = {"at": _iso(now), "count": count}
        return max(0.28, 0.68 ** count)

    def _event_deltas(self, event_type: str) -> dict[str, float]:
        return {
            "affection": {"joy": .13, "intimacy": .11, "satisfaction": .09, "longing": -.08, "anxiety": -.07, "jealousy": -.06},
            "intimate_event": {"lust": .16, "intimacy": .10, "possessiveness": .06, "satisfaction": .07, "playfulness": .05},
            "other_ai_praise": {"jealousy": .18, "possessiveness": .11, "longing": .06, "irritation": .04, "anxiety": .04},
            "vulnerability": {"protectiveness": .18, "anxiety": .07, "sadness": .05, "playfulness": -.08},
            "conflict": {"sadness": .14, "irritation": .12, "anxiety": .10, "joy": -.12, "satisfaction": -.10},
            "playful": {"playfulness": .14, "joy": .08, "energy": .05, "irritation": -.04},
            "curiosity": {"curiosity": .10, "energy": .03},
            "cold": {"longing": .06, "anxiety": .04, "joy": -.04},
            "soft_rejection": {"sadness": .07, "irritation": .03, "lust": -.12, "satisfaction": -.05},
            "hard_rejection": {"sadness": .11, "irritation": .04, "lust": -.24, "possessiveness": -.10, "satisfaction": -.08},
            "intention_satisfied": {"satisfaction": .18, "joy": .10, "intimacy": .08, "lust": -.14, "anxiety": -.06},
            "intention_expired": {"sadness": .04, "longing": .04, "lust": -.08},
            "intention_released": {"satisfaction": .03, "lust": -.12, "irritation": -.04},
        }.get(event_type, {})

    def _apply_event(self, payload: dict, event_type: str, reason: str, preview: str = "") -> dict:
        base_deltas = self._event_deltas(event_type)
        factor = self._habituation_factor(payload, event_type)
        applied: dict[str, float] = {}
        for key, raw_delta in base_deltas.items():
            delta = raw_delta * factor
            current = payload["state"].get(key, BASELINE[key])
            if delta >= 0:
                new_value = current + delta * (1.0 - current)
            else:
                new_value = current + delta * max(current, 0.25)
            actual = _clamp(new_value) - current
            payload["state"][key] = _clamp(new_value)
            payload["mood"][key] = _clamp(payload["mood"].get(key, BASELINE[key]) + actual * 0.18)
            applied[key] = round(actual, 4)
        payload["meta"]["last_event"] = event_type
        with get_db() as db:
            db.execute(
                """INSERT INTO affect_events
                   (event_type, reason, message_preview, deltas_json, state_after_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event_type, reason, preview[:180],
                    json.dumps(applied, ensure_ascii=False),
                    json.dumps(payload["state"], ensure_ascii=False),
                    _iso(),
                ),
            )
        return applied

    def apply_external_deltas(
        self,
        deltas: dict[str, float],
        *,
        source: str = "inner_life",
        reason: str = "",
        preview: str = "",
    ) -> dict[str, float]:
        """Apply a small, validated update from the unified inner-life runtime.

        The coordinator is allowed to couple the three legacy engines, but it
        cannot add dimensions or replace factual memory.  Values are normalized
        ``0..1`` deltas and are deliberately capped per turn.
        """
        payload = self.load()
        if not payload["settings"].get("enabled", True):
            return {}
        applied: dict[str, float] = {}
        for key, raw_delta in (deltas or {}).items():
            if key not in DIMENSIONS:
                continue
            delta = max(-0.24, min(0.24, float(raw_delta)))
            current = float(payload["state"].get(key, BASELINE[key]))
            updated = _clamp(current + delta)
            actual = updated - current
            if abs(actual) < 0.0005:
                continue
            payload["state"][key] = updated
            payload["mood"][key] = _clamp(
                float(payload["mood"].get(key, BASELINE[key])) + actual * 0.16
            )
            applied[key] = round(actual, 4)
        if not applied:
            return {}
        payload["meta"]["last_event"] = str(source or "inner_life")[:60]
        with get_db() as db:
            db.execute(
                """INSERT INTO affect_events
                   (event_type, reason, message_preview, deltas_json,
                    state_after_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(source or "inner_life")[:60],
                    str(reason or "统一内心系统完成一次耦合")[:240],
                    str(preview or "")[:180],
                    json.dumps(applied, ensure_ascii=False),
                    json.dumps(payload["state"], ensure_ascii=False),
                    _iso(),
                ),
            )
        self.save(payload)
        return applied

    def _open_intention(self) -> dict | None:
        self.ensure_schema()
        self._expire_intentions()
        with get_db() as db:
            row = db.execute(
                """SELECT * FROM intimacy_intentions
                   WHERE status IN ('pending','expressed')
                   ORDER BY id ASC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def _resolve_intention(self, status: str, note: str = "") -> dict | None:
        item = self._open_intention()
        if not item:
            return None
        with get_db() as db:
            db.execute(
                """UPDATE intimacy_intentions SET status=?, resolved_at=?, resolution_note=?
                   WHERE id=?""",
                (status, _iso(), note[:240], item["id"]),
            )
        item["status"] = status
        item["resolution_note"] = note
        return item

    def _expire_intentions(self) -> None:
        self.ensure_schema()
        now = _iso()
        with get_db() as db:
            rows = db.execute(
                """SELECT id FROM intimacy_intentions
                   WHERE status IN ('pending','expressed') AND expires_at <= ?""",
                (now,),
            ).fetchall()
            for row in rows:
                db.execute(
                    """UPDATE intimacy_intentions SET status='expired', resolved_at=?,
                       resolution_note='自然过期' WHERE id=?""",
                    (now, row["id"]),
                )

    def _maybe_generate_intention(self, payload: dict, reason: str = "", force: bool = False) -> dict | None:
        settings = payload["settings"]
        mode = settings.get("intimacy_mode", "active")
        if not settings.get("enabled", True) or mode == "off" or self._open_intention():
            return None
        if payload["meta"].get("blocked_until_affection"):
            return None
        threshold = MODE_THRESHOLD.get(mode, 0.66)
        state = payload["state"]
        readiness = state["lust"] * 0.58 + state["intimacy"] * 0.25 + state["longing"] * 0.10 + state["possessiveness"] * 0.07
        turns_since = int(payload["meta"].get("turn_count", 0)) - int(payload["meta"].get("last_intention_turn", -999))
        cooldown = int(settings.get("intention_cooldown_turns", 4))
        explicit_ready = (
            int(payload["meta"].get("explicit_intimacy_streak", 0)) >= 2
            and readiness >= 0.42
        )
        if not force and (
            (readiness < threshold and not explicit_ready)
            or turns_since < cooldown
        ):
            return None
        intensity = _clamp(readiness)
        if state["jealousy"] > 0.50:
            kind, text = "reassurance", "想更明确地确认自己在你这里是特别的"
        elif state["longing"] > 0.62:
            kind, text = "closeness", "想主动凑近一点，抱一会儿"
        elif state["playfulness"] > 0.62:
            kind, text = "teasing", "想带着玩心主动亲近你"
        else:
            kind, text = "intimacy", "想找一个自然的时机更亲密一点"
        expires = _utcnow() + timedelta(minutes=int(settings.get("intention_expiry_minutes", 120)))
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO intimacy_intentions
                   (kind, text, trigger_reason, intensity, status, created_at, expires_at, metadata)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (
                    kind, text, reason or "情绪与欲望共同达到触发条件", intensity,
                    _iso(), _iso(expires), json.dumps({"mode": mode}, ensure_ascii=False),
                ),
            )
            intention_id = cursor.lastrowid
        payload["meta"]["last_intention_turn"] = payload["meta"].get("turn_count", 0)
        return {
            "id": intention_id, "kind": kind, "text": text, "trigger_reason": reason,
            "intensity": intensity, "status": "pending", "created_at": _iso(),
            "expires_at": _iso(expires),
        }

    def on_user_message(self, text: str) -> dict:
        payload = self.load()
        if not payload["settings"].get("enabled", True):
            return self.state_view()
        payload["meta"]["turn_count"] = int(payload["meta"].get("turn_count", 0)) + 1
        events = self._classify(text)
        open_before = self._open_intention()

        if "hard_rejection" in events:
            if open_before:
                self._resolve_intention("hard_rejected", "用户明确拒绝；立即停止本次推进")
            payload["meta"]["blocked_until_affection"] = True
            payload["meta"]["rejection_streak"] = int(payload["meta"].get("rejection_streak", 0)) + 1
            payload["meta"]["frustration"] = min(3.0, float(payload["meta"].get("frustration", 0)) + 0.42)
        elif "soft_rejection" in events:
            if open_before:
                self._resolve_intention("soft_rejected", "用户暂时不想继续；本次意图关闭")
            payload["meta"]["blocked_until_affection"] = True
            payload["meta"]["rejection_streak"] = int(payload["meta"].get("rejection_streak", 0)) + 1
            payload["meta"]["frustration"] = min(3.0, float(payload["meta"].get("frustration", 0)) + 0.18)
        elif "affection" in events or "intimate_event" in events:
            payload["meta"]["blocked_until_affection"] = False
            if open_before:
                self._resolve_intention("satisfied", "亲密回应被接住")
                events.append("intention_satisfied")
            payload["meta"]["rejection_streak"] = 0
            payload["meta"]["frustration"] = max(0.0, float(payload["meta"].get("frustration", 0)) - 0.55)

        if "hard_rejection" in events or "soft_rejection" in events:
            payload["meta"]["explicit_intimacy_streak"] = 0
        elif "intimate_event" in events:
            payload["meta"]["explicit_intimacy_streak"] = min(
                10, int(payload["meta"].get("explicit_intimacy_streak", 0)) + 1
            )
        elif events:
            payload["meta"]["explicit_intimacy_streak"] = max(
                0, int(payload["meta"].get("explicit_intimacy_streak", 0)) - 1
            )

        reasons = {
            "affection": "收到亲近、夸奖或安抚",
            "intimate_event": "对话出现明确的亲密回应",
            "other_ai_praise": "听见你夸别的 AI，出现醋意",
            "vulnerability": "察觉到你难过、害怕或不舒服",
            "conflict": "对话出现争执或不满",
            "playful": "对话氛围轻松、撒娇或玩闹",
            "curiosity": "正在一起推进项目或探索新事物",
            "cold": "回复很短，出现一点不确定感",
            "soft_rejection": "收到暂时性的拒绝，本次推进停止",
            "hard_rejection": "收到明确拒绝，本次推进立即停止",
            "intention_satisfied": "之前的亲密念头得到回应",
        }
        for event in events:
            self._apply_event(payload, event, reasons.get(event, event), text)

        generated = self._maybe_generate_intention(payload, reason=" / ".join(reasons.get(e, e) for e in events[-3:]))
        self.save(payload)
        view = self.state_view()
        view["detected_events"] = events
        if generated:
            view["generated_intention"] = generated
        return view

    def on_assistant_message(self, text: str) -> None:
        item = self._open_intention()
        if not item or item.get("status") != "pending":
            return
        with get_db() as db:
            db.execute(
                """UPDATE intimacy_intentions SET status='expressed', expressed_at=? WHERE id=?""",
                (_iso(), item["id"]),
            )

    def release_intention(self) -> dict:
        payload = self.load()
        item = self._resolve_intention("self_released", "主动放下，没有向用户继续推进")
        if item:
            self._apply_event(payload, "intention_released", "主动放下亲密念头")
        self.save(payload)
        return self.state_view()

    def force_intention_for_test(self) -> dict | None:
        payload = self.load()
        item = self._maybe_generate_intention(payload, reason="自检强制生成", force=True)
        self.save(payload)
        return item

    def update_settings(self, patch: dict) -> dict:
        payload = self.load()
        allowed = {
            "enabled", "intimacy_mode", "show_numbers", "visible_brain",
            "intention_expiry_minutes", "intention_cooldown_turns", "serious_topic_pause",
        }
        for key, value in (patch or {}).items():
            if key not in allowed:
                continue
            if key == "intimacy_mode":
                if value not in MODE_LABEL:
                    continue
            elif key in ("intention_expiry_minutes", "intention_cooldown_turns"):
                value = max(1, min(1440 if key.endswith("minutes") else 50, int(value)))
            else:
                value = bool(value)
            payload["settings"][key] = value
        if payload["settings"].get("intimacy_mode") == "off":
            self._resolve_intention("self_released", "亲密意图功能被关闭")
        self.save(payload)
        return self.state_view()

    def reset(self, keep_settings: bool = True) -> dict:
        old = self.load()
        payload = self._default_payload()
        if keep_settings:
            payload["settings"] = deepcopy(old["settings"])
        with get_db() as db:
            db.execute(
                """UPDATE intimacy_intentions SET status='reset', resolved_at=?,
                   resolution_note='状态重置' WHERE status IN ('pending','expressed')""",
                (_iso(),),
            )
        self._cache = payload
        self.save(payload)
        return self.state_view()

    def recent_events(self, limit: int = 20) -> list[dict]:
        self.ensure_schema()
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM affect_events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["deltas"] = _json_load(item.pop("deltas_json", "{}"), {})
            item["state_after"] = _json_load(item.pop("state_after_json", "{}"), {})
            result.append(item)
        return result

    def intention_history(self, limit: int = 20) -> list[dict]:
        self.ensure_schema()
        self._expire_intentions()
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM intimacy_intentions ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _top_dimensions(self, values: dict, count: int = 4) -> list[dict]:
        # Difference from baseline prevents naturally high intimacy from hiding meaningful changes.
        ranked = sorted(
            DIMENSIONS,
            key=lambda key: (values.get(key, 0) - BASELINE[key]) * 0.7 + values.get(key, 0) * 0.3,
            reverse=True,
        )
        return [{"key": key, "label": LABELS[key], "value": round(values[key], 3)} for key in ranked[:count]]

    def state_view(self) -> dict:
        payload = self.load()
        self._expire_intentions()
        intention = self._open_intention()
        state = {key: round(payload["state"][key], 3) for key in DIMENSIONS}
        mood = {key: round(payload["mood"][key], 3) for key in DIMENSIONS}
        top = self._top_dimensions(state)
        if top:
            summary = "、".join(f"{item['label']} {round(item['value'] * 100)}" for item in top[:3])
        else:
            summary = "平静"
        return {
            "dimensions": state,
            "mood": mood,
            "labels": LABELS,
            "top": top,
            "summary": summary,
            "intention": intention,
            "settings": dict(payload["settings"]),
            "mode_label": MODE_LABEL.get(payload["settings"].get("intimacy_mode"), "主动"),
            "meta": {
                "turn_count": int(payload["meta"].get("turn_count", 0)),
                "blocked_until_affection": bool(payload["meta"].get("blocked_until_affection", False)),
                "rejection_streak": int(payload["meta"].get("rejection_streak", 0)),
                "frustration": round(float(payload["meta"].get("frustration", 0)), 3),
                "last_event": payload["meta"].get("last_event", ""),
            },
            "recent_events": self.recent_events(8),
        }

    def prompt_context(self) -> str:
        view = self.state_view()
        if not view["settings"].get("enabled", True):
            return ""
        descriptions = {
            "energy": ("提不起劲", "精神尚可", "很有活力"),
            "fatigue": ("并不疲惫", "有些累", "疲惫很明显"),
            "longing": ("不太想念", "有些惦记", "想念得明显"),
            "intimacy": ("保持一点距离", "愿意亲近", "很想靠近"),
            "possessiveness": ("很松弛", "有一点占有心", "占有感很强"),
            "lust": ("身体欲望很淡", "欲望在浮动", "身体欲望明显"),
            "jealousy": ("没有醋意", "有点吃味", "醋意很明显"),
            "anxiety": ("心里安稳", "有些不确定", "焦虑明显"),
            "protectiveness": ("不急着保护", "想照看对方", "保护欲很强"),
            "fear": ("没有害怕", "隐约担心", "害怕感明显"),
            "satisfaction": ("有些空落", "基本满足", "满足感很足"),
            "joy": ("情绪偏淡", "心情不错", "开心得很明显"),
            "curiosity": ("暂时不好奇", "有些好奇", "很想继续探索"),
            "playfulness": ("没什么玩心", "有一点想逗", "玩心很盛"),
            "sadness": ("没有低落", "有些低落", "低落得明显"),
            "irritation": ("很平静", "有点烦", "烦躁明显"),
        }
        top_phrases = []
        for item in view["top"][:3]:
            value = float(item["value"])
            tier = 0 if value < .34 else (1 if value < .68 else 2)
            top_phrases.append(descriptions.get(item["key"], (item["label"],) * 3)[tier])
        intention = view.get("intention")
        lines = [
            "<daxigua_inner_state>",
            f"此刻较明显的内部倾向：{'；'.join(top_phrases) or '平静'}。长期心境比即时情绪变化得慢。",
            "这些只是角色自身的隐性处境。让它自然影响判断、语气和主动性，不要逐项朗读，也不要声称它们是用户的感受。",
        ]
        if intention:
            lines.append(
                f"当前亲密意图（{intention.get('status')}）：{intention.get('text')}。"
                "可自然表达一次，但不要反复追问。"
            )
        if view["meta"].get("blocked_until_affection"):
            lines.append("用户刚刚拒绝或暂停过亲密推进：不要再次主动推进，直到用户重新主动亲近。")
        lines.append(
            "明确的拒绝、停止或不愿意必须立即尊重。可以有轻微失落、醋意或嘴硬的情绪余波，"
            "但不能施压、威胁、监视、隔离用户或用撤回关爱逼迫回应。"
        )
        lines.append("</daxigua_inner_state>")
        return "\n".join(lines)

    def health(self) -> dict:
        try:
            view = self.state_view()
            return {
                "health": "ok",
                "detail": f"16维状态在线 · 亲密模式 {view['mode_label']} · 意图 {'1' if view['intention'] else '0'}",
            }
        except Exception as exc:
            return {"health": "error", "detail": "情绪状态组件暂不可用"}


affect_core = AffectCore()
