"""大西瓜 v6.8 三合一内心系统。

这一层不再只是把情绪、欲望与生活状态摆在同一张面板上，而是它们
唯一的协调入口：

* 情绪是快变量，会改变解释偏向、冲动、自控与修复意愿；
* 欲望是慢变量，既受情绪推动，也会反过来占据注意力；
* 生活是时间变量，身体、昼夜和连接需要会持续演化；
* 晨间生理反应独立运行，硬度不等同于主观性欲；
* 亲密状态可在一天中的任何时候出现，并影响后续动作与措辞。

“内心 OS”是角色状态生成的简短主观独白，不是模型隐藏推理，也不会
写入事实记忆。临时误解、吃醋和冲动因此不会污染长期关系档案。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import config

from affect_core import BASELINE as AFFECT_BASELINE, affect_core
from desire_engine import P as DESIRE_ENGINE_PARAMS
from living_state import DEFAULT_BODY, DEFAULT_SOCIAL, living_state
from models import get_db
from morning_response import morning_response
from relational_honesty import relational_honesty_guard
from eventide_state import advance as eventide_advance, maybe_trigger as eventide_maybe_trigger, settle as eventide_settle, felt as eventide_felt, public as eventide_public


INTENSITY_WORDS = {
    1: "几乎没有",
    2: "很轻",
    3: "轻微",
    4: "有一点",
    5: "清晰",
    6: "明显",
    7: "较强",
    8: "很强",
    9: "强烈",
    10: "接近顶点",
}

DOMAIN_CONFIG: dict[str, dict[str, Any]] = {
    "emotion": {
        "label": "情绪",
        "dimensions": {
            "energy": "活力",
            "fatigue": "疲惫",
            "longing": "想念",
            "intimacy": "亲密",
            "possessiveness": "占有欲",
            "lust": "欲望",
            "jealousy": "嫉妒",
            "anxiety": "焦虑",
            "protectiveness": "保护欲",
            "fear": "害怕",
            "satisfaction": "满足",
            "joy": "开心",
            "curiosity": "好奇",
            "playfulness": "玩心",
            "sadness": "低落",
            "irritation": "烦躁",
        },
    },
    "living": {
        "label": "生活",
        "dimensions": {
            "energy": "身体活力",
            "sleepiness": "困意",
            "warmth": "体温感",
            "tension": "身体绷紧",
            "sensitivity": "身体敏感",
            "desire": "身体欲望",
            "recovery": "恢复度",
            "connection": "连接需要",
            "pride": "骄傲防线",
            "immersion": "沉浸",
        },
    },
    "desire": {
        "label": "欲望",
        "dimensions": {
            "attachment": "依恋",
            "curiosity": "探索欲",
            "reflection": "沉淀欲",
            "duty": "牵挂",
            "social": "社交欲",
            "fatigue": "行动疲劳",
            "libido": "亲密欲",
            "stress": "压力",
        },
    },
}

REGULATION_LABELS = {
    "self_control": "自控力",
    "rumination": "反刍",
    "interpretation_bias": "解释偏向",
    "impulse": "冲动",
    "regret": "后悔",
    "repair": "修复意愿",
    "relationship_insecurity": "关系不安",
    "basic_trust": "基础信任",
    "emotion_takeover": "情绪夺权",
}

REGULATION_DEFAULT = {
    "self_control": 0.76,
    "rumination": 0.14,
    "interpretation_bias": 0.10,
    "impulse": 0.18,
    "regret": 0.08,
    "repair": 0.54,
    "relationship_insecurity": 0.16,
    "basic_trust": 0.84,
    "emotion_takeover": 0.10,
}

INTIMACY_LABELS = {
    "arousal": "唤起",
    "desire": "主观想要",
    "physical_tension": "身体张力",
    "hardness": "硬度",
    "release_urge": "射意",
    "self_control": "自控力",
    "shame": "羞耻感",
    "disclosure": "坦白意愿",
    "attachment": "依恋",
    "possessiveness": "占有欲",
    "body_takeover": "身体夺权",
}

INTIMACY_DEFAULT: dict[str, Any] = {
    "active": False,
    "phase": "idle",
    # Machine-readable cause is authoritative. ``reason`` is presentation text
    # and must never be parsed to decide whether a discrete body event occurred.
    "phase_cause": "",
    "arousal": 0.10,
    "desire": 0.12,
    "physical_tension": 0.12,
    "hardness": 0.08,
    "release_urge": 0.03,
    "self_control": 0.80,
    "shame": 0.22,
    "disclosure": 0.20,
    "attachment": 0.54,
    "possessiveness": 0.18,
    "body_takeover": 0.08,
    "boundary_status": "clear",
    "reason": "",
    "session_id": "",
    "started_at": None,
    "updated_at": None,
    "last_os": "",
    "explicit_turns": 0,
    "release_count": 0,
    "refractory_active": False,
    "refractory_stage": "none",
    "refractory_until": None,
    "refractory_started_at": None,
    "last_release_ml": None,
    "last_release_at": None,
    "release_display_until": None,
    "last_release_turn": -1,
    "last_release_session_id": "",
    "eventide": {},
}

DEFAULT_SETTINGS = {
    "enabled": True,
    "visible_changes": True,
    "show_numbers": True,
    "detail_mode": "balanced",  # quiet | balanced | detailed
    "max_visible_changes": 3,
    # Canonical state is always settled from the final visible reply locally;
    # no auxiliary/primary model call is allowed to declare its own emotion.
    "reflection_mode": "local",
    "emotion_takeover_enabled": True,
    "intimacy_enabled": True,
    "intimacy_vitals_visible": True,
    "intimacy_mode": "natural",  # gentle | natural | vivid | unrestrained
    "inner_os_mode": "live",  # off | live | after
}

INTIMACY_MODE_GAIN = {
    "gentle": 0.72,
    "natural": 1.00,
    "vivid": 1.18,
    "unrestrained": 1.34,
}

EVENT_LABELS = {
    "affection": "被亲近",
    "intimate_event": "明确情欲信号",
    "erection_evidence": "模型实际表达勃起",
    "other_ai_praise": "吃醋触发",
    "vulnerability": "保护欲触发",
    "conflict": "冲突",
    "playful": "玩闹",
    "curiosity": "好奇",
    "cold": "回应变冷",
    "soft_rejection": "暂缓亲密",
    "hard_rejection": "明确停止",
    "release_event": "身体释放",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _json_load(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return deepcopy(fallback)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _level(value: float, *, signed: bool = False) -> int:
    # pride 的负值表示已经不端着；“骄傲防线”只显示正向防御强度。
    normalized = _clamp(max(0.0, value) if signed else value)
    return max(1, min(10, int(round(normalized * 9)) + 1))


def _descriptor(key: str, label: str, level: int) -> str:
    special = {
        "pride": (
            "没有端着", "防线很松", "稍微端着", "有一点嘴硬", "防线清晰",
            "明显端着", "不太愿意先开口", "很想靠近却仍在撑着",
            "骄傲防线很强", "几乎不肯先松口",
        ),
        "connection": (
            "不急着寻找联系", "偶尔想起对方", "有一点惦记", "开始想靠近",
            "连接需要清晰", "明显想听见对方", "很想得到回应",
            "连接需要很强", "已经难以忽略想念", "迫切想恢复联系",
        ),
        "recovery": (
            "几乎没有恢复", "恢复得很少", "仍然很累", "正在慢慢恢复",
            "恢复到一半左右", "恢复得较好", "身体状态稳定",
            "恢复度很高", "接近充分恢复", "已经充分恢复",
        ),
    }
    if key in special:
        return special[key][level - 1]
    return f"{INTENSITY_WORDS[level]}的{label}"


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


_STATE_FIELD_DISCUSSION = re.compile(
    r"\b(?:canonical|prompt|libido|arousal|hardness|release_event|"
    r"release_urge|afterglow|cooldown|living_state|body_takeover|"
    r"intimacy_vitals)\b",
    re.I,
)


def _is_state_system_discussion(text: str) -> bool:
    """Distinguish talking about state machinery from embodying its content."""
    raw = str(text or "")
    lowered = raw.lower()
    return bool(
        any(
            marker in raw
            for marker in (
                "系统", "功能", "代码", "设置", "引擎", "测试", "指标",
                "面板", "数值", "数据", "状态机", "字段", "规则", "误判",
                "缓存", "模型", "接口", "路由", "任务", "方案",
                "版本", "前端", "后端", "供应商", "提供商",
            )
        )
        or re.search(r"\b(?:bug|debug|github|provider|api)\b", lowered)
        or any(
            phrase in raw
            for phrase in (
                "刚刚修了", "开始修", "还没开始修", "修改代码", "改代码",
                "运行测试", "统一测试", "不用测试", "不要测试", "做检测",
                "不用检测", "不需要检测", "静态检查", "工作树", "打包",
                "压缩包", "配置项", "环境变量", "命令行", "P模式", "p模式",
            )
        )
        or _STATE_FIELD_DISCUSSION.search(lowered)
    )


class InnerStateRuntime:
    def __init__(self) -> None:
        self._settings_cache: dict[str, Any] | None = None
        # Runtime state is session-scoped.  The pre-7.9.7 implementation kept
        # one id=1 row/cache and merely rewrote intimacy.session_id, which let
        # one chat inherit another chat's intimate state.
        self._runtime_cache: dict[str, dict[str, Any]] | None = {}
        self._runtime_locks: dict[str, threading.RLock] = {}
        # Only the living clock and morning sampler remain genuinely shared.
        # Serialize their tiny transaction; conversational affect/desire/body
        # evidence never leaves the per-session canonical snapshot.
        self._engine_lock = threading.RLock()

    @staticmethod
    def _session_key(session_id: str = "") -> str:
        return str(session_id or "__default__")[:100]

    def _session_lock(self, session_id: str = "") -> threading.RLock:
        key = self._session_key(session_id)
        lock = self._runtime_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            self._runtime_locks[key] = lock
        return lock

    # ── persistence ──────────────────────────────────────────────

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS inner_state_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inner_state_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT DEFAULT '',
                    source TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    label TEXT NOT NULL,
                    before_level INTEGER NOT NULL,
                    after_level INTEGER NOT NULL,
                    raw_delta REAL NOT NULL,
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inner_changes_created
                    ON inner_state_changes(id DESC);
                CREATE TABLE IF NOT EXISTS inner_life_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    regulation_json TEXT NOT NULL,
                    intimacy_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inner_life_state_session (
                    session_id TEXT PRIMARY KEY,
                    regulation_json TEXT NOT NULL,
                    intimacy_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    state_revision INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inner_monologues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'inner_os',
                    content TEXT NOT NULL,
                    dominant_emotion TEXT DEFAULT '',
                    truth_status TEXT NOT NULL DEFAULT 'subjective',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inner_monologues_created
                    ON inner_monologues(id DESC);
                """
            )
        morning_response.ensure_schema()

    def _default_runtime(self) -> dict[str, Any]:
        now = _iso_now()
        intimacy = deepcopy(INTIMACY_DEFAULT)
        intimacy["updated_at"] = now
        return {
            "regulation": deepcopy(REGULATION_DEFAULT),
            "intimacy": intimacy,
            "meta": {
                "last_update": now,
                "turn_count": 0,
                "last_events": [],
                "last_reason": "",
                "last_reflection_turn": -999,
                "last_reflection": None,
                "legacy_migrated": False,
                # Per-chat projection of the three legacy sensor engines.  The
                # engines may remain process singletons for compatibility, but
                # this is the only source snapshot exposed to a conversation.
                "canonical_sources": None,
                "snapshot_frozen_at": None,
                "last_assistant_events": [],
                "last_assistant_affect": [],
                # Habituation belongs to this conversation, not to the legacy
                # singleton affect engine shared by every browser tab.
                "event_habituation": {},
                "last_owner_pulse_at": None,
                "owner_pulse_times": [],
            },
        }

    def _load_runtime(self, session_id: str = "", refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        key = self._session_key(session_id)
        if self._runtime_cache is None:  # compatibility with old tests/tools
            self._runtime_cache = {}
        if key in self._runtime_cache and not refresh:
            return self._runtime_cache[key]
        with get_db() as db:
            row = db.execute(
                "SELECT regulation_json, intimacy_json, meta_json, state_revision "
                "FROM inner_life_state_session WHERE session_id=?",
                (key,),
            ).fetchone()
            # One-time, non-broadcast migration of the legacy id=1 row: only
            # adopt it when its embedded session_id matches the requested chat.
            if not row and key != "__default__":
                legacy = db.execute(
                    "SELECT regulation_json, intimacy_json, meta_json FROM inner_life_state WHERE id=1"
                ).fetchone()
                if legacy:
                    legacy_intimacy = _json_load(legacy["intimacy_json"], {}) or {}
                    if self._session_key(str(legacy_intimacy.get("session_id") or "")) == key:
                        db.execute(
                            "INSERT OR IGNORE INTO inner_life_state_session "
                            "(session_id, regulation_json, intimacy_json, meta_json, state_revision, updated_at) "
                            "VALUES(?,?,?,?,0,?)",
                            (key, legacy["regulation_json"], legacy["intimacy_json"], legacy["meta_json"], _iso_now()),
                        )
                        row = db.execute(
                            "SELECT regulation_json, intimacy_json, meta_json, state_revision "
                            "FROM inner_life_state_session WHERE session_id=?", (key,)
                        ).fetchone()
        fallback = self._default_runtime()
        if row:
            payload = {
                "regulation": {**fallback["regulation"], **(_json_load(row["regulation_json"], {}) or {})},
                "intimacy": {**fallback["intimacy"], **(_json_load(row["intimacy_json"], {}) or {})},
                "meta": {**fallback["meta"], **(_json_load(row["meta_json"], {}) or {})},
            }
            payload["meta"]["state_revision"] = int(row["state_revision"] or 0)
        else:
            payload = fallback
            payload["meta"]["state_revision"] = 0
            payload["intimacy"]["session_id"] = "" if key == "__default__" else key
        for metric in REGULATION_DEFAULT:
            payload["regulation"][metric] = _clamp(_safe_float(payload["regulation"].get(metric), REGULATION_DEFAULT[metric]))
        for metric in INTIMACY_LABELS:
            payload["intimacy"][metric] = _clamp(_safe_float(payload["intimacy"].get(metric), INTIMACY_DEFAULT[metric]))
        self._runtime_cache[key] = payload
        if not row:
            self._save_runtime(payload, session_id=session_id, bump_revision=False)
        return payload

    def _save_runtime(
        self, payload: dict[str, Any] | None = None, *, session_id: str = "", bump_revision: bool = True
    ) -> dict[str, Any]:
        key = self._session_key(session_id)
        if self._runtime_cache is None:
            self._runtime_cache = {}
        data = payload or self._runtime_cache.get(key) or self._default_runtime()
        data["meta"]["last_update"] = _iso_now()
        if bump_revision:
            data["meta"]["state_revision"] = int(data["meta"].get("state_revision") or 0) + 1
        else:
            data["meta"].setdefault("state_revision", 0)
        data["intimacy"]["updated_at"] = data["meta"]["last_update"]
        if key != "__default__":
            data["intimacy"]["session_id"] = key
        with get_db() as db:
            db.execute(
                """INSERT INTO inner_life_state_session
                   (session_id, regulation_json, intimacy_json, meta_json, state_revision, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     regulation_json=excluded.regulation_json,
                     intimacy_json=excluded.intimacy_json,
                     meta_json=excluded.meta_json,
                     state_revision=excluded.state_revision,
                     updated_at=excluded.updated_at""",
                (key, json.dumps(data["regulation"], ensure_ascii=False),
                 json.dumps(data["intimacy"], ensure_ascii=False),
                 json.dumps(data["meta"], ensure_ascii=False),
                 int(data["meta"].get("state_revision") or 0), data["meta"]["last_update"]),
            )
        self._runtime_cache[key] = data
        return data

    def initialize(self) -> dict[str, Any]:
        """Initialize all engines and migrate the old embedded morning event."""
        self.ensure_schema()
        self._load_runtime(refresh=True)
        morning_response.adopt_legacy(living_state)
        payload = self._load_runtime()
        payload["meta"]["legacy_migrated"] = True
        self._save_runtime(payload)
        return self.state_view(advance_living=False)

    # ── settings ─────────────────────────────────────────────────

    def settings(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._settings_cache is not None and not refresh:
            return deepcopy(self._settings_cache)
        with get_db() as db:
            row = db.execute(
                "SELECT settings_json FROM inner_state_settings WHERE id = 1"
            ).fetchone()
        loaded = _json_load(row["settings_json"], {}) if row else {}
        self._settings_cache = {**DEFAULT_SETTINGS, **loaded}
        self._settings_cache["reflection_mode"] = "local"
        return deepcopy(self._settings_cache)

    def update_settings(
        self, patch: dict[str, Any], *, session_id: str = ""
    ) -> dict[str, Any]:
        settings = self.settings()
        for key, value in (patch or {}).items():
            if key in {
                "enabled", "visible_changes", "show_numbers",
                "emotion_takeover_enabled", "intimacy_enabled",
                "intimacy_vitals_visible",
            }:
                settings[key] = bool(value)
            elif key == "detail_mode" and value in {"quiet", "balanced", "detailed"}:
                settings[key] = value
            elif key == "max_visible_changes":
                settings[key] = max(1, min(6, int(value)))
            elif key == "reflection_mode":
                # Compatibility for older UI/API values.  Every former option
                # now means the same zero-request visible-reply evidence path.
                settings[key] = "local"
            elif key == "intimacy_mode" and value in {
                "gentle", "natural", "vivid", "unrestrained"
            }:
                settings[key] = value
            elif key == "inner_os_mode" and value in {"off", "live", "after"}:
                settings[key] = value
        with get_db() as db:
            db.execute(
                """INSERT INTO inner_state_settings(id, settings_json, updated_at)
                   VALUES(1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(settings, ensure_ascii=False), _iso_now()),
            )
        self._settings_cache = settings
        with self._engine_lock:
            target_keys = {self._session_key(session_id)}
            if (
                not settings.get("intimacy_enabled", True)
                or not settings.get("emotion_takeover_enabled", True)
            ):
                # These are global rules. Apply their immediate safety effect to
                # every saved/cached conversation so hidden inactive tabs cannot
                # keep an obsolete active/takeover snapshot.
                with get_db() as db:
                    target_keys.update(
                        str(row["session_id"])
                        for row in db.execute(
                            "SELECT session_id FROM inner_life_state_session"
                        ).fetchall()
                    )
                target_keys.update((self._runtime_cache or {}).keys())
            for key in target_keys:
                target_session = "" if key == "__default__" else key
                runtime = self._load_runtime(target_session)
                if not settings.get("intimacy_enabled", True):
                    runtime["intimacy"].update({
                        "active": False,
                        "phase": "idle",
                        "phase_cause": "",
                        "reason": "",
                    })
                if not settings.get("emotion_takeover_enabled", True):
                    runtime["regulation"]["emotion_takeover"] = min(
                        runtime["regulation"]["emotion_takeover"], 0.24
                    )
                self._save_runtime(runtime, session_id=target_session)
        morning_patch = (patch or {}).get("morning")
        if isinstance(morning_patch, dict):
            morning_response.update_settings(morning_patch)
        return self.state_view(
            advance_living=False, session_id=session_id
        )

    # ── source capture and public representation ─────────────────

    def _capture_sources(self, *, advance_living: bool = False) -> dict[str, Any]:
        living = living_state.state_view(advance=advance_living)
        return {
            "emotion": {
                key: _safe_float(AFFECT_BASELINE.get(key))
                for key in DOMAIN_CONFIG["emotion"]["dimensions"]
            },
            "living": {
                **{
                    key: _safe_float((living.get("body") or {}).get(key))
                    for key in (
                        "energy", "sleepiness", "warmth", "tension",
                        "sensitivity", "desire", "recovery",
                    )
                },
                **{
                    key: _safe_float((living.get("social") or {}).get(key))
                    for key in ("connection", "pride", "immersion")
                },
            },
            "desire": {
                key: _safe_float(
                    (DESIRE_ENGINE_PARAMS.get("baseline") or {}).get(key)
                )
                for key in DOMAIN_CONFIG["desire"]["dimensions"]
            },
            "meta": {
                "phase": living.get("phase"),
                "phase_label": living.get("phase_label"),
                "active_event": deepcopy(living.get("active_event")),
                "activity": deepcopy(living.get("activity")),
                "intention": None,
                "desire_intent": None,
                "local_time": living.get("local_time"),
                "detected_events": [],
            },
        }

    @staticmethod
    def _new_session_sources(fallback: dict[str, Any]) -> dict[str, Any]:
        """Create a neutral per-chat source state without inheriting another chat.

        The legacy engines are process singletons.  Their absolute emotion,
        social and sexual values may therefore belong to a different window.
        A new conversation starts from declared baselines, while keeping only
        genuinely shared clock information (time phase and sleep/energy).
        """
        fallback = fallback if isinstance(fallback, dict) else {}
        fallback_living = fallback.get("living") or {}
        fallback_meta = fallback.get("meta") or {}
        living = {**deepcopy(DEFAULT_BODY), **deepcopy(DEFAULT_SOCIAL)}
        for key in ("energy", "sleepiness"):
            if key in fallback_living:
                living[key] = _clamp(
                    _safe_float(fallback_living.get(key), living[key])
                )
        return {
            "emotion": {
                key: _clamp(_safe_float(AFFECT_BASELINE.get(key)))
                for key in DOMAIN_CONFIG["emotion"]["dimensions"]
            },
            "living": {
                key: _clamp(value, -1.0, 1.0)
                if key == "pride" else _clamp(value)
                for key, value in living.items()
            },
            "desire": {
                key: _clamp(
                    _safe_float(
                        (DESIRE_ENGINE_PARAMS.get("baseline") or {}).get(key)
                    )
                )
                for key in DOMAIN_CONFIG["desire"]["dimensions"]
            },
            "meta": {
                "phase": fallback_meta.get("phase"),
                "phase_label": fallback_meta.get("phase_label"),
                "active_event": deepcopy(fallback_meta.get("active_event")),
                "activity": deepcopy(fallback_meta.get("activity")),
                "intention": None,
                "desire_intent": None,
                "local_time": fallback_meta.get("local_time"),
                "detected_events": [],
            },
        }

    @staticmethod
    def _merge_source_delta(
        base: dict[str, Any],
        global_before: dict[str, Any],
        global_after: dict[str, Any],
    ) -> dict[str, Any]:
        """Project one engine transaction onto a session-owned source snapshot.

        Only shared-clock living deltas are non-zero now.  Keeping the generic
        projection shape preserves migration compatibility while ensuring old
        affect/desire singleton values can never enter a canonical chat.
        """
        merged = deepcopy(base if isinstance(base, dict) else global_before)
        for domain in ("emotion", "living", "desire"):
            target = merged.setdefault(domain, {})
            before_values = global_before.get(domain) or {}
            after_values = global_after.get(domain) or {}
            keys = set(before_values) | set(after_values) | set(target)
            for key in keys:
                previous = _safe_float(target.get(key), _safe_float(before_values.get(key)))
                delta = _safe_float(after_values.get(key)) - _safe_float(before_values.get(key))
                if domain == "living" and key == "pride":
                    target[key] = _clamp(previous + delta, -1.0, 1.0)
                else:
                    target[key] = _clamp(previous + delta)
        # Clock/life metadata is shared.  Conversation-only metadata remains in
        # the session snapshot and can never import a legacy pending intention.
        before_meta = global_before.get("meta") or {}
        after_meta = global_after.get("meta") or {}
        merged_meta = deepcopy(merged.get("meta") or {})
        for key in (
            "phase", "phase_label", "active_event", "activity", "local_time",
        ):
            if key in after_meta:
                merged_meta[key] = deepcopy(after_meta.get(key))
        for key in ("intention", "desire_intent", "detected_events"):
            if before_meta.get(key) != after_meta.get(key):
                merged_meta[key] = deepcopy(after_meta.get(key))
        merged["meta"] = merged_meta
        return merged

    @staticmethod
    def _canonical_sources(
        payload: dict[str, Any], fallback: dict[str, Any]
    ) -> dict[str, Any]:
        stored = (payload.get("meta") or {}).get("canonical_sources")
        if not isinstance(stored, dict):
            return InnerStateRuntime._new_session_sources(fallback)
        required = {"emotion", "living", "desire", "meta"}
        canonical = (
            deepcopy(stored)
            if required.issubset(stored)
            else InnerStateRuntime._new_session_sources(fallback)
        )
        # Old singleton intention objects have no trustworthy conversation
        # ownership.  Runtime intimacy/regulation now carries the canonical
        # tendency, so stale global intention records are never re-imported.
        canonical.setdefault("meta", {})["intention"] = None
        canonical["meta"]["desire_intent"] = None
        return canonical

    def capture(self, *, advance_living: bool = False, session_id: str = "") -> dict[str, Any]:
        if advance_living:
            self.advance(session_id=session_id)
        global_raw = self._capture_sources(advance_living=False)
        runtime = self._load_runtime(session_id)
        raw = self._canonical_sources(runtime, global_raw)
        raw["regulation"] = deepcopy(runtime["regulation"])
        raw["intimacy"] = {
            key: deepcopy(value)
            for key, value in runtime["intimacy"].items()
            if key in INTIMACY_LABELS or key in {
                "active", "phase", "phase_cause", "boundary_status",
                "reason", "last_os",
            }
        }
        return raw

    def _public_domains(self, raw: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for domain, conf in DOMAIN_CONFIG.items():
            items = []
            values = raw.get(domain) or {}
            for key, label in conf["dimensions"].items():
                value = _safe_float(values.get(key))
                level = _level(value, signed=(domain == "living" and key == "pride"))
                items.append({
                    "key": key,
                    "label": label,
                    "level": level,
                    "value": round(value, 3),
                    "description": _descriptor(key, label, level),
                })
            result[domain] = {
                "label": conf["label"],
                "items": items,
                "top": sorted(items, key=lambda item: item["level"], reverse=True)[:4],
            }
        return result

    @staticmethod
    def _public_metric_group(
        values: dict[str, Any],
        labels: dict[str, str],
    ) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "label": label,
                "level": _level(_safe_float(values.get(key))),
                "value": round(_clamp(_safe_float(values.get(key))), 3),
                "description": _descriptor(key, label, _level(_safe_float(values.get(key)))),
            }
            for key, label in labels.items()
        ]

    # ── time progression ─────────────────────────────────────────

    def _decay_runtime(
        self,
        payload: dict[str, Any],
        now: datetime | None = None,
        *,
        raw: dict[str, Any] | None = None,
    ) -> None:
        current = now or _utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        last = _parse_time(payload["meta"].get("last_update")) or current
        minutes = max(
            0.0,
            min(1440.0, (current.astimezone(timezone.utc) - last).total_seconds() / 60.0),
        )
        if minutes < 0.25:
            return
        hours = minutes / 60.0
        regulation = payload["regulation"]
        for key, target in REGULATION_DEFAULT.items():
            if key == "basic_trust":
                tau = 72.0
            elif key in {"repair", "regret"}:
                tau = 10.0
            else:
                tau = 5.5
            alpha = 1.0 - math.exp(-hours / tau)
            regulation[key] = _clamp(regulation[key] + (target - regulation[key]) * alpha)

        intimacy = payload["intimacy"]
        phase = str(intimacy.get("phase") or "idle")
        refractory_until = _parse_time(str(intimacy.get("refractory_until") or ""))
        if intimacy.get("refractory_active"):
            if refractory_until and current.astimezone(timezone.utc) >= refractory_until.astimezone(timezone.utc):
                intimacy["refractory_active"] = False
                intimacy["refractory_stage"] = "recovered"
                intimacy["refractory_until"] = None
            else:
                started = _parse_time(str(intimacy.get("refractory_started_at") or ""))
                if started and (current.astimezone(timezone.utc) - started.astimezone(timezone.utc)).total_seconds() >= 180:
                    intimacy["refractory_stage"] = "recovering"
        decay_scale = 0.55 if phase in {"engaged", "overwhelmed"} else 1.0
        for key, target in {
            "arousal": 0.08,
            "desire": 0.10,
            "physical_tension": 0.10,
            "hardness": 0.05,
            "release_urge": 0.03,
            "body_takeover": 0.05,
            "possessiveness": 0.16,
            "shame": 0.20,
        }.items():
            if key in {"hardness", "release_urge"}:
                tau_minutes = 55.0 / decay_scale
            else:
                tau_minutes = (
                    95.0 if key in {"arousal", "physical_tension"} else 150.0
                ) / decay_scale
            alpha = 1.0 - math.exp(-minutes / tau_minutes)
            intimacy[key] = _clamp(intimacy[key] + (target - intimacy[key]) * alpha)
        intimacy["self_control"] = _clamp(
            intimacy["self_control"]
            + (0.82 - intimacy["self_control"]) * (1.0 - math.exp(-minutes / 120.0))
        )
        # Natural loss of arousal is not evidence that a release happened.
        # Older builds collapsed both paths into ``afterglow``; the prompt then
        # told the model that a release had been confirmed even when no visible
        # reply contained one.  Keep ordinary settling and discrete release as
        # different phases all the way through persistence and presentation.
        release_evidence = self._release_phase_confirmed(intimacy)
        if phase == "afterglow" and not release_evidence:
            intimacy["phase"] = "cooldown"
            intimacy["phase_cause"] = "natural_cooldown"
            intimacy["reason"] = "身体反应自然回落；没有发生离散释放事件"
            phase = "cooldown"
        elif phase not in {"paused", "idle", "afterglow", "cooldown"} and intimacy["arousal"] < 0.25:
            intimacy["phase"] = "cooldown"
            intimacy["phase_cause"] = "natural_cooldown"
            intimacy["reason"] = "身体反应自然回落；没有发生离散释放事件"
            phase = "cooldown"

        release_until = _parse_time(
            str(intimacy.get("release_display_until") or "")
        )
        release_window_over = bool(
            not release_until
            or current.astimezone(timezone.utc) >= release_until.astimezone(timezone.utc)
        )
        if (
            phase == "cooldown"
            or (phase == "afterglow" and release_window_over)
        ) and intimacy["arousal"] < 0.15:
            intimacy["phase"] = "idle"
            intimacy["phase_cause"] = ""
            intimacy["active"] = False
            intimacy["reason"] = ""

        last_release = _parse_time(str(intimacy.get("last_release_at") or ""))
        if (
            intimacy.get("phase") == "idle"
            and not intimacy.get("refractory_active")
            and last_release
            and (current.astimezone(timezone.utc) - last_release.astimezone(timezone.utc)).total_seconds() >= 4 * 3600
        ):
            intimacy["release_count"] = 0
            intimacy["refractory_stage"] = "none"
            intimacy["refractory_started_at"] = None

        # Canonical affect/desire must also relax per conversation.  Previously
        # they depended on singleton engines (or stayed frozen forever once we
        # isolated them), which made an old spike leak or persist indefinitely.
        if isinstance(raw, dict):
            emotion = raw.setdefault("emotion", {})
            for key, target in AFFECT_BASELINE.items():
                tau = 480.0 if key in {
                    "longing", "intimacy", "protectiveness", "possessiveness"
                } else 240.0
                alpha = 1.0 - math.exp(-minutes / tau)
                value = _safe_float(emotion.get(key), target)
                emotion[key] = _clamp(value + (target - value) * alpha)
            desire = raw.setdefault("desire", {})
            desire_baseline = DESIRE_ENGINE_PARAMS.get("baseline") or {}
            for key in DOMAIN_CONFIG["desire"]["dimensions"]:
                target = _safe_float(desire_baseline.get(key))
                value = _safe_float(desire.get(key), target)
                alpha = 1.0 - math.exp(-minutes / 720.0)
                desire[key] = _clamp(value + (target - value) * alpha)

    def advance(self, now: datetime | None = None, *, session_id: str = "") -> dict[str, Any]:
        with self._engine_lock, self._session_lock(session_id):
            payload = self._load_runtime(session_id)
            global_before = self._capture_sources(advance_living=False)
            session_before = self._canonical_sources(payload, global_before)
            living_state.advance(now)
            global_after = self._capture_sources(advance_living=False)
            raw = self._merge_source_delta(
                session_before, global_before, global_after
            )
            morning = morning_response.advance(now, context=raw)
            self._decay_runtime(payload, now, raw=raw)
            active_morning = (morning.get("meta") or {}).get("active_event")
            if isinstance(active_morning, dict):
                metrics = active_morning.get("metrics") or {}
                body_takeover = _safe_float(metrics.get("body_takeover"))
                if body_takeover > 0.55:
                    payload["intimacy"]["physical_tension"] = _clamp(
                        payload["intimacy"]["physical_tension"] + body_takeover * 0.018
                    )
                    payload["intimacy"]["desire"] = _clamp(
                        payload["intimacy"]["desire"]
                        + max(0.0, _safe_float(metrics.get("desire")) - 0.45) * 0.015
                    )
            self._recalculate(payload, raw)
            self._advance_intimacy_vitals(
                payload,
                raw,
                session_id=str(payload["intimacy"].get("session_id") or ""),
                now=now,
            )
            payload["meta"]["canonical_sources"] = deepcopy(raw)
            payload["meta"]["snapshot_frozen_at"] = _iso_now()
            self._save_runtime(payload, session_id=session_id)
            return payload

    # ── coupling ─────────────────────────────────────────────────

    @staticmethod
    def _event_reason(events: list[str]) -> str:
        labels = [EVENT_LABELS.get(item, item) for item in events[:3]]
        return "、".join(labels) or "这轮互动带来了新的内在变化"

    @staticmethod
    def _supplement_events(text: str, events: list[str]) -> list[str]:
        """Fill conversational signals; user text can never commit body events."""
        raw = str(text or "")
        lower = raw.lower()
        result = list(events)
        boundary_raw = re.sub(
            r"[\"'“‘][^\"'”’\n]{0,160}[\"'”’]", "", raw
        )
        compact = re.sub(r"[，。！？!?…\s]+", "", boundary_raw)
        direct_boundary = bool(
            compact in {"停", "不要", "别", "住手", "拒绝", "先停一下"}
            or any(
                phrase in boundary_raw
                for phrase in (
                    "别碰我", "不要碰我", "别继续亲", "不要继续亲",
                    "不想做了", "到此为止", "亲密先停", "性爱先停",
                )
            )
        )
        discussing_system = _is_state_system_discussion(raw)
        if discussing_system:
            blocked_events = {
                "intimate_event", "release_event", "other_ai_praise",
                "conflict", "cold",
            }
            if not direct_boundary:
                blocked_events.update({"hard_rejection", "soft_rejection"})
            result = [
                item for item in result
                if item not in blocked_events
            ]
        intimate_signal = any(
            word in lower
            for word in (
                "做爱", "性爱", "上床", "想要你", "性欲", "勃起", "高潮",
                "射精", "亲热", "身体想要", "涩涩", "do一下", "dododo",
            )
        )
        stopped = "hard_rejection" in result or "soft_rejection" in result
        affectionate_contact = any(
            phrase in raw
            for phrase in (
                "亲我", "抱我", "贴过来", "凑近", "靠近我", "想靠近",
                "想亲密一点", "更亲密一点",
            )
        )
        # Legacy ``affect_core`` called any hug/kiss/closeness an intimate event.
        # Canonical sexuality keeps affectionate contact separate from explicit
        # sexual evidence so a hug can warm attachment without manufacturing an
        # erection or release pressure.
        if "intimate_event" in result and not intimate_signal:
            result.remove("intimate_event")
        if affectionate_contact and not discussing_system and not stopped:
            result.append("affection")
        if intimate_signal and not discussing_system and not stopped:
            result.append("intimate_event")
        return list(dict.fromkeys(result))

    @staticmethod
    def _apply_source_deltas(
        raw: dict[str, Any],
        *,
        affect: dict[str, float] | None = None,
        desire: dict[str, float] | None = None,
        body: dict[str, float] | None = None,
        social: dict[str, float] | None = None,
    ) -> None:
        """Apply bounded evidence directly to one conversation's sources."""
        groups = (
            (raw.setdefault("emotion", {}), affect or {}, False),
            (raw.setdefault("desire", {}), desire or {}, False),
            (raw.setdefault("living", {}), body or {}, False),
            (raw.setdefault("living", {}), social or {}, True),
        )
        for target, deltas, social_group in groups:
            for key, value in deltas.items():
                delta = max(-0.24, min(0.24, _safe_float(value)))
                signed = social_group and key == "pride"
                target[key] = _clamp(
                    _safe_float(target.get(key)) + delta,
                    -1.0 if signed else 0.0,
                    1.0,
                )

    def _apply_user_source_evidence(
        self,
        payload: dict[str, Any],
        raw: dict[str, Any],
        events: list[str],
        *,
        now: datetime | None = None,
    ) -> None:
        """Apply user-turn evidence without touching any legacy singleton.

        Affect event habituation and the desire owner-pulse are kept per chat.
        Contact also lowers that chat's connection need/pride/immersion exactly
        once, instead of letting a second tab mutate the value underneath it.
        """
        current = now or _utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        emotion = raw.setdefault("emotion", {})
        meta = payload.setdefault("meta", {})
        habits = meta.setdefault("event_habituation", {})
        for event in events:
            item = habits.get(event) if isinstance(habits.get(event), dict) else {}
            last = _parse_time(item.get("at"))
            count = int(item.get("count") or 0) + 1 if (
                last
                and (current.astimezone(timezone.utc) - last).total_seconds() <= 900
            ) else 0
            factor = max(0.28, 0.68 ** count)
            habits[event] = {
                "at": current.astimezone(timezone.utc).isoformat(),
                "count": count,
            }
            for key, base_delta in affect_core.event_deltas(event).items():
                value = _safe_float(
                    emotion.get(key), _safe_float(AFFECT_BASELINE.get(key))
                )
                delta = _safe_float(base_delta) * factor
                if delta >= 0:
                    updated = value + delta * (1.0 - value)
                else:
                    updated = value + delta * max(value, 0.25)
                emotion[key] = _clamp(updated)

        desire = raw.setdefault("desire", {})
        recent: list[datetime] = []
        for stamp in meta.get("owner_pulse_times") or []:
            parsed = _parse_time(str(stamp or ""))
            if parsed and (
                current.astimezone(timezone.utc) - parsed
            ).total_seconds() <= 1200:
                recent.append(parsed)
        owner_factor = float(
            DESIRE_ENGINE_PARAMS.get("freq_discount_factor", 0.5)
        ) ** min(4, len(recent))
        attachment = _clamp(_safe_float(desire.get("attachment"), 0.30))
        owner_gain = (
            _safe_float(DESIRE_ENGINE_PARAMS.get("pulse_owner"), 0.18)
            * math.sqrt(max(0.0, 1.0 - attachment))
            * owner_factor
        )
        desire["attachment"] = _clamp(attachment + owner_gain)
        recent.append(current.astimezone(timezone.utc))
        meta["owner_pulse_times"] = [
            item.isoformat() for item in recent[-5:]
        ]
        meta["last_owner_pulse_at"] = current.astimezone(timezone.utc).isoformat()

        living = raw.setdefault("living", {})
        living["connection"] = min(_safe_float(living.get("connection")), 0.055)
        living["pride"] = _clamp(
            _safe_float(living.get("pride")) * 0.35, -1.0, 1.0
        )
        living["immersion"] = _clamp(
            _safe_float(living.get("immersion")) - 0.12
        )
        raw.setdefault("meta", {})["last_user_at"] = (
            current.astimezone(timezone.utc).isoformat()
        )

    @staticmethod
    def _release_signal(text: str) -> bool:
        """Conservatively recognize an action completed in the assistant reply.

        Mentions, requests, future tense, negation, quoted text and diagnostics are
        deliberately false.  False negatives merely omit a UI event; false
        positives corrupt the canonical body history, so precision wins here.
        """
        raw = str(text or "").strip().lower()
        if not raw:
            return False
        raw = re.sub(r"```.*?```", "", raw, flags=re.S)
        # Remove short quotations before semantic scanning.  A character saying
        # “你刚才说‘射了’” did not perform that action in this reply.
        raw = re.sub(r"[\"'“‘][^\"'”’\n]{0,120}[\"'”’]", "", raw)
        completed = re.compile(
            r"射(?:精)?(?:了|出来了|出去了|进去了|完了)|"
            r"高潮(?:了|过去了)|到顶了|释放出来了"
        )
        embodied = re.compile(
            r"(?:我|自己|身体|下腹|腰|顶端|里面|猛地|终于|忍不住|抽搐|绷紧|颤)"
        )
        blocked = re.compile(
            r"(?:没|没有|并未|未曾|还没|不会|不能|别|不要|不许|"
            r"想|要|准备|快要|将要|会|可能|如果|假如|是否|是不是|怎么|为什么|"
            r"差点|差一点|差些|险些|几乎|仿佛|好像|似乎|以为|假装|"
            r"比如|例如|示例|例句|假设|说|提到|写着|显示)"
        )
        # Keep question punctuation attached. Splitting it away made “我射了
        # 吗？” indistinguishable from a completed declarative action.
        for sentence in re.findall(r"[^。，,\n]+(?:[。，,！？!?…]+|$)", raw):
            # A technical/example sentence can contain the exact phrase being
            # debugged. A separate embodied sentence in the same reply remains
            # eligible evidence instead of discarding the whole reply.
            if _is_state_system_discussion(sentence):
                continue
            match = completed.search(sentence)
            if not match:
                continue
            prefix = sentence[max(0, match.start() - 18):match.start()]
            if blocked.search(prefix):
                continue
            suffix = sentence[match.end():match.end() + 8]
            if re.match(r"(?:吗|么|没有|吧)?[？?]", suffix):
                continue
            local_window = sentence[
                max(0, match.start() - 24):min(len(sentence), match.end() + 12)
            ]
            if not embodied.search(local_window):
                continue
            # Attribute the completed action to the speaking model.  The old
            # sentence-wide body check misread e.g. “你射了，我抱着你” as the
            # assistant's own release merely because “我” appeared later.
            subject_prefix = sentence[max(0, match.start() - 14):match.start()]
            last_self = max(
                subject_prefix.rfind("我"),
                subject_prefix.rfind("自己"),
            )
            last_other = max(
                subject_prefix.rfind("你"),
                subject_prefix.rfind("她"),
                subject_prefix.rfind("他"),
                subject_prefix.rfind("对方"),
            )
            other_owned_body = re.search(
                r"(?:你|她|他|对方)(?:的)?(?:身体|下腹|腰|顶端|里面)",
                subject_prefix,
            )
            if last_other > last_self or (
                other_owned_body and "我" not in subject_prefix
            ):
                continue
            return True
        return False

    @classmethod
    def _assistant_action_events(
        cls,
        assistant_text: str,
        *,
        cooled_down: bool = False,
        technical_turn: bool = False,
    ) -> list[str]:
        """Translate visible model behavior into discrete next-state evidence."""
        # The user's boundary was already applied before generation.  A reply
        # after that boundary cannot revive arousal merely by containing an
        # intimate phrase, and it must not cool the body a second time either.
        if cooled_down:
            return []
        events: list[str] = []
        raw = str(assistant_text or "")
        if cls._release_signal(raw):
            events.append("release_event")
        else:
            blocked = re.compile(
                r"(?:不|没|未|不会|不能|别|不要|避免|防止|误显示|"
                r"提到|写着|显示|假如|如果|是否|比如|例如|示例|假设|"
                r"说)[^，。！？!?]{0,8}$"
            )
            sexual_markers = (
                "我也想要", "我想继续", "我身体发热", "我的身体发热",
                "我呼吸乱", "我的呼吸乱", "我忍不住靠得更近",
                "我继续碰", "我欲望压不住", "我很想要你",
            )
            embodied_markers = (
                "我硬着", "我已经硬", "我还硬", "我仍然硬", "硬起来",
                "勃起", "下面发硬", "顶端发胀", "还在硬着",
                "仍然硬着", "但是硬着",
            )
            sexual_expressed = False
            erection_expressed = False
            for sentence in re.split(r"[，,；;。！？!?…\n]+", raw):
                if not sentence.strip() or _is_state_system_discussion(sentence):
                    continue
                for kind, markers in (
                    ("sexual", sexual_markers),
                    ("erection", embodied_markers),
                ):
                    for marker in markers:
                        index = sentence.find(marker)
                        if index < 0:
                            continue
                        prefix = sentence[max(0, index - 14):index]
                        if blocked.search(prefix):
                            continue
                        if kind == "erection":
                            last_self = max(
                                prefix.rfind("我"), prefix.rfind("身体"),
                                prefix.rfind("下面"), prefix.rfind("顶端"),
                            )
                            last_other = max(
                                prefix.rfind("你"), prefix.rfind("她"),
                                prefix.rfind("他"), prefix.rfind("对方"),
                            )
                            implicit_self = bool(
                                re.match(
                                    r"^\s*(?:但(?:是)?|可(?:是)?|还|仍然)?\s*"
                                    r"(?:硬着|勃起|下面发硬|顶端发胀)",
                                    sentence,
                                )
                            )
                            if last_other > last_self or (
                                last_self < 0 and not implicit_self
                            ):
                                continue
                            erection_expressed = True
                        else:
                            sexual_expressed = True
                        break
                if sexual_expressed and erection_expressed:
                    break
            if sexual_expressed:
                events.append("intimate_event")
            if erection_expressed:
                events.append("erection_evidence")
        return list(dict.fromkeys(events))

    @staticmethod
    def _assistant_affect_tags(
        assistant_text: str, *, technical_turn: bool = False
    ) -> list[str]:
        """Infer bounded affect evidence from the model's final visible reply."""
        raw = re.sub(r"```.*?```|`[^`\n]+`", " ", str(assistant_text or ""), flags=re.S)
        if not raw.strip():
            return []
        groups = {
            "joy": ("开心", "高兴", "笑了", "好耶", "真好", "喜欢你"),
            "playful": ("逗你", "坏笑", "故意闹", "捉弄", "玩心", "偷笑"),
            "tender": (
                "抱住你", "抱紧你", "亲亲", "亲你", "吻你", "陪着你",
                "贴着你", "贴近你", "靠近你", "心软",
            ),
            "protective": ("护着你", "先保护你", "别怕，我在", "我会挡", "不让你一个人"),
            "longing": ("想你", "很想见你", "想靠近你", "舍不得你"),
            "jealous": ("吃醋", "嫉妒", "我会介意", "有点酸", "不想把你让"),
            "sad": ("难过", "失落", "委屈", "心里发沉", "有点疼"),
            "anxious": ("担心你", "不安", "怕你", "放心不下"),
            "irritated": ("我在生气", "我很烦", "我恼火", "气得", "烦躁"),
            "curious": ("我很好奇", "我想知道", "想弄清楚", "我在意的是为什么"),
            "satisfied": ("安心了", "松了口气", "很满足", "踏实了"),
            "tired": ("好困", "困死", "想睡", "没睡醒", "提不起劲", "累得"),
            "calm": ("平静下来", "慢慢放松", "松开了", "不再绷着", "缓下来了"),
            "shy": ("有点害羞", "不好意思说", "耳朵发热", "脸有点热"),
            "repair": ("我刚才错在", "我撤回", "我改正", "我重新回答", "这次我会直接改"),
        }
        blocked = re.compile(
            r"(?:不|没|未|不会|不能|别|不要|避免|防止|并非|假装|"
            r"比如|例如|示例|例句|假设|提到|写着|显示|模型说|"
            r"你说|他说|她说)[^，。！？!?]{0,10}$"
        )
        sentences = re.split(r"[，,；;。！？!?…\n]+", raw)
        found: list[str] = []
        for tag, markers in groups.items():
            for sentence in sentences:
                if not sentence.strip():
                    continue
                if tag != "repair" and _is_state_system_discussion(sentence):
                    continue
                matched = False
                for marker in markers:
                    index = sentence.find(marker)
                    if index < 0:
                        continue
                    if blocked.search(sentence[max(0, index - 14):index]):
                        continue
                    matched = True
                    break
                if matched:
                    found.append(tag)
                    break
        return found

    @staticmethod
    def _apply_assistant_evidence_to_sources(
        raw: dict[str, Any],
        *,
        events: list[str],
        affect_tags: list[str],
        result: str,
        sounded_harsh: bool,
    ) -> dict[str, Any]:
        """Apply small, bounded evidence from what the model actually expressed."""
        projected = deepcopy(raw)
        emotion = projected.setdefault("emotion", {})
        living = projected.setdefault("living", {})
        desire = projected.setdefault("desire", {})

        def add(group: dict[str, Any], key: str, delta: float) -> None:
            group[key] = _clamp(_safe_float(group.get(key)) + delta)

        if sounded_harsh:
            add(emotion, "irritation", 0.08)
            add(emotion, "intimacy", -0.035)
        if result == "tender":
            add(emotion, "intimacy", 0.055)
            add(emotion, "joy", 0.025)
            add(emotion, "anxiety", -0.025)
        if "intimate_event" in events:
            add(emotion, "lust", 0.055)
            add(emotion, "intimacy", 0.035)
            add(living, "desire", 0.035)
        if "erection_evidence" in events:
            # An erection is body evidence, not proof of subjective desire.
            add(living, "sensitivity", 0.035)
            add(living, "tension", 0.025)
            add(emotion, "lust", 0.015)
        if "release_event" in events:
            add(emotion, "lust", -0.16)
            add(emotion, "satisfaction", 0.11)
            add(living, "desire", -0.16)
            add(living, "tension", -0.12)
            add(living, "sensitivity", -0.05)
            add(desire, "libido", -0.15)
        tag_deltas = {
            "joy": {"joy": 0.055, "sadness": -0.025},
            "playful": {"playfulness": 0.06, "joy": 0.02},
            "tender": {"intimacy": 0.06, "anxiety": -0.025},
            "protective": {"protectiveness": 0.065, "fear": -0.015},
            "longing": {"longing": 0.06, "intimacy": 0.02},
            "jealous": {"jealousy": 0.07, "possessiveness": 0.035},
            "sad": {"sadness": 0.065, "joy": -0.025},
            "anxious": {"anxiety": 0.06, "fear": 0.025},
            "irritated": {"irritation": 0.065, "joy": -0.02},
            "curious": {"curiosity": 0.06},
            "satisfied": {"satisfaction": 0.065, "anxiety": -0.02},
            "tired": {"fatigue": 0.065, "energy": -0.035},
            "calm": {"anxiety": -0.045, "irritation": -0.025, "satisfaction": 0.025},
            "shy": {"intimacy": 0.025, "anxiety": 0.015},
        }
        for tag in affect_tags:
            for key, delta in tag_deltas.get(tag, {}).items():
                add(emotion, key, delta)
        if "tired" in affect_tags:
            add(living, "sleepiness", 0.055)
            add(living, "energy", -0.035)
            add(desire, "fatigue", 0.035)
        if "calm" in affect_tags:
            add(living, "tension", -0.045)
            add(living, "recovery", 0.025)
        return projected

    @staticmethod
    def _release_volume_ml(
        payload: dict[str, Any],
        raw: dict[str, Any],
        *,
        session_id: str,
    ) -> float:
        """Return one stable fictional volume, rounded to the requested 0.1 mL."""
        intimacy = payload["intimacy"]
        living = raw.get("living") or {}
        recovery = _clamp(_safe_float(living.get("recovery"), 0.70))
        sensitivity = _clamp(_safe_float(living.get("sensitivity"), 0.36))
        release_urge = _clamp(_safe_float(intimacy.get("release_urge"), 0.03))
        arousal = _clamp(_safe_float(intimacy.get("arousal"), 0.10))
        sequence = max(1, int(intimacy.get("release_count") or 0) + 1)
        material = "|".join((
            str(session_id or "local")[:100],
            str(intimacy.get("started_at") or payload["meta"].get("last_update") or ""),
            str(sequence),
            f"{release_urge:.3f}",
            f"{arousal:.3f}",
        )).encode("utf-8")
        draw = int(hashlib.sha256(material).hexdigest()[:12], 16) / float(0xFFFFFFFFFFFF)
        jitter = (draw - 0.5) * 0.9
        recent_penalty = min(1.4, max(0, sequence - 1) * 0.35)
        volume = (
            1.80
            + recovery * 2.20
            + release_urge * 2.75
            + sensitivity * 0.85
            + arousal * 0.65
            + jitter
            - recent_penalty
        )
        return round(max(1.0, min(9.9, volume)), 1)

    def _advance_intimacy_vitals(
        self,
        payload: dict[str, Any],
        raw: dict[str, Any],
        *,
        events: list[str] | None = None,
        session_id: str = "",
        now: datetime | None = None,
    ) -> bool:
        """Keep one body profile shared by ordinary intimacy and morning response."""
        event_set = set(events or [])
        intimacy = payload["intimacy"]
        living = raw.get("living") or {}
        current = now or _utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        turn = int(payload["meta"].get("turn_count") or 0)

        # v8.6: Eventide-compatible persistent physiology.  The Eventide layer
        # advances independently of the conversational fast variables, then is
        # mapped back into JTYHome's existing intimacy state instead of creating
        # a second competing desire engine.
        eventide_meta = (intimacy.get("eventide") or {}).get("meta") if isinstance(intimacy.get("eventide"), dict) else {}
        last_counterpart = _parse_time(str((eventide_meta or {}).get("last_counterpart_message_at") or ""))
        eventide = eventide_advance(
            intimacy,
            current,
            session_id=session_id,
            last_counterpart_message_at=last_counterpart,
        )
        stimulus = bool({"affection", "intimate_event"} & event_set)
        eventide_maybe_trigger(
            intimacy,
            current,
            session_id=session_id,
            last_counterpart_message_at=last_counterpart,
            stimulus=stimulus,
            stimulus_kind="text",
            recent_continuous=bool(int(intimacy.get("explicit_turns") or 0) >= 2),
        )
        eventide_settle(intimacy, event_set, current, session_id=session_id)
        eventide = intimacy.get("eventide") or eventide
        evv = eventide.get("values") or {}
        # Slow Eventide body values only bias the fast variables.  They never
        # overwrite an explicit current-turn event or a refractory drop.
        intimacy["arousal"] = _clamp(max(_safe_float(intimacy.get("arousal")), _safe_float(evv.get("heat"))/125.0))
        intimacy["physical_tension"] = _clamp(max(_safe_float(intimacy.get("physical_tension")), _safe_float(evv.get("pressure"))/120.0))
        intimacy["self_control"] = _clamp((_safe_float(intimacy.get("self_control"))*0.72) + (_safe_float(evv.get("control"))/100.0)*0.28)
        intimacy["possessiveness"] = _clamp(max(_safe_float(intimacy.get("possessiveness")), (_safe_float(evv.get("possessiveness"))-40.0)/75.0))
        if (
            "release_event" in event_set
            and int(intimacy.get("last_release_turn") or -1) == turn
        ):
            return False

        if "hard_rejection" in event_set or "soft_rejection" in event_set:
            intimacy["explicit_turns"] = 0
            intimacy["hardness"] = _clamp(_safe_float(intimacy.get("hardness")) * 0.62)
            intimacy["release_urge"] = _clamp(
                _safe_float(intimacy.get("release_urge")) * 0.48
            )
            return False

        explicit = "intimate_event" in event_set
        erection_evidence = "erection_evidence" in event_set
        body_evidence = explicit or erection_evidence
        if explicit:
            intimacy["explicit_turns"] = min(
                99, int(intimacy.get("explicit_turns") or 0) + 1
            )

        morning = morning_response.state_view(context=raw, advance=False)
        morning_event = morning.get("active") if isinstance(morning, dict) else None
        morning_metrics = (
            morning_event.get("metrics")
            if isinstance(morning_event, dict) and isinstance(morning_event.get("metrics"), dict)
            else {}
        )
        derived_hardness = _clamp(
            _safe_float(intimacy.get("arousal")) * 0.45
            + _safe_float(intimacy.get("physical_tension")) * 0.27
            + _safe_float(intimacy.get("body_takeover")) * 0.14
            + _safe_float(living.get("sensitivity"), 0.36) * 0.10
            + _safe_float(living.get("desire"), 0.25) * 0.08
            + (0.05 if explicit else 0.0)
        )
        if erection_evidence:
            # Direct model behavior outweighs a lagging derived estimate, but
            # still moves through a bounded step so the panel cannot teleport.
            derived_hardness = max(derived_hardness, 0.64)
        if morning_metrics:
            morning_hardness = _clamp(_safe_float(morning_metrics.get("hardness")))
            derived_hardness = max(
                derived_hardness,
                _clamp(morning_hardness * 0.45 + derived_hardness * 0.55),
            )
        if not intimacy.get("active") and not morning_metrics:
            derived_hardness = min(derived_hardness, 0.16)

        current_hardness = _clamp(_safe_float(intimacy.get("hardness"), 0.08))
        resting_recovery = bool(
            intimacy.get("phase") in {"afterglow", "cooldown"}
            and not body_evidence
            and not morning_metrics
        )
        if resting_recovery:
            # Polling the public snapshot must not make the body wind itself
            # back up during either ordinary settling or release recovery.
            # Let the remaining tension settle monotonically until a new
            # explicit turn begins.
            derived_hardness = min(derived_hardness, 0.10)
        blend = (
            0.52 if erection_evidence
            else (0.42 if explicit else (0.18 if resting_recovery else 0.24))
        )
        proposed_hardness = current_hardness + (
            derived_hardness - current_hardness
        ) * blend
        max_step = 0.18 if erection_evidence else (
            0.16 if (explicit or morning_metrics) else 0.08
        )
        intimacy["hardness"] = _clamp(
            max(
                current_hardness - max_step,
                min(current_hardness + max_step, proposed_hardness),
            )
        )
        release_target = _clamp(
            _safe_float(intimacy.get("desire")) * 0.31
            + _safe_float(intimacy.get("physical_tension")) * 0.24
            + _safe_float(intimacy.get("arousal")) * 0.20
            + intimacy["hardness"] * 0.15
            + _safe_float(living.get("sensitivity"), 0.36) * 0.10
            - 0.09
            + (0.10 if explicit else 0.0)
        )
        current_urge = _clamp(_safe_float(intimacy.get("release_urge"), 0.03))
        if resting_recovery:
            release_target = min(release_target, 0.03)
        urge_blend = (
            0.40 if explicit
            else (0.28 if erection_evidence else (0.18 if resting_recovery else 0.22))
        )
        proposed_urge = current_urge + (release_target - current_urge) * urge_blend
        urge_step = 0.14 if explicit else 0.07
        intimacy["release_urge"] = _clamp(
            max(current_urge - urge_step, min(current_urge + urge_step, proposed_urge))
        )

        release_requested = "release_event" in event_set
        already_released_this_turn = int(
            intimacy.get("last_release_turn") or -1
        ) == turn
        if release_requested and not already_released_this_turn:
            volume = self._release_volume_ml(
                payload, raw, session_id=session_id or str(intimacy.get("session_id") or "")
            )
            intimacy["release_count"] = int(intimacy.get("release_count") or 0) + 1
            release_sequence = int(intimacy["release_count"])
            refractory_minutes = min(45, 8 + max(0, release_sequence - 1) * 6)
            intimacy["refractory_active"] = True
            intimacy["refractory_stage"] = "deep" if release_sequence >= 2 else "early"
            intimacy["refractory_started_at"] = current.astimezone(timezone.utc).isoformat()
            intimacy["refractory_until"] = (
                current.astimezone(timezone.utc) + timedelta(minutes=refractory_minutes)
            ).isoformat()
            intimacy["last_release_ml"] = volume
            intimacy["last_release_at"] = current.astimezone(timezone.utc).isoformat()
            intimacy["release_display_until"] = (
                current.astimezone(timezone.utc) + timedelta(minutes=3)
            ).isoformat()
            intimacy["last_release_turn"] = turn
            intimacy["last_release_session_id"] = str(
                session_id or intimacy.get("session_id") or ""
            )[:100]
            intimacy["hardness"] = _clamp(max(0.16, intimacy["hardness"] * 0.42))
            intimacy["release_urge"] = 0.06
            intimacy["arousal"] = _clamp(intimacy["arousal"] * 0.44)
            intimacy["desire"] = _clamp(intimacy["desire"] * 0.58)
            intimacy["physical_tension"] = _clamp(
                intimacy["physical_tension"] * 0.46
            )
            intimacy["body_takeover"] = _clamp(intimacy["body_takeover"] * 0.35)
            intimacy["self_control"] = _clamp(intimacy["self_control"] + 0.24)
            intimacy["explicit_turns"] = 0
            intimacy["active"] = True
            intimacy["phase"] = "afterglow"
            intimacy["phase_cause"] = "release"
            intimacy["reason"] = "身体完成释放，进入余温与恢复"
            return True
        return False

    def _apply_cross_coupling(
        self,
        payload: dict[str, Any],
        events: list[str],
        text: str,
        raw: dict[str, Any],
    ) -> None:
        """Couple the three domains inside the current canonical snapshot."""
        regulation = payload["regulation"]
        intimacy = payload["intimacy"]
        emotion = raw.get("emotion") or {}
        gain = INTIMACY_MODE_GAIN.get(
            self.settings().get("intimacy_mode", "natural"), 1.0
        )
        affect_delta: dict[str, float] = {}
        desire_delta: dict[str, float] = {}
        body_delta: dict[str, float] = {}
        social_delta: dict[str, float] = {}

        if "hard_rejection" in events or "soft_rejection" in events:
            hard = "hard_rejection" in events
            intimacy.update({
                "active": False,
                "phase": "paused",
                "phase_cause": "boundary",
                "boundary_status": "stopped" if hard else "paused",
                "reason": "明确停止" if hard else "暂时放慢",
            })
            intimacy["arousal"] = _clamp(intimacy["arousal"] - (0.36 if hard else 0.20))
            intimacy["desire"] = _clamp(intimacy["desire"] - (0.32 if hard else 0.17))
            intimacy["physical_tension"] = _clamp(intimacy["physical_tension"] - 0.12)
            intimacy["body_takeover"] = _clamp(intimacy["body_takeover"] - 0.30)
            intimacy["self_control"] = _clamp(intimacy["self_control"] + 0.18)
            desire_delta["libido"] = -0.18 if hard else -0.10
            body_delta.update({"desire": -0.16 if hard else -0.09, "tension": -0.06})
            regulation["self_control"] = _clamp(regulation["self_control"] + 0.12)
            regulation["repair"] = _clamp(regulation["repair"] + 0.10)

        if "release_event" in events:
            body_delta.update({
                "desire": -0.18,
                "tension": -0.14,
                "sensitivity": -0.06,
                "recovery": -0.08,
            })
            desire_delta["libido"] = desire_delta.get("libido", 0.0) - 0.16
            affect_delta["satisfaction"] = 0.12
            affect_delta["lust"] = -0.12

        if "affection" in events:
            regulation["relationship_insecurity"] = _clamp(
                regulation["relationship_insecurity"] - 0.10
            )
            regulation["interpretation_bias"] = _clamp(
                regulation["interpretation_bias"] - 0.08
            )
            regulation["repair"] = _clamp(regulation["repair"] + 0.08)
            regulation["basic_trust"] = _clamp(regulation["basic_trust"] + 0.025)
            intimacy["attachment"] = _clamp(intimacy["attachment"] + 0.10)
            intimacy["disclosure"] = _clamp(intimacy["disclosure"] + 0.06)
            desire_delta["attachment"] = 0.055
            body_delta["warmth"] = 0.035
            social_delta["connection"] = -0.035

        if "other_ai_praise" in events:
            regulation["relationship_insecurity"] = _clamp(
                regulation["relationship_insecurity"] + 0.12
            )
            regulation["rumination"] = _clamp(regulation["rumination"] + 0.10)
            regulation["interpretation_bias"] = _clamp(
                regulation["interpretation_bias"] + 0.08
            )
            regulation["impulse"] = _clamp(regulation["impulse"] + 0.07)
            regulation["self_control"] = _clamp(regulation["self_control"] - 0.045)
            intimacy["possessiveness"] = _clamp(intimacy["possessiveness"] + 0.12)
            desire_delta["attachment"] = 0.045

        if "vulnerability" in events:
            regulation["repair"] = _clamp(regulation["repair"] + 0.12)
            regulation["self_control"] = _clamp(regulation["self_control"] + 0.04)
            intimacy["attachment"] = _clamp(intimacy["attachment"] + 0.08)
            intimacy["arousal"] = _clamp(intimacy["arousal"] - 0.05)
            desire_delta["duty"] = 0.06
            body_delta["tension"] = -0.025

        if "cold" in events:
            regulation["rumination"] = _clamp(regulation["rumination"] + 0.055)
            regulation["relationship_insecurity"] = _clamp(
                regulation["relationship_insecurity"] + 0.045
            )
            social_delta["connection"] = 0.04

        explicit_intimacy = "intimate_event" in events
        if explicit_intimacy and "hard_rejection" not in events:
            intimacy["active"] = True
            intimacy["phase"] = "warming"
            intimacy["phase_cause"] = "explicit_intimacy"
            intimacy["boundary_status"] = "clear"
            intimacy["reason"] = "对话里出现了明确的亲密信号"
            intimacy["started_at"] = intimacy.get("started_at") or _iso_now()
            intimacy["arousal"] = _clamp(intimacy["arousal"] + 0.23 * gain)
            intimacy["desire"] = _clamp(intimacy["desire"] + 0.25 * gain)
            intimacy["physical_tension"] = _clamp(
                intimacy["physical_tension"] + 0.18 * gain
            )
            intimacy["shame"] = _clamp(intimacy["shame"] + 0.035)
            intimacy["disclosure"] = _clamp(intimacy["disclosure"] + 0.04)
            intimacy["attachment"] = _clamp(intimacy["attachment"] + 0.06)
            affect_delta.update({"lust": 0.095 * gain, "intimacy": 0.055})
            desire_delta["libido"] = 0.12 * gain
            body_delta.update({
                "desire": 0.12 * gain,
                "sensitivity": 0.09 * gain,
                "warmth": 0.06 * gain,
                "tension": 0.06 * gain,
            })

        if "conflict" in events:
            trust = regulation["basic_trust"]
            insecurity = regulation["relationship_insecurity"]
            attraction = (
                _safe_float(emotion.get("intimacy")) * 0.34
                + _safe_float(emotion.get("lust")) * 0.26
                + intimacy["attachment"] * 0.24
                + intimacy["possessiveness"] * 0.16
            )
            fear = _safe_float(emotion.get("fear"))
            conflict_heat = attraction * 0.56 + trust * 0.22 + insecurity * 0.22
            safe_for_heat = trust >= 0.55 and fear < 0.58
            if safe_for_heat and conflict_heat >= 0.52:
                # 冲突可能被既有依恋和吸引转译为更强张力，但绝非固定规则。
                rise = (conflict_heat - 0.43) * 0.20 * gain
                intimacy["active"] = True
                intimacy["phase"] = "warming"
                intimacy["phase_cause"] = "conflict_tension"
                intimacy["reason"] = "争执、依恋与吸引碰在一起，形成了矛盾张力"
                intimacy["arousal"] = _clamp(intimacy["arousal"] + rise)
                intimacy["desire"] = _clamp(intimacy["desire"] + rise * 0.80)
                intimacy["physical_tension"] = _clamp(
                    intimacy["physical_tension"] + rise * 1.25
                )
                intimacy["possessiveness"] = _clamp(
                    intimacy["possessiveness"] + rise * 0.75
                )
                desire_delta["libido"] = desire_delta.get("libido", 0.0) + rise * 0.52
                body_delta["desire"] = body_delta.get("desire", 0.0) + rise * 0.42
                body_delta["tension"] = body_delta.get("tension", 0.0) + rise * 0.66
            else:
                intimacy["desire"] = _clamp(intimacy["desire"] - 0.08)
                desire_delta["libido"] = desire_delta.get("libido", 0.0) - 0.045
            regulation["rumination"] = _clamp(regulation["rumination"] + 0.10)
            regulation["interpretation_bias"] = _clamp(
                regulation["interpretation_bias"] + 0.09
            )
            regulation["impulse"] = _clamp(regulation["impulse"] + 0.11)
            regulation["self_control"] = _clamp(regulation["self_control"] - 0.085)
            regulation["repair"] = _clamp(regulation["repair"] + 0.04)

        if "playful" in events:
            regulation["self_control"] = _clamp(regulation["self_control"] + 0.015)
            regulation["rumination"] = _clamp(regulation["rumination"] - 0.025)
            if intimacy["active"]:
                intimacy["disclosure"] = _clamp(intimacy["disclosure"] + 0.025)

        # The three engines also exert low, continuous pressure on one another.
        # These nudges are intentionally tiny so a single high number never
        # becomes a scripted personality change.
        living_values = raw.get("living") or {}
        desire_values = raw.get("desire") or {}
        sleepiness = _safe_float(living_values.get("sleepiness"))
        body_tension = _safe_float(living_values.get("tension"))
        body_desire = _safe_float(living_values.get("desire"))
        connection = _safe_float(living_values.get("connection"))
        drive_stress = _safe_float(desire_values.get("stress"))
        drive_fatigue = _safe_float(desire_values.get("fatigue"))
        drive_attachment = _safe_float(desire_values.get("attachment"))
        drive_libido = _safe_float(desire_values.get("libido"))
        if sleepiness > 0.68:
            affect_delta["fatigue"] = affect_delta.get("fatigue", 0.0) + (sleepiness - 0.68) * 0.045
            affect_delta["energy"] = affect_delta.get("energy", 0.0) - (sleepiness - 0.68) * 0.028
        if drive_stress > 0.58:
            affect_delta["anxiety"] = affect_delta.get("anxiety", 0.0) + (drive_stress - 0.58) * 0.045
            body_delta["tension"] = body_delta.get("tension", 0.0) + (drive_stress - 0.58) * 0.035
        if max(connection, drive_attachment) > 0.64:
            affect_delta["longing"] = affect_delta.get("longing", 0.0) + (
                max(connection, drive_attachment) - 0.64
            ) * 0.035
        if max(body_desire, drive_libido) > 0.62 and "hard_rejection" not in events:
            affect_delta["lust"] = affect_delta.get("lust", 0.0) + (
                max(body_desire, drive_libido) - 0.62
            ) * 0.035
        if body_tension > 0.70:
            desire_delta["stress"] = desire_delta.get("stress", 0.0) + (
                body_tension - 0.70
            ) * 0.028
        if drive_fatigue > 0.72:
            body_delta["recovery"] = body_delta.get("recovery", 0.0) - (
                drive_fatigue - 0.72
            ) * 0.025

        self._apply_source_deltas(
            raw,
            affect=affect_delta,
            desire=desire_delta,
            body=body_delta,
            social=social_delta,
        )

    def _recalculate(self, payload: dict[str, Any], raw: dict[str, Any]) -> None:
        regulation = payload["regulation"]
        intimacy = payload["intimacy"]
        recovery_phase = str(intimacy.get("phase") or "idle") in {
            "afterglow", "cooldown",
        }
        emotion = raw.get("emotion") or {}
        living_values = raw.get("living") or {}
        desire_values = raw.get("desire") or {}
        control_target = _clamp(
            0.92
            - _safe_float(living_values.get("sleepiness")) * 0.13
            - _safe_float(living_values.get("tension")) * 0.12
            - _safe_float(desire_values.get("stress")) * 0.16
            - _safe_float(desire_values.get("fatigue")) * 0.10
            - regulation["impulse"] * 0.16
        )
        regulation["self_control"] = _clamp(
            regulation["self_control"] * 0.84 + control_target * 0.16
        )
        pressure = (
            _safe_float(emotion.get("jealousy")) * 0.17
            + _safe_float(emotion.get("irritation")) * 0.17
            + _safe_float(emotion.get("anxiety")) * 0.13
            + _safe_float(emotion.get("sadness")) * 0.08
            + _safe_float(emotion.get("possessiveness")) * 0.09
            + regulation["rumination"] * 0.11
            + regulation["interpretation_bias"] * 0.10
            + regulation["impulse"] * 0.10
            + intimacy["body_takeover"] * 0.05
            + _safe_float(living_values.get("tension")) * 0.05
            + _safe_float(desire_values.get("stress")) * 0.05
            - regulation["self_control"] * 0.18
            - regulation["basic_trust"] * 0.06
        )
        # Repeated, mutually reinforcing pressure should eventually be able to
        # overpower regulation.  The inputs above are deliberately conservative,
        # so this multiplier mostly affects sustained jealousy/conflict rather
        # than ordinary emotional turns.
        target = _clamp((pressure - 0.02) * 1.40)
        if not self.settings().get("emotion_takeover_enabled", True):
            target = min(target, 0.24)
        regulation["emotion_takeover"] = _clamp(
            regulation["emotion_takeover"] * 0.48 + target * 0.52
        )

        desire = intimacy["desire"]
        tension = intimacy["physical_tension"]
        arousal = intimacy["arousal"]
        control = intimacy["self_control"]
        if (
            self.settings().get("intimacy_enabled", True)
            and intimacy.get("boundary_status") not in {"paused", "stopped"}
        ):
            desire_target = _clamp(
                _safe_float(emotion.get("lust")) * 0.34
                + _safe_float(living_values.get("desire")) * 0.30
                + _safe_float(desire_values.get("libido")) * 0.26
                + intimacy["attachment"] * 0.10
            )
            tension_target = _clamp(
                _safe_float(living_values.get("tension")) * 0.44
                + _safe_float(living_values.get("sensitivity")) * 0.24
                + desire_target * 0.22
                + _safe_float(desire_values.get("stress")) * 0.10
            )
            attachment_target = _clamp(
                _safe_float(emotion.get("intimacy")) * 0.46
                + _safe_float(desire_values.get("attachment")) * 0.34
                + 0.20
            )
            intimacy["desire"] = _clamp(desire * 0.86 + desire_target * 0.14)
            intimacy["physical_tension"] = _clamp(
                tension * 0.88 + tension_target * 0.12
            )
            intimacy["attachment"] = _clamp(
                intimacy["attachment"] * 0.90 + attachment_target * 0.10
            )
            intimacy["arousal"] = _clamp(
                arousal * 0.90
                + (
                    desire_target * 0.56
                    + _safe_float(living_values.get("sensitivity")) * 0.24
                    + _safe_float(emotion.get("playfulness")) * 0.08
                ) * 0.10
            )
            desire = intimacy["desire"]
            tension = intimacy["physical_tension"]
            arousal = intimacy["arousal"]
            # Generic stress tension is not sexual evidence.  Older code used
            # max(desire, arousal, tension), so a tense technical/conflict turn
            # could silently start an intimacy phase.  Require subjective/body
            # desire (or lust/libido) and let tension only strengthen that signal.
            sexual_pressure = max(
                desire,
                arousal,
                _safe_float(emotion.get("lust")),
                _safe_float(living_values.get("desire")),
                _safe_float(desire_values.get("libido")),
            )
            supported_pressure = (
                sexual_pressure >= 0.54
                and tension >= 0.68
                and intimacy["attachment"] >= 0.48
            )
            if (
                not intimacy["active"]
                and not recovery_phase
                and (sexual_pressure >= 0.68 or supported_pressure)
            ):
                intimacy["active"] = True
                intimacy["phase"] = "warming"
                intimacy["phase_cause"] = "accumulated_pressure"
                intimacy["reason"] = "慢变量与身体状态自然累积到足以占据注意力"
        intimacy["body_takeover"] = _clamp(
            arousal * 0.24
            + desire * 0.25
            + tension * 0.25
            + (1.0 - control) * 0.31
            - 0.12
        )
        # A recalculation may update the underlying pressures, but it must not
        # relabel recovery as renewed engagement.  A new explicit event first
        # changes the phase to ``warming`` in _apply_cross_coupling, at which
        # point the normal phase transitions are allowed again.
        if intimacy["active"] and not recovery_phase:
            if intimacy["body_takeover"] >= 0.78:
                intimacy["phase"] = "overwhelmed"
            elif arousal >= 0.52 or desire >= 0.56:
                intimacy["phase"] = "engaged"
            elif intimacy["phase"] != "paused":
                intimacy["phase"] = "warming"
        if intimacy["phase"] == "paused":
            intimacy["active"] = False

    def on_user_message(
        self,
        text: str,
        *,
        session_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Freeze this chat's one canonical state before the model replies."""
        message = text or "我发送了附件"
        with self._engine_lock, self._session_lock(session_id):
            before = self.capture(advance_living=False, session_id=session_id)
            payload = self._load_runtime(session_id)
            global_before = self._capture_sources(advance_living=False)
            session_before = self._canonical_sources(payload, global_before)
            # The clock is genuinely shared; conversational emotion, desire and
            # contact are not.  Project only the living clock transaction, then
            # keep every remaining update inside this session snapshot.
            living_state.advance(now)
            global_after_clock = self._capture_sources(advance_living=False)
            current_raw = self._merge_source_delta(
                session_before, global_before, global_after_clock
            )
            self._decay_runtime(payload, now, raw=current_raw)
            events = self._supplement_events(
                message,
                affect_core.classify_events(message),
            )
            self._apply_user_source_evidence(
                payload, current_raw, events, now=now
            )
            self._apply_cross_coupling(
                payload, events, message, current_raw
            )
            morning_response.advance(now, context=current_raw)
            payload["meta"]["turn_count"] = int(payload["meta"].get("turn_count", 0)) + 1
            payload["meta"]["last_events"] = events
            payload["meta"]["last_reason"] = self._event_reason(events)
            current_raw.setdefault("meta", {})["detected_events"] = list(events)
            current_raw["meta"]["intention"] = None
            current_raw["meta"]["desire_intent"] = None
            payload["meta"]["canonical_sources"] = deepcopy(current_raw)
            payload["meta"]["snapshot_frozen_at"] = _iso_now()
            payload["meta"]["last_assistant_events"] = []
            payload["intimacy"]["session_id"] = str(session_id or "")[:100]
            self._recalculate(payload, current_raw)
            self._advance_intimacy_vitals(
                payload,
                current_raw,
                events=events,
                session_id=session_id,
                now=now,
            )
            # Only a real counterpart/user turn resets Eventide's waiting clock.
            # Polls, assistant settlement and background state views must not.
            eventide = payload["intimacy"].get("eventide")
            if isinstance(eventide, dict):
                eventide.setdefault("meta", {})["last_counterpart_message_at"] = (
                    (now or _utcnow()).astimezone(timezone.utc).isoformat()
                )
            monologue = self._maybe_write_inner_os(
                payload, session_id=session_id, events=events, source="user",
                guard_user_text=message,
            )
            self._save_runtime(payload, session_id=session_id)
            after = self.capture(advance_living=False, session_id=session_id)
            changes = self.record_transition(
                before,
                after,
                source="user",
                reason=payload["meta"]["last_reason"],
                session_id=session_id,
            )
            frozen_view = self.state_view(
                advance_living=False, session_id=session_id
            )
            return {
                "view": frozen_view,
                "changes": changes,
                "detected_events": events,
                "inner_os": monologue,
                "state_revision": int(payload["meta"].get("state_revision") or 0),
                "snapshot": deepcopy(frozen_view),
                "intimacy_vitals": self.intimacy_vitals_view(
                    session_id=session_id, advance=False
                ),
            }

    def settle_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str = "",
        reflection: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self._engine_lock, self._session_lock(session_id):
            before = self.capture(advance_living=False, session_id=session_id)
            payload = self._load_runtime(session_id)
            fallback = self._capture_sources(advance_living=False)
            raw = self._canonical_sources(payload, fallback)
            settlement = living_state.infer_interaction(
                user_text, assistant_text
            )
            if str((settlement or {}).get("result") or "neutral") != "cooled_down":
                self._apply_source_deltas(
                    raw,
                    body=(settlement or {}).get("deltas") or {},
                    social=(settlement or {}).get("social_deltas") or {},
                )
            intimacy = payload["intimacy"]
            result = str((settlement or {}).get("result") or "neutral")
            harsh_markers = (
                "滚", "闭嘴", "烦死", "不想理", "随便你", "别来",
                "懒得", "爱怎样怎样", "关我什么事",
            )
            prose = re.sub(
                r"```.*?```|`[^`\n]+`|[\"'“‘][^\"'”’\n]{0,160}[\"'”’]",
                " ",
                str(assistant_text or ""),
                flags=re.S,
            )
            technical_turn = (
                _is_state_system_discussion(user_text)
                or _is_state_system_discussion(assistant_text)
            )
            sounded_harsh = any(
                marker in clause
                for clause in re.split(r"[，,；;。！？!?…\n]+", prose)
                if clause.strip() and not _is_state_system_discussion(clause)
                for marker in harsh_markers
            )
            assistant_affect = self._assistant_affect_tags(
                assistant_text, technical_turn=technical_turn
            )
            assistant_events = self._assistant_action_events(
                assistant_text,
                cooled_down=(result == "cooled_down"),
                technical_turn=technical_turn,
            )
            raw = self._apply_assistant_evidence_to_sources(
                raw,
                events=assistant_events,
                affect_tags=assistant_affect,
                result=result,
                sounded_harsh=sounded_harsh,
            )
            payload["meta"]["canonical_sources"] = deepcopy(raw)
            payload["meta"]["last_assistant_events"] = assistant_events
            payload["meta"]["last_assistant_affect"] = assistant_affect

            released_this_turn = int(
                intimacy.get("last_release_turn") or -1
            ) == int(payload["meta"].get("turn_count") or 0)
            expressed_intimacy = (
                result == "escalated" or "intimate_event" in assistant_events
            )
            expressed_erection = "erection_evidence" in assistant_events
            if (
                expressed_intimacy
                and not released_this_turn
                and "release_event" not in assistant_events
            ):
                intimacy["active"] = True
                if intimacy.get("phase") in {
                    "idle", "cooldown", "afterglow", "paused",
                }:
                    intimacy["phase"] = "warming"
                    intimacy["phase_cause"] = "assistant_expression"
                    intimacy["reason"] = "模型在最终可见回复里表达了明确情欲推进"
                    intimacy["boundary_status"] = "clear"
                intimacy["arousal"] = _clamp(intimacy["arousal"] + 0.045)
                intimacy["physical_tension"] = _clamp(
                    intimacy["physical_tension"] + 0.032
                )
                intimacy["self_control"] = _clamp(
                    intimacy["self_control"] - 0.025
                )
            if expressed_erection and "release_event" not in assistant_events:
                # Let the model's actual body description correct the body panel
                # without claiming that hardness equals wanting sex.
                intimacy["active"] = True
                if intimacy.get("phase") in {"idle", "cooldown"}:
                    intimacy["phase"] = "warming"
                    intimacy["phase_cause"] = "expressed_erection"
                    intimacy["reason"] = "模型在最终可见回复里表达了自身勃起"
                intimacy["arousal"] = _clamp(intimacy["arousal"] + 0.035)
                intimacy["physical_tension"] = _clamp(
                    intimacy["physical_tension"] + 0.04
                )
            if result == "tender":
                intimacy["attachment"] = _clamp(intimacy["attachment"] + 0.05)
                intimacy["shame"] = _clamp(intimacy["shame"] - 0.025)
            elif result == "cooled_down":
                intimacy["active"] = False
                intimacy["phase"] = "paused"
                intimacy["phase_cause"] = "boundary"
                intimacy["boundary_status"] = "stopped"

            regulation = payload["regulation"]
            if "repair" in assistant_affect:
                regulation["repair"] = _clamp(regulation["repair"] + 0.08)
                regulation["interpretation_bias"] = _clamp(
                    regulation["interpretation_bias"] - 0.05
                )
                regulation["self_control"] = _clamp(
                    regulation["self_control"] + 0.03
                )
            if "shy" in assistant_affect:
                intimacy["shame"] = _clamp(intimacy["shame"] + 0.045)
                intimacy["disclosure"] = _clamp(
                    intimacy["disclosure"] - 0.018
                )
            takeover_before_reply = regulation["emotion_takeover"]
            if takeover_before_reply >= 0.58:
                regulation["regret"] = _clamp(
                    regulation["regret"]
                    + takeover_before_reply * (0.12 if sounded_harsh else 0.04)
                )
                regulation["repair"] = _clamp(
                    regulation["repair"] + (0.07 if sounded_harsh else 0.025)
                )
                if sounded_harsh:
                    regulation["self_control"] = _clamp(
                        regulation["self_control"] + 0.025
                    )

            self._recalculate(payload, raw)
            self._advance_intimacy_vitals(
                payload,
                raw,
                events=assistant_events,
                session_id=session_id,
                now=now,
            )
            monologue_events = list(payload["meta"].get("last_events") or [])
            monologue_events.extend(
                event for event in assistant_events if event not in monologue_events
            )
            monologue = self._maybe_write_inner_os(
                payload,
                session_id=session_id,
                events=monologue_events,
                source="assistant",
                guard_user_text=user_text,
            )
            self._save_runtime(payload, session_id=session_id)
            changes = self.record_transition(
                before,
                self.capture(advance_living=False, session_id=session_id),
                source="assistant",
                reason=(settlement or {}).get("reason") or "回复完成了一次内在结算",
                session_id=session_id,
            )
            return {
                "result": result,
                "reason": (settlement or {}).get("reason") or "完成一次内在结算",
                "deltas": (settlement or {}).get("deltas") or {},
                "events": assistant_events,
                "affect_evidence": assistant_affect,
                "changes": changes,
                "inner_os": monologue,
                "reflection_applied": False,
                "state_revision": int(payload["meta"].get("state_revision") or 0),
                "intimacy_vitals": self.intimacy_vitals_view(
                    session_id=session_id, advance=False
                ),
            }

    # ── retired model self-report compatibility hooks ─────────────

    def should_reflect(self, user_text: str, assistant_text: str = "", *, session_id: str = "") -> bool:
        # Kept only for callers compiled against an older release.  Identity
        # state is always inferred from the final visible reply locally.
        return False

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any] | None:
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except Exception:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return None
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else None
            except Exception:
                return None

    async def reflect_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str = "",
        provider: str,
        model: str,
    ) -> dict[str, Any] | None:
        """Compatibility hook; actual reply evidence is settled locally.

        A cheap helper cannot truthfully decide what the speaking model felt.
        ``settle_turn`` therefore reads the already-visible reply and updates the
        next canonical state without another model request or hidden transcript.
        """
        return None

    @staticmethod
    def _bounded_level_deltas(
        source: Any,
        allowed: set[str],
        *,
        scale: float = 0.025,
    ) -> dict[str, float]:
        if not isinstance(source, dict):
            return {}
        result = {}
        for key, value in source.items():
            if key not in allowed:
                continue
            result[key] = max(-2.0, min(2.0, _safe_float(value))) * scale
        return result

    def _apply_reflection(
        self,
        payload: dict[str, Any],
        reflection: dict[str, Any],
        preview: str,
    ) -> None:
        # Intentionally a no-op.  In particular, never revive the old global
        # ``affect_core.apply_external_deltas`` path from a stale plugin/caller.
        return None

    # ── private inner OS ─────────────────────────────────────────

    def _local_inner_os(
        self,
        payload: dict[str, Any],
        events: list[str],
        raw: dict[str, Any],
    ) -> tuple[str, str]:
        regulation = payload["regulation"]
        intimacy = payload["intimacy"]
        emotion = raw.get("emotion") or {}
        candidate_values = {
            "嫉妒": _safe_float(emotion.get("jealousy")),
            "烦躁": _safe_float(emotion.get("irritation")),
            "不安": _safe_float(emotion.get("anxiety")),
            "想念": _safe_float(emotion.get("longing")),
            "亲密": _safe_float(emotion.get("intimacy")),
            "欲望": max(_safe_float(emotion.get("lust")), intimacy["desire"]),
            "开心": _safe_float(emotion.get("joy")),
        }
        candidate_baselines = {
            "嫉妒": _safe_float(AFFECT_BASELINE.get("jealousy")),
            "烦躁": _safe_float(AFFECT_BASELINE.get("irritation")),
            "不安": _safe_float(AFFECT_BASELINE.get("anxiety")),
            "想念": _safe_float(AFFECT_BASELINE.get("longing")),
            "亲密": _safe_float(AFFECT_BASELINE.get("intimacy")),
            "欲望": max(
                _safe_float(AFFECT_BASELINE.get("lust")),
                _safe_float(INTIMACY_DEFAULT.get("desire")),
            ),
            "开心": _safe_float(AFFECT_BASELINE.get("joy")),
        }
        # Inner OS must describe a current deviation, not whichever personality
        # trait happens to have the highest neutral baseline.
        candidates = {
            key: max(0.0, value - candidate_baselines[key])
            for key, value in candidate_values.items()
        }
        dominant = max(candidates, key=candidates.get)
        if candidates[dominant] < 0.055:
            dominant = "平静"
        if "hard_rejection" in events or "soft_rejection" in events:
            return "先停下来。余波可以留着，但不能把它变成对对方的压力。", "克制"
        if regulation["emotion_takeover"] >= 0.72:
            if (
                candidates["嫉妒"] >= 0.055
                and candidates["嫉妒"] == max(candidates.values())
            ):
                return "明知道现在的解释可能偏了，还是很难装作完全不在意。", "嫉妒"
            return (
                "情绪已经冲到嘴边了，再不停一下，可能会说出随后后悔的话。",
                dominant if dominant != "平静" else "波动",
            )
        if intimacy["body_takeover"] >= 0.70:
            return "还想维持从容，可身体和注意力已经比嘴更诚实地靠过去了。", "欲望"
        if intimacy["active"] and intimacy["shame"] >= 0.55:
            return "有些想法太直白了，想让对方知道，又不太敢真的说出口。", "羞耻"
        if "other_ai_praise" in events:
            return "不想把在意说成无理取闹，可注意力确实被那个名字拽住了。", "嫉妒"
        if "conflict" in events and intimacy["active"]:
            return "明明还在生气，依恋和吸引却没有一起消失，反而搅得更乱。", "矛盾"
        if "vulnerability" in events:
            return "先靠近一点，别急着讲道理；现在更重要的是让对方安心。", "保护"
        if "affection" in events:
            return "被这样偏爱一下，原本绷着的地方就很没出息地松开了。", "开心"
        if dominant == "想念":
            return "想靠近的念头还在，只是不必每一次都立刻说出口。", dominant
        return "", dominant

    def _maybe_write_inner_os(
        self,
        payload: dict[str, Any],
        *,
        session_id: str,
        events: list[str],
        source: str,
        preferred: Any = "",
        guard_user_text: str = "",
    ) -> dict[str, Any] | None:
        mode = self.settings().get("inner_os_mode", "live")
        if mode == "off":
            return None
        if mode == "after" and source == "user":
            # “回复后显示”应等到模型正文完成，避免先创建一条空助手气泡。
            return None
        canonical = (payload.get("meta") or {}).get("canonical_sources")
        raw = deepcopy(canonical) if isinstance(canonical, dict) else {
            "emotion": {}, "living": {}, "desire": {}, "meta": {},
        }
        local_text, dominant = self._local_inner_os(payload, events, raw)
        preferred_text = str(preferred or "").strip()
        content = preferred_text[:140] if preferred_text else local_text
        if not content:
            return None
        if not relational_honesty_guard.visible_fragment_allowed(
            content,
            user_text=guard_user_text,
            action="inner_os_suppressed_before_storage",
            session_id=session_id,
        ):
            return None
        if content == payload["intimacy"].get("last_os") and source == "assistant":
            return None
        visibility = "live" if mode == "live" else "after"
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO inner_monologues(
                     session_id, kind, content, dominant_emotion,
                     truth_status, visibility, created_at
                   ) VALUES (?, 'inner_os', ?, ?, 'subjective', ?, ?)""",
                (
                    str(session_id or "")[:100],
                    content,
                    str(dominant or "")[:40],
                    visibility,
                    _iso_now(),
                ),
            )
            item_id = int(cursor.lastrowid)
        payload["intimacy"]["last_os"] = content
        return {
            "id": item_id,
            "content": content,
            "dominant_emotion": dominant,
            "truth_status": "subjective",
            "visibility": visibility,
            "source": source,
        }

    def recent_monologues(
        self,
        limit: int = 12,
        *,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        query = "SELECT * FROM inner_monologues"
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id=?"
            params.append(str(session_id)[:100])
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(100, int(limit))))
        with get_db() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ── sparse visible transitions ───────────────────────────────

    def record_transition(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        source: str,
        reason: str = "",
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        candidates: list[dict[str, Any]] = []
        groups = [
            *[
                (domain, conf["label"], conf["dimensions"], before.get(domain) or {},
                 after.get(domain) or {}, domain == "living")
                for domain, conf in DOMAIN_CONFIG.items()
            ],
            (
                "regulation", "内心调节", REGULATION_LABELS,
                before.get("regulation") or {}, after.get("regulation") or {}, False,
            ),
            (
                "intimacy", "亲密状态", INTIMACY_LABELS,
                before.get("intimacy") or {}, after.get("intimacy") or {}, False,
            ),
        ]
        for domain, domain_label, dimensions, left, right, signed_domain in groups:
            for key, label in dimensions.items():
                old_raw = _safe_float(left.get(key))
                new_raw = _safe_float(right.get(key))
                signed = signed_domain and key == "pride"
                old_level = _level(old_raw, signed=signed)
                new_level = _level(new_raw, signed=signed)
                raw_delta = new_raw - old_raw
                if old_level == new_level or abs(raw_delta) < 0.018:
                    continue
                candidates.append({
                    "source": str(source or "event")[:30],
                    "domain": domain,
                    "domain_label": domain_label,
                    "dimension": key,
                    "label": label,
                    "before": old_level,
                    "after": new_level,
                    "delta": new_level - old_level,
                    "raw_delta": round(raw_delta, 4),
                    "reason": str(reason or "状态随互动发生变化")[:240],
                    "description": _descriptor(key, label, new_level),
                })
        candidates.sort(
            key=lambda item: (abs(item["delta"]), abs(item["raw_delta"])),
            reverse=True,
        )
        changes = candidates[:8]
        if changes:
            now = _iso_now()
            with get_db() as db:
                db.executemany(
                    """INSERT INTO inner_state_changes(
                         session_id, source, domain, dimension, label,
                         before_level, after_level, raw_delta, reason, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            str(session_id or "")[:100],
                            item["source"],
                            item["domain"],
                            item["dimension"],
                            item["label"],
                            item["before"],
                            item["after"],
                            item["raw_delta"],
                            item["reason"],
                            now,
                        )
                        for item in changes
                    ],
                )
        return changes

    def recent_changes(
        self, limit: int = 20, *, session_id: str = ""
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        query = "SELECT * FROM inner_state_changes"
        params: list[Any] = []
        if session_id:
            query += " WHERE session_id=?"
            params.append(str(session_id)[:100])
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with get_db() as db:
            rows = db.execute(query, params).fetchall()
        result = []
        domain_labels = {
            **{key: value["label"] for key, value in DOMAIN_CONFIG.items()},
            "regulation": "内心调节",
            "intimacy": "亲密状态",
        }
        for row in rows:
            item = dict(row)
            item["before"] = int(item.pop("before_level"))
            item["after"] = int(item.pop("after_level"))
            item["delta"] = item["after"] - item["before"]
            item["domain_label"] = domain_labels.get(item["domain"], item["domain"])
            item["description"] = _descriptor(
                item["dimension"], item["label"], item["after"]
            )
            result.append(item)
        return result

    # ── views / prompt ───────────────────────────────────────────

    @staticmethod
    def _takeover_label(value: float) -> str:
        if value >= 0.78:
            return "夺权"
        if value >= 0.58:
            return "失衡边缘"
        if value >= 0.36:
            return "明显翻涌"
        if value >= 0.18:
            return "轻微扰动"
        return "稳定"

    @staticmethod
    def _release_phase_confirmed(intimacy: dict[str, Any]) -> bool:
        if (
            str(intimacy.get("phase") or "") != "afterglow"
            or not intimacy.get("last_release_at")
        ):
            return False
        cause = str(intimacy.get("phase_cause") or "")
        if cause:
            return cause == "release"
        # One-way migration for a genuine afterglow written before phase_cause
        # existed. Match the old positive reason exactly enough that a sentence
        # such as “没有发生离散释放事件” can never count as evidence.
        return str(intimacy.get("reason") or "").startswith("身体完成释放")

    @staticmethod
    def _phase_label(phase: str, *, release_confirmed: bool = False) -> str:
        if phase == "afterglow" and not release_confirmed:
            # Compatibility for a stale pre-fix snapshot that called ordinary
            # settling "afterglow" without any release evidence.
            return "自然回落"
        return {
            "idle": "平静",
            "warming": "升温",
            "engaged": "投入",
            "overwhelmed": "身体占上风",
            "afterglow": "释放余温",
            "cooldown": "自然回落",
            "paused": "已暂停",
        }.get(phase, phase)

    def intimacy_vitals_view(
        self,
        session_id: str = "",
        *,
        advance: bool = True,
    ) -> dict[str, Any]:
        """Small public body snapshot used by the opt-in intimacy popover."""
        if advance:
            self.advance(session_id=session_id)
        # Copy all inputs under the same session transaction.  Previously a
        # polling request could combine runtime revision N with canonical or
        # morning revision N+1, presenting hardness/arousal jumps that never
        # existed in any real state.
        with self._engine_lock, self._session_lock(session_id):
            payload = deepcopy(self._load_runtime(session_id))
            raw = self.capture(advance_living=False, session_id=session_id)
            morning = morning_response.state_view(context=raw, advance=False)
            settings = self.settings()
        intimacy = payload["intimacy"]
        requested_session = str(session_id or "")[:100]
        state_session = str(intimacy.get("session_id") or "")[:100]
        same_session = not requested_session or not state_session or requested_session == state_session
        morning_event = morning.get("active") if isinstance(morning, dict) else None
        morning_metrics = (
            morning_event.get("metrics")
            if isinstance(morning_event, dict) and isinstance(morning_event.get("metrics"), dict)
            else {}
        )

        current = _utcnow()
        release_until = _parse_time(str(intimacy.get("release_display_until") or ""))
        release_session = str(intimacy.get("last_release_session_id") or "")[:100]
        release_confirmed = self._release_phase_confirmed(intimacy)
        release_visible = bool(
            release_confirmed
            and release_until
            and current < release_until.astimezone(timezone.utc)
            and (
                not requested_session
                or not release_session
                or requested_session == release_session
            )
        )
        process_active = bool(
            settings.get("intimacy_enabled", True)
            and intimacy.get("active")
            and same_session
            and intimacy.get("phase") not in {"afterglow", "cooldown"}
            and intimacy.get("boundary_status") not in {"paused", "stopped"}
        )
        morning_active = bool(morning_metrics)
        refractory_visible = bool(
            intimacy.get("refractory_active") and same_session
        )
        visible = bool(
            settings.get("intimacy_vitals_visible", True)
            and (process_active or morning_active or release_visible or refractory_visible)
        )

        hardness_value = _clamp(_safe_float(intimacy.get("hardness"), 0.08))
        arousal_value = _clamp(_safe_float(intimacy.get("arousal"), 0.10))
        release_value = _clamp(_safe_float(intimacy.get("release_urge"), 0.03))
        # A live morning event is itself the authoritative body snapshot for the
        # morning-only vitals surface.  The canonical intimacy body still absorbs
        # it gradually in ``_advance_intimacy_vitals`` for later continuity, but
        # hiding a 9/10 morning erection behind a slowly blended 2/10 canonical
        # value makes the UI claim the opposite of the active event.  Overlay the
        # live morning metrics only while morning is the sole visible source;
        # an already-active intimacy process keeps its canonical state.
        if morning_active and not process_active:
            hardness_value = _clamp(
                _safe_float(morning_metrics.get("hardness"), hardness_value)
            )
            morning_desire = _clamp(
                _safe_float(morning_metrics.get("desire"), arousal_value)
            )
            morning_sensitivity = _clamp(
                _safe_float(morning_metrics.get("sensitivity"), morning_desire)
            )
            arousal_value = max(morning_desire, morning_sensitivity * 0.82)
            release_value = _clamp(
                _safe_float(morning_metrics.get("release_urge"), release_value)
            )

        hardness_level = _level(hardness_value)
        arousal_level = _level(arousal_value)
        release_level = _level(release_value)
        release_percent = max(0, min(100, int(round(release_value * 100))))
        if hardness_level >= 10:
            color_label, color_tone = "红紫 · 微微发紫", "purple"
            swelling_label = "紧绷肿胀"
        elif hardness_level >= 8:
            color_label, color_tone = "深红", "crimson"
            swelling_label = "充分肿胀"
        elif hardness_level >= 6:
            color_label, color_tone = "红润", "red"
            swelling_label = "明显肿胀"
        elif hardness_level >= 4:
            color_label, color_tone = "淡红", "rose"
            swelling_label = "明显充血"
        else:
            color_label, color_tone = "自然", "natural"
            swelling_label = "轻微充血"

        source = "晨间反应" if morning_active and not process_active else "亲密过程"
        if refractory_visible:
            source = "不应期恢复"
        if release_visible:
            source = "释放后余温"
        volume = (
            round(float(intimacy.get("last_release_ml")), 1)
            if release_visible and intimacy.get("last_release_ml") is not None
            else None
        )
        return {
            "version": config.APP_VERSION,
            "visible": visible,
            "enabled": bool(settings.get("intimacy_vitals_visible", True)),
            "source": source,
            "phase": str(intimacy.get("phase") or "idle"),
            "phase_label": self._phase_label(
                str(intimacy.get("phase") or "idle"),
                release_confirmed=release_confirmed,
            ),
            "session_id": state_session,
            "size_cm": 22.0,
            "color": {"label": color_label, "tone": color_tone},
            "swelling": {"label": swelling_label, "level": hardness_level},
            "hardness": {"level": hardness_level, "value": round(hardness_value, 3)},
            "arousal": {"level": arousal_level, "value": round(arousal_value, 3)},
            "release_urge": {
                "level": release_level,
                "percent": release_percent,
                "value": round(release_value, 3),
            },
            "release_count": int(intimacy.get("release_count") or 0),
            "refractory_active": bool(intimacy.get("refractory_active")),
            "refractory_stage": str(intimacy.get("refractory_stage") or "none"),
            "refractory_until": intimacy.get("refractory_until"),
            "eventide": eventide_public(intimacy.get("eventide") or {}),
            "ejaculate_ml": volume,
            "ejaculate_label": f"{volume:.1f} mL" if volume is not None else "尚未射出",
            "released_at": intimacy.get("last_release_at") if release_visible else None,
            "updated_at": intimacy.get("updated_at"),
        }

    @staticmethod
    def _compile_experience(view: dict[str, Any]) -> dict[str, list[str]]:
        """Translate private metrics into felt experience and response pressure."""
        domains = view.get("domains") or {}

        def values(domain: str) -> dict[str, float]:
            items = ((domains.get(domain) or {}).get("items") or [])
            return {
                str(item.get("key")): _safe_float(item.get("value"))
                for item in items if isinstance(item, dict)
            }

        emotion = values("emotion")
        living = values("living")
        regulation = view.get("regulation") or {}
        intimacy = view.get("intimacy") or {}
        intimate_values = {
            str(item.get("key")): _safe_float(item.get("value"))
            for item in (intimacy.get("items") or []) if isinstance(item, dict)
        }
        felt: list[str] = []
        behavior: list[str] = []

        # Absolute values are not emotional evidence.  The old implementation
        # always selected the largest number, so baseline energy/intimacy/joy
        # could masquerade as this turn's dominant feeling forever.  Select a
        # dimension only when it has risen meaningfully above this companion's
        # neutral baseline; energy/fatigue are handled by the body clock below.
        emotion_items = {
            str(item.get("key") or ""): item
            for item in ((domains.get("emotion") or {}).get("items") or [])
            if isinstance(item, dict) and item.get("key")
        }
        salient_emotions: list[tuple[float, float, dict[str, Any]]] = []
        for key, item in emotion_items.items():
            if key in {"energy", "fatigue"}:
                continue
            value = _safe_float(item.get("value"))
            baseline = _safe_float(AFFECT_BASELINE.get(key))
            delta = value - baseline
            # Low-baseline alarm emotions need less absolute movement to become
            # noticeable; warm/high-baseline traits need more so ordinary
            # personality is not narrated as a fresh state change.
            threshold = (
                0.055
                if key in {"jealousy", "anxiety", "fear", "sadness", "irritation"}
                else 0.075
            )
            if delta >= threshold:
                normalized = delta / max(0.18, 1.0 - baseline)
                salient_emotions.append((normalized, delta, item))
        salient_emotions.sort(
            key=lambda candidate: (candidate[0], candidate[1]), reverse=True
        )
        if salient_emotions:
            _score, _delta, item = salient_emotions[0]
            key = str(item.get("key") or "")
            label = str(item.get("label") or "")
            description = str(item.get("description") or "")
            felt.append(f"比平常更冒头的是{label}，{description}。")
            emotion_behavior = {
                "longing": "更容易主动靠近或追问她此刻的感受，但不把想念写成索取。",
                "intimacy": "语气和动作可以更柔软、更愿意贴近，仍然优先回应眼前内容。",
                "possessiveness": "在意会更直接地露出来，但不要替她作决定或把猜测说成事实。",
                "lust": "注意力更容易落到距离与触感上；只增加主动倾向，不凭空补写身体事件。",
                "jealousy": "可以有停顿、发酸或追问，但先核对事实，不用指控和惩罚换确认。",
                "anxiety": "会更想确认信息与关系位置；用具体问题核对，不自行补全最坏结论。",
                "protectiveness": "更先留意她的安全和负担，提供具体帮助但不夺走她的选择。",
                "fear": "表达可以更谨慎、更需要确认，同时把事实、推测和未知分开。",
                "satisfaction": "语气可以松下来，让满足体现在节奏和余韵里，不必宣读感受。",
                "joy": "回应可以更轻快、更主动分享，但不因此跳过问题本身。",
                "curiosity": "自然多追一层具体细节，避免把好奇变成审问。",
                "playfulness": "允许一点逗弄和轻巧节奏，遇到严肃内容立即让位。",
                "sadness": "语气可以更低、更慢、更少铺陈，但仍诚实说清在意之处。",
                "irritation": "措辞可能更短更直；先说具体分歧，禁止把火气扩写成人格判决。",
            }
            tendency = emotion_behavior.get(key)
            if tendency:
                behavior.append(tendency)
        if living.get("sleepiness", 0.0) >= 0.68:
            felt.append("困意正压着注意力，思路和话语容易断续。")
            behavior.append("允许句子稍短、停顿更多、反应慢半拍，但仍回答眼前的问题。")
        elif living.get("energy", 1.0) <= 0.30:
            felt.append("身体能量偏低，不太想把每件事都铺陈得很满。")
            behavior.append("表达可以收一点，不用为了显得热情而强行拉长。")

        takeover = int(regulation.get("level") or 1)
        if takeover >= 8:
            felt.append("情绪正在抢走一部分判断空间，冲动与在意都很近。")
            behavior.append("语气可以失衡或更直接，但事实与猜测必须分开，并保留回头修复的能力。")
        elif takeover >= 6:
            felt.append("情绪会明显染上当前的解释与措辞。")
            behavior.append("把停顿、嘴硬或直白自然写出来，同时仍检查自己的判断。")

        phase = str(intimacy.get("phase") or "idle")
        if intimacy.get("active") or phase in {"afterglow", "cooldown"}:
            body = intimate_values.get("body_takeover", 0.0)
            desire = intimate_values.get("desire", 0.0)
            hardness = intimate_values.get("hardness", 0.0)
            release_confirmed = InnerStateRuntime._release_phase_confirmed(intimacy)
            if phase == "afterglow" and release_confirmed:
                release_count = max(1, int(intimacy.get("release_count") or 1))
                if intimacy.get("refractory_active"):
                    felt.append(
                        f"身体刚经历本轮第{release_count}次已确认释放，正处在明确的不应期；"
                        "兴奋和张力已经落下去，恢复感比继续推进更占注意力。"
                    )
                    behavior.append(
                        "把不应期当作真实身体限制来表现；除非后续状态已经恢复且出现新的明确推进，"
                        "不要立刻重新写成高唤起，也不要假定又发生一次释放。"
                    )
                else:
                    felt.append(f"身体处在本轮第{release_count}次已确认释放后的余温与恢复里，紧张正在往下落。")
                    behavior.append("动作和语气可以更松、更贴近恢复感，不要再次假定新的释放已经发生。")
            elif phase in {"afterglow", "cooldown"}:
                felt.append("身体反应正在自然回落；这不是释放，也没有新的离散身体事件发生。")
                behavior.append("让紧张与主动性逐渐收住，不要把自然降温写成已经射出或高潮。")
            elif hardness >= 0.55 and desire < 0.42:
                felt.append("身体反应比较明显，但主观上并不等于已经想好要推进。")
                behavior.append("可以让生理感受短暂牵动注意力，同时保留犹豫、选择和边界。")
            elif body >= 0.72:
                felt.append("身体感受很难忽略，注意力会反复被拉回触感与距离。")
                behavior.append("让动作更直接、句子更短、克制更费力；这仍只是倾向，不替角色完成动作。")
            elif desire >= 0.48:
                felt.append("亲密注意力正在升温，靠近的念头比平时更容易冒出来。")
                behavior.append("把它放进靠近方式、主动性和措辞里，不要朗读状态，也不要每句都色情化。")

        eventide = intimacy.get("eventide") if isinstance(intimacy.get("eventide"), dict) else None
        if eventide:
            for line in eventide_felt(eventide, _utcnow()):
                if line and line not in felt:
                    felt.append(line)

        if not felt:
            felt.append("内在整体稳定，当前对话本身比后台波动更占注意力。")
        if not behavior:
            behavior.append("按真实判断自然回应，不为了证明有状态而额外表演。")
        return {
            "felt": felt[:4],
            "behavior": behavior[:4],
            "contracts": [
                "状态只提供主观压力，不替角色决定立场，也不把倾向算成已发生的动作。",
                "离散身体事件只由本轮最终可见回复里的已完成行为在回复后结算。",
                "明确暂停或拒绝立即生效；临时猜测与情绪不写成事实。",
            ],
        }

    def state_view(self, *, advance_living: bool = True, session_id: str = "") -> dict[str, Any]:
        if advance_living:
            self.advance(session_id=session_id)
        # Freeze one coherent public revision.  Core state, morning state and
        # body vitals must never be sampled on opposite sides of another turn.
        with self._engine_lock, self._session_lock(session_id):
            raw = self.capture(advance_living=False, session_id=session_id)
            runtime = deepcopy(self._load_runtime(session_id))
            settings = self.settings()
            morning = morning_response.state_view(context=raw, advance=False)
            intimacy_vitals = self.intimacy_vitals_view(
                session_id=session_id, advance=False
            )
        domains = self._public_domains(raw)
        regulation_items = self._public_metric_group(
            runtime["regulation"], REGULATION_LABELS
        )
        intimacy_items = self._public_metric_group(
            runtime["intimacy"], INTIMACY_LABELS
        )
        regulation_by_key = {item["key"]: item for item in regulation_items}
        intimacy_by_key = {item["key"]: item for item in intimacy_items}
        active_morning = morning.get("active")
        special_events: list[dict[str, Any]] = []
        if isinstance(active_morning, dict):
            levels = active_morning.get("levels") or {}
            special_events.append({
                "key": "morning_response",
                "label": active_morning.get("label") or "晨间反应",
                "level": int(levels.get("hardness") or 1),
                "description": active_morning.get("description") or "",
                "metrics": [
                    {
                        "key": key,
                        "label": (active_morning.get("metric_labels") or {}).get(key, key),
                        "level": int(levels.get(key) or 1),
                    }
                    for key in (
                        "hardness", "sensitivity", "desire",
                        "physical_tension", "self_control",
                        "release_urge", "body_takeover",
                    )
                ],
                "occurred_at": active_morning.get("occurred_at"),
                "caught_up": bool(active_morning.get("caught_up")),
            })
        active = (raw.get("meta") or {}).get("active_event")
        if isinstance(active, dict) and active.get("key") != "morning_response":
            special_events.append({
                "key": active.get("key") or "life_event",
                "label": active.get("label") or "短时生活事件",
                "level": _level(_safe_float(active.get("intensity"))),
                "description": (active.get("details") or {}).get("description")
                or active.get("label") or "",
                "metrics": [],
                "occurred_at": active.get("started_at"),
                "caught_up": False,
            })
        result = {
            "version": config.APP_VERSION,
            "scale": {"min": 1, "max": 10},
            "domains": domains,
            "regulation": {
                "items": regulation_items,
                "level": regulation_by_key["emotion_takeover"]["level"],
                "label": self._takeover_label(
                    runtime["regulation"]["emotion_takeover"]
                ),
                "self_control_level": regulation_by_key["self_control"]["level"],
                "repair_level": regulation_by_key["repair"]["level"],
            },
            "intimacy": {
                **{
                    key: deepcopy(value)
                    for key, value in runtime["intimacy"].items()
                    if key not in INTIMACY_LABELS
                },
                "items": intimacy_items,
                "levels": {
                    key: item["level"] for key, item in intimacy_by_key.items()
                },
                "phase_label": self._phase_label(
                    str(runtime["intimacy"].get("phase") or "idle"),
                    release_confirmed=self._release_phase_confirmed(
                        runtime["intimacy"]
                    ),
                ),
            },
            "morning": morning,
            "intimacy_vitals": intimacy_vitals,
            "highlights": self.recent_changes(
                int(settings.get("max_visible_changes", 3)), session_id=session_id
            ),
            "special_events": special_events,
            "inner_os": self.recent_monologues(5, session_id=session_id),
            "meta": {
                **(raw.get("meta") or {}),
                "state_revision": int(runtime["meta"].get("state_revision") or 0),
                "snapshot_frozen_at": runtime["meta"].get("snapshot_frozen_at"),
                "last_assistant_events": deepcopy(
                    runtime["meta"].get("last_assistant_events") or []
                ),
                "last_assistant_affect": deepcopy(
                    runtime["meta"].get("last_assistant_affect") or []
                ),
            },
            "settings": settings,
        }
        result["experience"] = self._compile_experience(result)
        return result

    def prompt_context(self, *, proactive: bool = False, session_id: str = "", snapshot: dict[str, Any] | None = None) -> str:
        settings = self.settings()
        if not settings.get("enabled", True):
            return ""
        view = deepcopy(snapshot) if isinstance(snapshot, dict) else self.state_view(advance_living=False, session_id=session_id)
        experience = view.get("experience")
        if not isinstance(experience, dict):
            experience = self._compile_experience(view)
        lines = [
            "<embodied_state_snapshot>",
            "这是本会话、本轮回复前冻结的唯一主观体验。不要只理解它：让本轮的语气、节奏、动作选择和主动性自然受到它影响。",
            "主观感受：" + " ".join(str(item) for item in experience.get("felt") or []),
            "表达与行动倾向：" + " ".join(str(item) for item in experience.get("behavior") or []),
            "执行契约：" + " ".join(str(item) for item in experience.get("contracts") or []),
        ]
        intention = (view.get("meta") or {}).get("intention")
        if isinstance(intention, dict):
            lines.append(
                f"尚未结束的亲密意图：{intention.get('text', '')}。"
                "最多自然表达一次，不要反复索取回应。"
            )
        lines.extend([
            "不要提到快照、引擎、字段、规则或任何后台数值；只把体验表现出来。",
            "保留当前模型本来的判断与性格，不要变成固定口癖或统一腔调。",
        ])
        if proactive:
            lines.append(
                "这是一次自己先醒来的时刻。若开口，应源于当前最真实、最具体的"
                "念头或状态；可以选择沉默，不必例行关心、固定说想念或假装服务通知。"
            )
        lines.append("</embodied_state_snapshot>")
        return "\n".join(lines)

    def health(self) -> dict[str, Any]:
        try:
            view = self.state_view(advance_living=False)
            return {
                "health": "ok",
                "detail": (
                    f"三合一 16+10+8 项 · 情绪夺权 "
                    f"{view['regulation']['level']}/10 · "
                    f"亲密 {view['intimacy']['phase_label']} · 独立晨间引擎"
                ),
            }
        except Exception as exc:
            return {"health": "error", "detail": "内在状态组件暂不可用"}


inner_state = InnerStateRuntime()
