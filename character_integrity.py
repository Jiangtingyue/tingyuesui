"""Character continuity, expression fatigue, and local persona checks.

This module never rewrites model output.  It notices recently overused wording
and gives the *next* native model turn a small temporary nudge, preserving the
actual Claude/GPT voice instead of post-processing it into house style.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from config import (
    CHARACTER_CONFIG, SYSTEM_PROMPT_BLOCKS, USER_NAME, USER_NICKNAME,
)
from models import get_db


DEFAULT_SETTINGS = {
    "enabled": True,
    "native_voice": True,
    "phrase_fatigue": True,
    "punctuation_fatigue": True,
    "history_messages": 24,
    "cooldown_messages": 10,
    "dash_soft_limit": 2,
    "watch_phrases": ["我狠狠干", "稳稳接住你"],
}

_COMMON_FRAGMENTS = {
    "我知道", "你现在", "这件事情", "如果你愿意", "没有关系", "我觉得",
    "所以现在", "但是这个", "不是因为", "而是因为", "可以直接", "我们可以",
}

_IDENTITY_DRIFT_PATTERNS = (
    re.compile(r"我(?:其实|本来)?是(?:一只|一个|一颗|颗)?\s*大西瓜"),
    re.compile(r"作为(?:一只|一个|一颗|颗)?\s*大西瓜"),
    re.compile(r"(?:本|这只|这颗)西瓜(?:会|想|要|也|正在|觉得|知道|陪)"),
)

_REVERSED_ADDRESS_PATTERN = re.compile(
    rf"(?:^|[\n。！？!?])\s*(?:(?:{re.escape(USER_NAME)}|{re.escape(USER_NICKNAME)})"
    r"[，,、 ]*)?(?:我的)?主人(?:[呀啊呢哦哟～~！!，,:：]|$)"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CharacterIntegrity:
    def __init__(self) -> None:
        self._settings: dict[str, Any] | None = None

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS character_integrity_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS style_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_id INTEGER,
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    dash_count INTEGER DEFAULT 0,
                    watch_hits_json TEXT DEFAULT '[]',
                    repeated_json TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_style_audit_session
                    ON style_audits(session_id, id DESC);
                """
            )

    def load_settings(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._settings is not None and not refresh:
            return self._settings
        with get_db() as db:
            row = db.execute(
                "SELECT settings_json FROM character_integrity_state WHERE id=1"
            ).fetchone()
        stored: dict[str, Any] = {}
        if row:
            try:
                value = json.loads(row["settings_json"] or "{}")
                if isinstance(value, dict):
                    stored = value
            except Exception:
                stored = {}
        configured = {
            "enabled": bool(CHARACTER_CONFIG.get("enabled", True)),
            "phrase_fatigue": bool(CHARACTER_CONFIG.get("phrase_fatigue", True)),
            "history_messages": int(CHARACTER_CONFIG.get("history_messages", 24)),
            "cooldown_messages": int(CHARACTER_CONFIG.get("cooldown_messages", 10)),
            "dash_soft_limit": int(CHARACTER_CONFIG.get("dash_soft_limit", 2)),
            "watch_phrases": list(CHARACTER_CONFIG.get("watch_phrases", [])),
        }
        settings = {**DEFAULT_SETTINGS, **configured, **stored}
        settings["watch_phrases"] = self._clean_phrases(settings.get("watch_phrases"))
        self._settings = settings
        if not row:
            self.save_settings(settings)
        return settings

    @staticmethod
    def _clean_phrases(value: Any) -> list[str]:
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for raw in value:
            phrase = re.sub(r"\s+", "", str(raw or "")).strip("，。！？；：,.!?;:—- ")
            if 2 <= len(phrase) <= 30 and phrase not in result:
                result.append(phrase)
        return result[:30]

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.ensure_schema()
        with get_db() as db:
            db.execute(
                """INSERT INTO character_integrity_state(id, settings_json, updated_at)
                   VALUES(1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(settings, ensure_ascii=False), _utcnow()),
            )
        self._settings = settings

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        settings = deepcopy(self.load_settings())
        for key, value in (patch or {}).items():
            if key in {"enabled", "native_voice", "phrase_fatigue", "punctuation_fatigue"}:
                settings[key] = bool(value)
            elif key == "history_messages":
                settings[key] = max(6, min(int(value), 80))
            elif key == "cooldown_messages":
                settings[key] = max(2, min(int(value), 40))
            elif key == "dash_soft_limit":
                settings[key] = max(0, min(int(value), 12))
            elif key == "watch_phrases":
                settings[key] = self._clean_phrases(value)
        self.save_settings(settings)
        return self.state_view("")

    def _recent_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with get_db() as db:
            rows = db.execute(
                """SELECT id, content, provider, model, created_at FROM messages
                   WHERE session_id=? AND role='assistant'
                   ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _dash_count(text: str) -> int:
        return sum(max(1, len(match.group(0))) for match in re.finditer(r"—+", text or ""))

    @staticmethod
    def _segments(text: str) -> list[str]:
        return [
            item for item in re.findall(r"[\u3400-\u9fffA-Za-z0-9]{5,48}", text or "")
            if len(item) >= 5
        ]

    @staticmethod
    def _match_excerpt(text: str, start: int, end: int, radius: int = 34) -> str:
        source = str(text or "")
        if not source:
            return ""
        left = max(0, start - radius)
        right = min(len(source), end + radius)
        excerpt = re.sub(r"\s+", " ", source[left:right]).strip()
        return ("…" if left else "") + excerpt + ("…" if right < len(source) else "")

    def analyze_text(self, text: str) -> dict[str, Any]:
        """Inspect saved visible text only; never call a model or rewrite output."""
        source = str(text or "")
        settings = self.load_settings()
        identity_hits: list[dict[str, str]] = []
        for pattern in _IDENTITY_DRIFT_PATTERNS:
            for match in pattern.finditer(source):
                identity_hits.append({
                    "match": match.group(0),
                    "excerpt": self._match_excerpt(source, match.start(), match.end()),
                })
        address_hits = [
            {
                "match": match.group(0).strip(),
                "excerpt": self._match_excerpt(source, match.start(), match.end()),
            }
            for match in _REVERSED_ADDRESS_PATTERN.finditer(source)
        ]
        watch_hits = [
            phrase for phrase in settings.get("watch_phrases", [])
            if phrase and phrase in source
        ]
        dash_count = self._dash_count(source)
        dash_limit = int(settings.get("dash_soft_limit", 2))
        return {
            "identity_hits": identity_hits,
            "address_hits": address_hits,
            "watch_hits": watch_hits,
            "dash_count": dash_count,
            "dash_over_limit": max(0, dash_count - dash_limit),
        }

    def _repeated_phrases(self, texts: list[str]) -> list[str]:
        # Count support across distinct messages so one long repeated sound does
        # not dominate.  Longest overlapping candidate wins.
        support: dict[str, set[int]] = defaultdict(set)
        totals: Counter[str] = Counter()
        for message_index, text in enumerate(texts):
            seen: set[str] = set()
            for segment in self._segments(text[:2400]):
                for size in range(5, min(12, len(segment)) + 1):
                    for start in range(0, len(segment) - size + 1):
                        gram = segment[start:start + size]
                        if gram in _COMMON_FRAGMENTS:
                            continue
                        totals[gram] += 1
                        seen.add(gram)
            for gram in seen:
                support[gram].add(message_index)
        candidates = [
            gram for gram, message_ids in support.items()
            if len(message_ids) >= 2 and totals[gram] >= 2
        ]
        candidates.sort(key=lambda gram: (len(support[gram]), totals[gram], len(gram)), reverse=True)
        selected: list[str] = []
        for gram in candidates:
            if any(gram in old or old in gram for old in selected):
                continue
            selected.append(gram)
            if len(selected) >= 6:
                break
        return selected

    def analyze(self, session_id: str) -> dict[str, Any]:
        settings = self.load_settings()
        limit = int(settings.get("history_messages", 24))
        recent = self._recent_messages(session_id, limit)
        texts = [str(item.get("content") or "") for item in recent]
        cooldown = texts[:int(settings.get("cooldown_messages", 10))]
        watched = [
            phrase for phrase in settings.get("watch_phrases", [])
            if any(phrase in text for text in cooldown)
        ]
        repeated = self._repeated_phrases(texts) if settings.get("phrase_fatigue") else []
        dash_counts = [self._dash_count(text) for text in texts[:8]]
        dash_tired = bool(
            settings.get("punctuation_fatigue") and dash_counts
            and (sum(dash_counts) / len(dash_counts) > float(settings.get("dash_soft_limit", 2))
                 or dash_counts[0] > int(settings.get("dash_soft_limit", 2)))
        )
        return {
            "message_count": len(texts),
            "watch_phrases": list(settings.get("watch_phrases", [])),
            "active_watch_phrases": watched,
            "repeated_phrases": repeated,
            "dash_counts": dash_counts,
            "dash_tired": dash_tired,
        }

    def prompt_context(self, session_id: str, provider: str = "") -> str:
        settings = self.load_settings()
        if not settings.get("enabled", True):
            return ""
        report = self.analyze(session_id)
        tired = (
            list(dict.fromkeys(report["active_watch_phrases"] + report["repeated_phrases"]))[:8]
            if settings.get("phrase_fatigue") else []
        )
        lines = [
            "<expression_integrity>",
            "保留当前模型自己的自然声音、判断和节奏；不要模仿历史回复的表面措辞，也不要为了符合前端而改变人格。",
            "不要寻找新的固定口号来替换旧口号。相似情绪可以用完全不同的句法、行动或干脆留白表达。",
        ]
        if tired:
            lines.append("近期已经出现偏多、暂时进入冷却的表达：" + "、".join(tired) + "。这只是短期降频，不是永久禁词。")
        if settings.get("punctuation_fatigue"):
            if report["dash_tired"]:
                lines.append("近期破折号密度偏高；本轮优先使用句号、逗号、冒号、括号或自然换行，只有确有插入/转折语义时才用破折号。")
            else:
                lines.append(f"破折号不是默认节奏；通常把整轮控制在 {int(settings.get('dash_soft_limit', 2))} 处以内。")
        lines.append("不要在回复里提及表达检测、冷却词、模型供应商或这些内部说明。")
        lines.append("</expression_integrity>")
        return "\n".join(lines)

    def audit_response(self, session_id: str, message_id: int | None, text: str,
                       provider: str = "", model: str = "") -> dict[str, Any]:
        settings = self.load_settings()
        watch_hits = [p for p in settings.get("watch_phrases", []) if p in (text or "")]
        result = {"dash_count": self._dash_count(text), "watch_hits": watch_hits}
        with get_db() as db:
            db.execute(
                """INSERT INTO style_audits
                   (session_id, message_id, provider, model, dash_count,
                    watch_hits_json, repeated_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, '[]', ?)""",
                (session_id, message_id, provider, model, result["dash_count"],
                 json.dumps(watch_hits, ensure_ascii=False), _utcnow()),
            )
        return result

    def recent_audits(self, session_id: str = "", limit: int = 12) -> list[dict[str, Any]]:
        self.ensure_schema()
        with get_db() as db:
            if session_id:
                rows = db.execute(
                    "SELECT * FROM style_audits WHERE session_id=? ORDER BY id DESC LIMIT ?",
                    (session_id, max(1, min(limit, 100))),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM style_audits ORDER BY id DESC LIMIT ?",
                    (max(1, min(limit, 100)),),
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["watch_hits"] = json.loads(item.pop("watch_hits_json", "[]"))
            except Exception:
                item["watch_hits"] = []
            item.pop("repeated_json", None)
            result.append(item)
        return result

    def lab_view(self, session_id: str = "", limit: int = 20) -> dict[str, Any]:
        """Return an explainable, offline stability receipt for recent replies."""
        settings = self.load_settings()
        recent = self._recent_messages(session_id, max(1, min(int(limit), 60)))
        metrics = {
            "messages": len(recent),
            "identity_drift": 0,
            "address_reversal": 0,
            "watch_hits": 0,
            "dash_over_limit": 0,
        }
        issues: list[dict[str, Any]] = []
        providers: Counter[str] = Counter()
        for item in recent:
            report = self.analyze_text(str(item.get("content") or ""))
            provider = str(item.get("provider") or "unknown")
            providers[provider] += 1
            metrics["identity_drift"] += len(report["identity_hits"])
            metrics["address_reversal"] += len(report["address_hits"])
            metrics["watch_hits"] += len(report["watch_hits"])
            metrics["dash_over_limit"] += int(report["dash_over_limit"])
            common = {
                "message_id": int(item.get("id") or 0),
                "provider": provider,
                "model": str(item.get("model") or ""),
                "created_at": item.get("created_at"),
            }
            for hit in report["identity_hits"]:
                issues.append({**common, "kind": "identity", "severity": "high",
                               "label": "把应用名当成自我身份", "excerpt": hit["excerpt"]})
            for hit in report["address_hits"]:
                issues.append({**common, "kind": "address", "severity": "high",
                               "label": "关系称呼方向疑似颠倒", "excerpt": hit["excerpt"]})
            if report["watch_hits"]:
                issues.append({**common, "kind": "phrase", "severity": "medium",
                               "label": "观察词再次出现",
                               "excerpt": "、".join(report["watch_hits"][:4])})
            if report["dash_over_limit"]:
                issues.append({**common, "kind": "dash", "severity": "low",
                               "label": "破折号超过本轮软上限",
                               "excerpt": f"共 {report['dash_count']} 处，超过 {report['dash_over_limit']} 处"})

        penalty = (
            metrics["identity_drift"] * 18
            + metrics["address_reversal"] * 16
            + metrics["watch_hits"] * 3
            + min(metrics["dash_over_limit"], 20)
        )
        score = None if not recent else max(0, min(100, 100 - penalty))
        persona = str(SYSTEM_PROMPT_BLOCKS.get("persona") or "")
        contracts = [
            {
                "key": "app_identity",
                "label": "应用名不等于自我身份",
                "ok": "应用名称" in persona and "不是你的姓名" in persona,
            },
            {
                "key": "address_direction",
                "label": f"不会反向称{USER_NICKNAME}为主人",
                "ok": f"反向称呼{USER_NICKNAME}为主人" in persona,
            },
            {
                "key": "native_voice",
                "label": "保留 Claude / GPT 原生性格",
                "ok": bool(settings.get("native_voice", True)),
            },
            {
                "key": "untouched_output",
                "label": "自检不改模型原文",
                "ok": True,
            },
        ]
        return {
            "offline": True,
            "sample_limit": max(1, min(int(limit), 60)),
            "sample_count": len(recent),
            "score": score,
            "metrics": metrics,
            "contracts": contracts,
            "issues": issues[:24],
            "provider_breakdown": dict(providers),
        }

    def state_view(self, session_id: str = "") -> dict[str, Any]:
        return {"settings": deepcopy(self.load_settings()),
                "analysis": self.analyze(session_id),
                "recent_audits": self.recent_audits(session_id, 8),
                "lab": self.lab_view(session_id, 20)}

    def health(self) -> dict[str, Any]:
        settings = self.load_settings()
        return {"health": "ok", "detail":
                f"原生声音 {'开启' if settings.get('native_voice') else '关闭'} · 表达疲劳 {'开启' if settings.get('phrase_fatigue') else '关闭'}"}


character_integrity = CharacterIntegrity()
