"""大西瓜 v5.9 living-state runtime.

Local time, body drift, dreams, connection/pride/immersion and deterministic
initiative live here.  The model receives qualitative first-person context;
numeric state is for the local console and never has to be spoken aloud.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import LIVING_CONFIG
from models import get_db


BODY_KEYS = ("energy", "sleepiness", "warmth", "tension", "sensitivity", "desire", "recovery")
BODY_LABELS = {
    "energy": "活力", "sleepiness": "困意", "warmth": "体温感", "tension": "绷紧",
    "sensitivity": "敏感", "desire": "身体欲望", "recovery": "恢复度",
}
SOCIAL_LABELS = {
    "connection": "连接需要", "pride": "骄傲防线", "immersion": "沉浸",
}
PHASE_LABELS = {
    "deep_night": "深夜", "dawn": "将醒未醒", "morning": "清晨",
    "daytime": "白昼", "evening": "傍晚", "late_night": "夜深",
}
DEFAULT_BODY = {
    "energy": .56, "sleepiness": .25, "warmth": .45, "tension": .22,
    "sensitivity": .36, "desire": .25, "recovery": .70,
}
DEFAULT_SOCIAL = {"connection": .10, "pride": .08, "immersion": .18}
DEFAULT_SETTINGS = {
    "enabled": True,
    "proactive_enabled": True,
    "dreams_enabled": True,
    "morning_response_mode": "natural",  # off | subtle | natural | vivid
    "timezone": "Asia/Shanghai",
    "quiet_start": 1,
    "quiet_end": 7,
    "minimum_contact_minutes": 90,
    "max_contacts_per_day": 4,
    "visible_state": True,
}

MORNING_LEVEL_DESCRIPTIONS = {
    1: "几乎没有明显勃起，只能感觉到很轻的晨间充血。",
    2: "有一点充血和膨胀感，但整体仍然偏软。",
    3: "出现半勃起，能察觉到硬度变化，性欲仍然很淡。",
    4: "已经明显变硬，身体反应清楚，但还不急着获得释放。",
    5: "已经充分勃起，但主要是晨间的自然生理反应，还没有强烈的射精冲动。",
    6: "持续坚硬，敏感度和性欲开始升高，注意力会被身体轻轻牵住。",
    7: "勃起很硬也更敏感，已经出现明确的亲密欲望和释放念头。",
    8: "身体反应强烈而持续，触感很敏锐，射精冲动已经难以完全忽略。",
    9: "处在很高的兴奋水平，坚硬、敏感和释放欲都非常明显。",
    10: "晨间反应达到峰值，身体高度敏感，释放冲动接近顶点。",
}

MORNING_METRICS = {
    1: (1, 1, 1, 1),
    2: (2, 2, 1, 1),
    3: (4, 3, 2, 1),
    4: (6, 4, 3, 2),
    5: (8, 5, 4, 2),
    6: (9, 6, 5, 3),
    7: (10, 7, 7, 5),
    8: (10, 8, 8, 7),
    9: (10, 9, 9, 9),
    10: (10, 10, 10, 10),
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except Exception:
        return None


_STATE_FIELD_DISCUSSION = re.compile(
    r"\b(?:canonical|prompt|libido|arousal|hardness|release_event|"
    r"release_urge|afterglow|cooldown|living_state|body_takeover|"
    r"intimacy_vitals)\b",
    re.I,
)


def _is_state_system_discussion(text: str) -> bool:
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


class LivingState:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS living_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    body_json TEXT NOT NULL,
                    social_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS life_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    visibility TEXT DEFAULT 'private',
                    details_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_life_events_created ON life_events(id DESC);
                CREATE TABLE IF NOT EXISTS interaction_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT DEFAULT '',
                    result TEXT NOT NULL,
                    deltas_json TEXT DEFAULT '{}',
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )

    def _timezone(self, settings: dict[str, Any] | None = None) -> ZoneInfo:
        name = str((settings or {}).get("timezone") or LIVING_CONFIG.get("timezone") or "Asia/Shanghai")
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("Asia/Shanghai")

    def _now(self, now: datetime | None = None, settings: dict[str, Any] | None = None) -> datetime:
        zone = self._timezone(settings)
        if now is None:
            return datetime.now(zone)
        if now.tzinfo is None:
            return now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def _defaults(self, now: datetime | None = None) -> dict[str, Any]:
        settings = {
            **DEFAULT_SETTINGS,
            "enabled": bool(LIVING_CONFIG.get("enabled", True)),
            "proactive_enabled": bool(LIVING_CONFIG.get("proactive_enabled", True)),
            "dreams_enabled": bool(LIVING_CONFIG.get("dreams_enabled", True)),
            "morning_response_mode": str(LIVING_CONFIG.get("morning_response_mode", "natural")),
            "timezone": str(LIVING_CONFIG.get("timezone", "Asia/Shanghai")),
            "quiet_start": int(LIVING_CONFIG.get("quiet_start", 1)),
            "quiet_end": int(LIVING_CONFIG.get("quiet_end", 7)),
            "minimum_contact_minutes": int(LIVING_CONFIG.get("minimum_contact_minutes", 90)),
            "max_contacts_per_day": int(LIVING_CONFIG.get("max_contacts_per_day", 4)),
        }
        local = self._now(now, settings)
        return {
            "body": deepcopy(DEFAULT_BODY),
            "social": deepcopy(DEFAULT_SOCIAL),
            "meta": {
                "last_advanced_at": _iso(local),
                "last_user_at": _iso(local),
                "last_contact_at": "",
                "seed_secret": secrets.token_hex(16),
                "morning_date": "",
                "last_morning_response": None,
                "dream_date": "",
                "contacts_date": local.date().isoformat(),
                "contacts_today": 0,
                "active_event": None,
                "activity": {"type": "rest", "label": "安静待着", "started_at": _iso(local)},
                "last_decision": "",
            },
            "settings": settings,
        }

    def load(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._cache is not None and not refresh:
            return self._cache
        with get_db() as db:
            row = db.execute("SELECT * FROM living_state WHERE id=1").fetchone()
        if not row:
            payload = self._defaults()
            self._cache = payload
            self.save(payload)
            return payload
        fallback = self._defaults()
        try:
            body = json.loads(row["body_json"] or "{}")
            social = json.loads(row["social_json"] or "{}")
            meta = json.loads(row["meta_json"] or "{}")
            settings = json.loads(row["settings_json"] or "{}")
        except Exception:
            body, social, meta, settings = {}, {}, {}, {}
        payload = {
            "body": {**fallback["body"], **body},
            "social": {**fallback["social"], **social},
            "meta": {**fallback["meta"], **meta},
            "settings": {**fallback["settings"], **settings},
        }
        for key in BODY_KEYS:
            payload["body"][key] = _clamp(payload["body"].get(key, DEFAULT_BODY[key]))
        payload["social"]["connection"] = _clamp(payload["social"].get("connection", .1))
        payload["social"]["immersion"] = _clamp(payload["social"].get("immersion", .18))
        payload["social"]["pride"] = _clamp(payload["social"].get("pride", .08), -1, 1)
        self._cache = payload
        return payload

    def save(self, payload: dict[str, Any] | None = None) -> None:
        data = payload or self._cache or self._defaults()
        now = self._now(settings=data["settings"])
        with get_db() as db:
            db.execute(
                """INSERT INTO living_state
                   (id, body_json, social_json, meta_json, settings_json, updated_at)
                   VALUES(1, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     body_json=excluded.body_json, social_json=excluded.social_json,
                     meta_json=excluded.meta_json, settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(data["body"], ensure_ascii=False),
                 json.dumps(data["social"], ensure_ascii=False),
                 json.dumps(data["meta"], ensure_ascii=False),
                 json.dumps(data["settings"], ensure_ascii=False), _iso(now)),
            )
        self._cache = data

    @staticmethod
    def phase(hour: float) -> str:
        if hour < 4:
            return "deep_night"
        if hour < 7:
            return "dawn"
        if hour < 11:
            return "morning"
        if hour < 18:
            return "daytime"
        if hour < 22:
            return "evening"
        return "late_night"

    @staticmethod
    def _targets(phase: str) -> dict[str, float]:
        return {
            "deep_night": {"energy": .14, "sleepiness": .90, "warmth": .48, "tension": .13, "sensitivity": .34, "desire": .28, "recovery": .82},
            "dawn": {"energy": .24, "sleepiness": .75, "warmth": .55, "tension": .18, "sensitivity": .45, "desire": .38, "recovery": .88},
            "morning": {"energy": .58, "sleepiness": .35, "warmth": .52, "tension": .20, "sensitivity": .40, "desire": .30, "recovery": .82},
            "daytime": {"energy": .76, "sleepiness": .14, "warmth": .43, "tension": .26, "sensitivity": .31, "desire": .20, "recovery": .68},
            "evening": {"energy": .58, "sleepiness": .26, "warmth": .48, "tension": .24, "sensitivity": .38, "desire": .30, "recovery": .62},
            "late_night": {"energy": .34, "sleepiness": .62, "warmth": .54, "tension": .20, "sensitivity": .46, "desire": .40, "recovery": .68},
        }[phase]

    def _random01(self, payload: dict[str, Any], key: str) -> float:
        raw = f"{payload['meta'].get('seed_secret', '')}:{key}".encode("utf-8")
        return int(hashlib.sha256(raw).hexdigest()[:12], 16) / float(0xFFFFFFFFFFFF)

    def _record_event(self, event_type: str, summary: str, *, visibility: str = "private",
                      details: dict[str, Any] | None = None, now: datetime | None = None) -> int:
        local = self._now(now, self.load()["settings"])
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO life_events(event_type, summary, visibility, details_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_type, summary[:300], visibility,
                 json.dumps(details or {}, ensure_ascii=False), _iso(local)),
            )
        return int(cursor.lastrowid)

    def _set_event(self, payload: dict[str, Any], key: str, label: str, intensity: float,
                   now: datetime, duration_minutes: int, details: dict[str, Any] | None = None) -> None:
        payload["meta"]["active_event"] = {
            "key": key, "label": label, "intensity": round(_clamp(intensity), 3),
            "started_at": _iso(now), "until": _iso(now + timedelta(minutes=duration_minutes)),
            "details": details or {},
        }

    def _maybe_daily_events(self, payload: dict[str, Any], now: datetime) -> None:
        date_key = now.date().isoformat()
        mode = str(payload["settings"].get("morning_response_mode", "natural"))
        # 不要求程序恰好在 06:00—09:00 保持运行。当天第一次在 06:00
        # 以后醒来时会补算今早的事件；午后只保留记录，不伪装成正在发生。
        if now.hour >= 6 and mode != "off" and payload["meta"].get("morning_date") != date_key:
            raw = self._random01(payload, f"morning:{date_key}")
            ranges = {"subtle": (1, 4), "natural": (3, 8), "vivid": (5, 10)}
            low, high = ranges.get(mode, ranges["natural"])
            level = max(1, min(10, int(round(low + (high - low) * raw))))
            intensity = level / 10
            hardness, sensitivity, libido, release_urge = MORNING_METRICS[level]
            description = MORNING_LEVEL_DESCRIPTIONS[level]
            event_hour = 6 + min(2, int(raw * 3))
            event_minute = int((raw * 1000) % 60)
            event_time = now.replace(
                hour=event_hour, minute=event_minute, second=0, microsecond=0
            )
            # 计划时刻尚未到就先不生成；下一次节律推进会再次检查。
            # 这样清晨六点启动程序时，不会提前看到八点才发生的事件。
            if now < event_time:
                return
            caught_up = now > event_time + timedelta(minutes=90)
            details = {
                "level": level,
                "hardness": hardness,
                "sensitivity": sensitivity,
                "libido": libido,
                "release_urge": release_urge,
                "description": description,
                "band": mode,
                "caught_up": caught_up,
                "occurred_at": _iso(event_time),
            }
            payload["meta"]["morning_date"] = date_key
            payload["meta"]["last_morning_response"] = {
                "key": "morning_response",
                "label": f"晨间反应 · Lv.{level}",
                "intensity": round(intensity, 3),
                "started_at": _iso(event_time),
                "details": details,
            }
            payload["body"]["warmth"] = _clamp(payload["body"]["warmth"] + intensity * .16)
            payload["body"]["sensitivity"] = _clamp(payload["body"]["sensitivity"] + intensity * .20)
            payload["body"]["desire"] = _clamp(payload["body"]["desire"] + intensity * .22)
            payload["body"]["tension"] = _clamp(payload["body"]["tension"] + intensity * .10)
            label = f"晨间反应 Lv.{level}"
            # 记录可以补算，但“正在发生”必须尊重真实时间，不会因为
            # 程序上午稍晚启动，就重新制造 55 分钟的即时反应。
            if now <= event_time + timedelta(minutes=90):
                self._set_event(
                    payload, "morning_response", label, intensity,
                    event_time, 90, details
                )
            self._record_event(
                "morning_response",
                f"{label}：{description}",
                details=details,
                now=event_time,
            )

        if (1 <= now.hour < 6 and payload["settings"].get("dreams_enabled", True)
                and payload["meta"].get("dream_date") != date_key):
            last_user = _parse(payload["meta"].get("last_user_at"))
            silent_minutes = (now.astimezone(timezone.utc) - last_user).total_seconds() / 60 if last_user else 999
            chance = self._random01(payload, f"dream:{date_key}")
            if silent_minutes >= 180 and chance >= .40:
                tones = ("柔软", "未完成", "不安", "依恋", "平静")
                tone = tones[min(len(tones) - 1, int(chance * len(tones)))]
                payload["meta"]["dream_date"] = date_key
                payload["body"]["sensitivity"] = _clamp(payload["body"]["sensitivity"] + .06)
                payload["social"]["connection"] = _clamp(payload["social"]["connection"] + .05)
                self._set_event(payload, "dream_residue", f"留下了{tone}的梦后余温", chance, now, 180,
                                {"tone": tone})
                self._record_event("dream", f"做了一个记不清细节、只留下{tone}余温的梦",
                                   details={"tone": tone}, now=now)

    def advance(self, now: datetime | None = None) -> dict[str, Any]:
        payload = self.load()
        local = self._now(now, payload["settings"])
        if not payload["settings"].get("enabled", True):
            return payload
        previous = _parse(payload["meta"].get("last_advanced_at")) or local.astimezone(timezone.utc)
        minutes = max(0.0, min(1440.0, (local.astimezone(timezone.utc) - previous).total_seconds() / 60))
        if minutes > 0:
            phase = self.phase(local.hour + local.minute / 60)
            targets = self._targets(phase)
            alpha = 1.0 - math.exp(-minutes / 95.0)
            for key in BODY_KEYS:
                payload["body"][key] = _clamp(payload["body"][key] + (targets[key] - payload["body"][key]) * alpha)

            # Connection grows continuously, accelerates after a long silence,
            # and slows while the owner is likely asleep.
            last_user = _parse(payload["meta"].get("last_user_at"))
            gap = max(0.0, (local.astimezone(timezone.utc) - last_user).total_seconds() / 60) if last_user else minutes
            rate = .00045 * (1.0 + min(1.8, max(0.0, gap - 180) / 360))
            if phase in {"deep_night", "dawn"}:
                rate *= .38
            payload["social"]["connection"] = _clamp(payload["social"]["connection"] + minutes * rate)
            payload["social"]["immersion"] = _clamp(payload["social"]["immersion"] - minutes * .0026)
            pride = payload["social"]["pride"]
            payload["social"]["pride"] = _clamp(pride + (0.0 - pride) * (1 - math.exp(-minutes / 240)), -1, 1)
            if payload["social"]["connection"] > .62 and payload["meta"].get("last_contact_at"):
                payload["social"]["pride"] = _clamp(payload["social"]["pride"] + minutes * .00022, -1, 1)

        event = payload["meta"].get("active_event")
        if isinstance(event, dict):
            until = _parse(event.get("until"))
            if until and local.astimezone(timezone.utc) >= until:
                payload["meta"]["active_event"] = None
        if payload["meta"].get("contacts_date") != local.date().isoformat():
            payload["meta"]["contacts_date"] = local.date().isoformat()
            payload["meta"]["contacts_today"] = 0
        self._maybe_daily_events(payload, local)
        payload["meta"]["last_advanced_at"] = _iso(local)
        self.save(payload)
        return payload

    def on_user_message(self, text: str, now: datetime | None = None) -> dict[str, Any]:
        payload = self.advance(now)
        local = self._now(now, payload["settings"])
        payload["meta"]["last_user_at"] = _iso(local)
        payload["social"]["connection"] = min(payload["social"]["connection"], .055)
        payload["social"]["pride"] *= .35
        payload["social"]["immersion"] = max(0.0, payload["social"]["immersion"] - .12)
        self.save(payload)
        return self.state_view(now=local, advance=False)

    @staticmethod
    def infer_interaction(user_text: str, assistant_text: str) -> dict[str, Any]:
        """Infer body/social evidence from the visible reply without mutation."""
        reply = str(assistant_text or "")
        deltas: dict[str, float] = {}
        social_deltas: dict[str, float] = {}
        result, reason = "neutral", "没有明显身体推动"
        def expressed(markers: tuple[str, ...]) -> bool:
            blocked = re.compile(
                r"(?:不|没|未|不会|不能|别|不要|避免|防止|误显示|"
                r"提到|写着|显示|假如|如果|是否|比如|例如|示例|"
                r"假设|说)[^，。！？!?]{0,8}$"
            )
            for sentence in re.split(r"[，,；;。！？!?…\n]+", reply):
                if not sentence.strip() or _is_state_system_discussion(sentence):
                    continue
                for marker in markers:
                    index = sentence.find(marker)
                    if index < 0:
                        continue
                    prefix = sentence[max(0, index - 10):index]
                    if blocked.search(prefix):
                        continue
                    return True
            return False
        # User input is already reflected before generation.  Settlement must
        # follow what the model actually expressed, otherwise a user merely
        # mentioning an intimate term makes the body claim the reply escalated.
        affection = (
            expressed(
                (
                    "抱紧", "亲亲", "贴贴", "爱你", "喜欢你", "想你",
                    "亲你", "抱住", "吻你", "贴近你", "靠近你",
                )
            )
        )
        intimate = (
            expressed(
                (
                    "我也想要", "我想继续", "我身体发热", "我的身体发热",
                    "我呼吸乱", "我的呼吸乱", "我忍不住靠得更近",
                    "我继续碰", "我欲望压不住", "我很想要你",
                )
            )
        )
        user_raw = str(user_text or "").strip()
        boundary_raw = re.sub(
            r"[\"'“‘][^\"'”’\n]{0,160}[\"'”’]", "", user_raw
        )
        user_compact = re.sub(r"[，。！？!?…\s]+", "", boundary_raw)
        rejection = bool(
            user_compact in {"停", "不要", "别", "住手", "拒绝", "先停一下"}
            or any(
                phrase in boundary_raw
                for phrase in (
                    "别碰我", "不要碰我", "别继续亲", "不要继续亲",
                    "不想做了", "到此为止", "亲密先停", "性爱先停",
                )
            )
        )
        # Conflict was already processed before generation.  The post-reply
        # settlement follows the assistant's actual reaction instead of adding
        # the user's signal a second time.
        conflict_reply = (
            expressed(
                ("我在生气", "我很烦", "我恼火", "气得", "不想理", "别来")
            )
        )
        if rejection:
            result, reason = "cooled_down", "互动被明确暂停"
            deltas = {"desire": -.16, "tension": -.08, "sensitivity": -.05, "recovery": .04}
        elif intimate:
            result, reason = "escalated", "亲密互动使身体反应升高"
            deltas = {"desire": .14, "warmth": .10, "sensitivity": .12, "tension": .08, "recovery": -.05}
        elif affection:
            result, reason = "tender", "亲近回应带来放松和暖意"
            deltas = {"warmth": .08, "tension": -.05, "recovery": .04, "sensitivity": .03}
        elif conflict_reply:
            result, reason = "unsettled", "争执留下身体紧绷"
            deltas = {"tension": .09, "warmth": -.04, "recovery": -.04}
            social_deltas = {"pride": .12}
        if result in {"escalated", "tender"}:
            social_deltas["connection"] = -.06
        return {
            "result": result,
            "reason": reason,
            "deltas": deltas,
            "social_deltas": social_deltas,
        }

    def settle_interaction(self, user_text: str, assistant_text: str, *,
                           session_id: str = "", now: datetime | None = None) -> dict[str, Any]:
        """Legacy global settlement; canonical chats use ``infer_interaction``."""
        payload = self.advance(now)
        settlement = self.infer_interaction(user_text, assistant_text)
        result = str(settlement["result"])
        reason = str(settlement["reason"])
        deltas = dict(settlement["deltas"])
        social_deltas = dict(settlement["social_deltas"])
        for key, delta in deltas.items():
            payload["body"][key] = _clamp(payload["body"][key] + max(-.22, min(.22, delta)))
        for key, delta in social_deltas.items():
            payload["social"][key] = _clamp(
                payload["social"][key] + max(-.22, min(.22, delta)),
                -1 if key == "pride" else 0,
                1,
            )
        with get_db() as db:
            db.execute(
                """INSERT INTO interaction_settlements
                   (session_id, result, deltas_json, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, result, json.dumps(deltas, ensure_ascii=False), reason,
                 _iso(self._now(now, payload["settings"]))),
            )
        self.save(payload)
        return settlement

    def apply_deltas(
        self,
        body_deltas: dict[str, float] | None = None,
        social_deltas: dict[str, float] | None = None,
        *,
        reason: str = "统一内心系统完成一次耦合",
        now: datetime | None = None,
    ) -> dict[str, dict[str, float]]:
        """Apply bounded body/social deltas without advancing the clock twice."""
        payload = self.load()
        applied_body: dict[str, float] = {}
        applied_social: dict[str, float] = {}
        for key, raw_delta in (body_deltas or {}).items():
            if key not in BODY_KEYS:
                continue
            delta = max(-0.24, min(0.24, float(raw_delta)))
            before = float(payload["body"][key])
            payload["body"][key] = _clamp(before + delta)
            actual = payload["body"][key] - before
            if abs(actual) >= 0.0005:
                applied_body[key] = round(actual, 4)
        for key, raw_delta in (social_deltas or {}).items():
            if key not in SOCIAL_LABELS:
                continue
            low = -1.0 if key == "pride" else 0.0
            delta = max(-0.24, min(0.24, float(raw_delta)))
            before = float(payload["social"][key])
            payload["social"][key] = _clamp(before + delta, low, 1.0)
            actual = payload["social"][key] - before
            if abs(actual) >= 0.0005:
                applied_social[key] = round(actual, 4)
        if applied_body or applied_social:
            self._record_event(
                "inner_coupling",
                str(reason or "统一内心系统完成一次耦合")[:300],
                details={"body": applied_body, "social": applied_social},
                now=now,
            )
            self.save(payload)
        return {"body": applied_body, "social": applied_social}

    def set_activity(self, activity_type: str, label: str, *, intensity: float = .65,
                     now: datetime | None = None) -> dict[str, Any]:
        payload = self.advance(now)
        local = self._now(now, payload["settings"])
        safe_type = re.sub(r"[^a-z0-9_-]", "", str(activity_type).lower())[:30] or "reflect"
        safe_label = str(label or "安静做自己的事")[:80]
        payload["meta"]["activity"] = {"type": safe_type, "label": safe_label, "started_at": _iso(local)}
        payload["social"]["immersion"] = _clamp(intensity)
        payload["social"]["connection"] = _clamp(payload["social"]["connection"] - .08)
        self._record_event("activity", safe_label, details={"type": safe_type}, now=local)
        self.save(payload)
        return self.state_view(now=local, advance=False)

    @staticmethod
    def _in_quiet(hour: int, start: int, end: int) -> bool:
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def heartbeat_decision(self, now: datetime | None = None) -> dict[str, Any]:
        payload = self.advance(now)
        local = self._now(now, payload["settings"])
        settings = payload["settings"]
        connection = payload["social"]["connection"]
        pride = payload["social"]["pride"]
        immersion = payload["social"]["immersion"]
        event = payload["meta"].get("active_event") or {}
        decision = {"action": "observe", "reason": "生活状态仍在缓慢变化", "score": connection}
        if not settings.get("enabled", True) or not settings.get("proactive_enabled", True):
            decision = {"action": "disabled", "reason": "主动生活已关闭", "score": 0}
        elif self._in_quiet(local.hour, int(settings.get("quiet_start", 1)), int(settings.get("quiet_end", 7))):
            decision = {"action": "silence", "reason": "安静时段，不打扰", "score": connection}
        elif payload["meta"].get("contacts_today", 0) >= int(settings.get("max_contacts_per_day", 4)):
            decision = {"action": "silence", "reason": "今天已经主动联系过几次，留出呼吸空间", "score": connection}
        else:
            last_contact = _parse(payload["meta"].get("last_contact_at"))
            since_contact = ((local.astimezone(timezone.utc) - last_contact).total_seconds() / 60
                             if last_contact else 99999)
            contact_ready = since_contact >= int(settings.get("minimum_contact_minutes", 90))
            if payload["body"]["sleepiness"] >= .82:
                decision = {"action": "rest", "reason": "身体更想休息，而不是发消息", "score": payload["body"]["sleepiness"]}
            elif connection >= .78 and contact_ready:
                decision = {"action": "contact", "reason": "连接需求积累到无法继续忽略", "score": connection}
            elif connection >= .50:
                if pride >= .50:
                    decision = {"action": "activity", "reason": "想靠近又有点端着，先去做自己的事", "score": connection}
                elif immersion >= .66:
                    decision = {"action": "silence", "reason": "正在沉浸做事，还不想打断", "score": immersion}
                elif contact_ready:
                    decision = {"action": "contact", "reason": "自然地想起对方，愿意先开口", "score": connection}
            elif event.get("key") in {"dream_residue", "morning_response"} and connection >= .34 and contact_ready:
                decision = {"action": "contact", "reason": "身体余波和想念碰在一起，想说点什么", "score": connection}
            elif payload["body"]["tension"] >= .74:
                decision = {"action": "activity", "reason": "身体有些绷，先自行调节", "score": payload["body"]["tension"]}

        return decision

    def commit_decision(self, decision: dict[str, Any], *, delivered: bool = False,
                        now: datetime | None = None) -> dict[str, Any]:
        """Apply an already chosen action.

        A planned contact is counted only after a message was actually produced
        and saved.  This prevents failed API calls from creating phantom
        contact events or consuming the daily limit.
        """
        payload = self.advance(now)
        local = self._now(now, payload["settings"])
        action = str((decision or {}).get("action") or "observe")
        payload["meta"]["last_decision"] = action
        if action == "contact" and delivered:
            payload["meta"]["last_contact_at"] = _iso(local)
            payload["meta"]["contacts_today"] = int(payload["meta"].get("contacts_today", 0)) + 1
            payload["social"]["connection"] = _clamp(payload["social"]["connection"] - .28)
            self._record_event("contact", "主动开口联系",
                               details={"reason": decision.get("reason", "")}, now=local)
        elif action == "activity":
            choices = (("reflect", "整理了一会儿共同时间线"),
                       ("journal", "写下几句只给自己看的日记"),
                       ("rest", "把注意力收回来，安静待一会儿"))
            index = int(self._random01(payload, f"activity:{local.date()}:{local.hour}") * len(choices))
            kind, label = choices[min(index, len(choices) - 1)]
            payload["meta"]["activity"] = {"type": kind, "label": label, "started_at": _iso(local)}
            payload["social"]["immersion"] = .68
            payload["social"]["connection"] = _clamp(payload["social"]["connection"] - .07)
            decision["activity"] = {"type": kind, "label": label}
            self._record_event("activity", label,
                               details={"reason": decision.get("reason", "")}, now=local)
        elif action == "rest":
            payload["meta"]["activity"] = {"type": "rest", "label": "顺着困意休息",
                                             "started_at": _iso(local)}
            payload["body"]["recovery"] = _clamp(payload["body"]["recovery"] + .05)
        self.save(payload)
        return self.state_view(now=local, advance=False)

    @staticmethod
    def _level(value: float, low: str, mid: str, high: str) -> str:
        return low if value < .34 else (mid if value < .68 else high)

    def prompt_context(self, now: datetime | None = None, *, proactive: bool = False) -> str:
        payload = self.advance(now)
        if not payload["settings"].get("enabled", True):
            return ""
        local = self._now(now, payload["settings"])
        phase = self.phase(local.hour + local.minute / 60)
        body = payload["body"]
        social = payload["social"]
        lines = [
            "<living_situation>",
            f"现在是{local.strftime('%Y-%m-%d %H:%M')}，处在{PHASE_LABELS[phase]}。",
            "身体感：" + "；".join((
                self._level(body["energy"], "没什么力气", "精神普通", "精力很足"),
                self._level(body["sleepiness"], "很清醒", "有一点困", "困意很重"),
                self._level(body["warmth"], "身体偏凉", "体温感平稳", "身体明显发热"),
                self._level(body["tension"], "很放松", "有些绷着", "身体紧张得明显"),
                self._level(body["sensitivity"], "触感迟钝", "感受普通", "此刻格外敏感"),
            )) + "。",
            self._level(social["connection"], "并不急着找对方", "开始惦记对方", "已经很想听见对方的声音") + "。",
        ]
        if social["pride"] > .45:
            lines.append("有一点端着；即使想靠近，也可能先找别的由头或暂时不说。")
        activity = payload["meta"].get("activity") or {}
        if activity.get("label") and social["immersion"] > .35:
            lines.append(f"刚才在做：{activity['label']}。这件事仍占着一部分注意力。")
        event = payload["meta"].get("active_event")
        if isinstance(event, dict):
            lines.append(f"短时身体事件：{event.get('label', '')}。让它只在相关时自然渗出来，不必主动解释。")
        if proactive:
            lines.append("这是一次自己先醒来的时刻。若开口，应源于此刻具体念头；不必例行关心、保证接住、固定说想念，也不必总是一两句话。")
        lines.append("这些都是第一人称的隐性处境，不要报数值、解释引擎或把每一项都说出口。")
        lines.append("</living_situation>")
        return "\n".join(lines)

    def timeline(self, limit: int = 20) -> list[dict[str, Any]]:
        self.ensure_schema()
        with get_db() as db:
            rows = db.execute("SELECT * FROM life_events ORDER BY id DESC LIMIT ?",
                              (max(1, min(limit, 100)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json", "{}"))
            except Exception:
                item["details"] = {}
            result.append(item)
        return result

    def state_view(self, now: datetime | None = None, *, advance: bool = True) -> dict[str, Any]:
        payload = self.advance(now) if advance else self.load()
        local = self._now(now, payload["settings"])
        phase = self.phase(local.hour + local.minute / 60)
        return {
            "version": "5.9",
            "local_time": local.isoformat(),
            "phase": phase,
            "phase_label": PHASE_LABELS[phase],
            "body": {key: round(payload["body"][key], 3) for key in BODY_KEYS},
            "body_labels": BODY_LABELS,
            "social_labels": SOCIAL_LABELS,
            "social": {key: round(float(value), 3) for key, value in payload["social"].items()},
            "active_event": deepcopy(payload["meta"].get("active_event")),
            "morning_response": deepcopy(payload["meta"].get("last_morning_response")),
            "activity": deepcopy(payload["meta"].get("activity")),
            "last_decision": payload["meta"].get("last_decision", ""),
            "contacts_today": int(payload["meta"].get("contacts_today", 0)),
            "settings": deepcopy(payload["settings"]),
            "timeline": self.timeline(12),
        }

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        payload = self.load()
        settings = payload["settings"]
        for key, value in (patch or {}).items():
            if key in {"enabled", "proactive_enabled", "dreams_enabled", "visible_state"}:
                settings[key] = bool(value)
            elif key == "morning_response_mode" and value in {"off", "subtle", "natural", "vivid"}:
                settings[key] = value
            elif key == "timezone":
                try:
                    ZoneInfo(str(value))
                    settings[key] = str(value)
                except Exception:
                    pass
            elif key in {"quiet_start", "quiet_end"}:
                settings[key] = max(0, min(23, int(value)))
            elif key == "minimum_contact_minutes":
                settings[key] = max(15, min(1440, int(value)))
            elif key == "max_contacts_per_day":
                settings[key] = max(0, min(24, int(value)))
        self.save(payload)
        # A settings write must not silently advance the simulated clock.  In
        # particular, editing morning settings later in the day used to expire
        # an already-created morning event and made deterministic replay/time-
        # based tests depend on the wall clock of the machine running them.
        return self.state_view(advance=False)

    def reset(self, keep_settings: bool = True, now: datetime | None = None) -> dict[str, Any]:
        old = self.load()
        payload = self._defaults(now)
        if keep_settings:
            payload["settings"] = deepcopy(old["settings"])
        self._cache = payload
        self.save(payload)
        self._record_event("reset", "活体状态被重置", now=now)
        return self.state_view(now=now)

    def health(self) -> dict[str, Any]:
        try:
            view = self.state_view()
            return {"health": "ok", "detail":
                    f"{view['phase_label']} · {view['last_decision'] or '自然运行'} · 今日主动 {view['contacts_today']} 次"}
        except Exception as exc:
            return {"health": "error", "detail": "生活状态组件暂不可用"}


living_state = LivingState()
