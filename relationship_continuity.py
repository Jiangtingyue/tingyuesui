"""大西瓜 v6.0 relationship continuity engine.

The engine stores relationship evidence, unfinished threads and a small shared
world without asking another model to rewrite the conversation.  Everything
that enters a model prompt is either an exact source excerpt or user-edited
local data.  It deliberately does not post-process Claude/GPT output.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from config import RELATIONSHIP_CONFIG
from memory_archive import exact_quote, query_terms
from models import get_db


AXIS_KEYS = ("familiarity", "warmth", "playfulness", "steadiness", "tension")
AXIS_LABELS = {
    "familiarity": "熟悉感",
    "warmth": "温度",
    "playfulness": "玩心",
    "steadiness": "安定感",
    "tension": "未消化的张力",
}
DEFAULT_AXES = {
    "familiarity": .08,
    "warmth": .34,
    "playfulness": .20,
    "steadiness": .28,
    "tension": .02,
}
DEFAULT_SETTINGS = {
    "enabled": True,
    "auto_threads": True,
    "shared_context": True,
    "moment_capture": True,
    "context_thread_limit": 5,
    "context_shared_limit": 5,
}

_POSITIVE = ("谢谢", "喜欢", "爱你", "抱抱", "开心", "厉害", "可爱", "安心", "想你", "💕", "❤")
_PLAYFUL = ("哈哈", "嘿嘿", "笑死", "逗", "好玩", "玩玩", "游戏", "调皮", "笨蛋", "嘻嘻")
_TENSION = ("生气", "讨厌", "烦", "失望", "难过", "委屈", "别这样", "不要继续", "不理你")
_REPAIR = ("对不起", "没关系", "原谅", "和好", "不生气了", "抱抱就好", "说开了")
_MILESTONE_RULES = (
    ("promise", "我们留下了一项约定", re.compile(r"(?:答应我|我答应你|我们约好|说好了|别忘了)")),
    ("repair", "我们把一件事说开了", re.compile(r"(?:和好|原谅你|原谅我|不生气了|说开了)")),
    ("first", "我们有了一个第一次", re.compile(r"(?:第一次|头一次)")),
    ("milestone", "这件事对我们有特别意义", re.compile(r"(?:纪念日|终于|特别重要|一定要记住)")),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _json(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, type(fallback)):
        return raw
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(fallback)) else deepcopy(fallback)
    except Exception:
        return deepcopy(fallback)


class RelationshipContinuity:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS relationship_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    axes_json TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuity_threads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'unfinished',
                    title TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    importance REAL NOT NULL DEFAULT .6,
                    owner TEXT NOT NULL DEFAULT 'together',
                    source_session_id TEXT DEFAULT '',
                    source_message_id INTEGER,
                    source_excerpt TEXT DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_continuity_threads_status
                    ON continuity_threads(status, importance DESC, id DESC);
                CREATE TABLE IF NOT EXISTS shared_world_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT 'object',
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    source_session_id TEXT DEFAULT '',
                    source_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shared_world_status
                    ON shared_world_items(status, id DESC);
                CREATE TABLE IF NOT EXISTS relationship_moments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_session_id TEXT DEFAULT '',
                    user_message_id INTEGER,
                    assistant_message_id INTEGER,
                    salience REAL NOT NULL DEFAULT .5,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relationship_moments
                    ON relationship_moments(id DESC);
                CREATE TABLE IF NOT EXISTS relationship_foundation (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    content TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def get_foundation(self) -> dict[str, Any]:
        """Return the user-authored relationship truth, independent of chats."""
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM relationship_foundation WHERE id=1"
            ).fetchone()
        if row:
            item = dict(row)
            content = str(item.get("content") or "")
            enabled = bool(item.get("enabled"))
            updated_at = item.get("updated_at") or ""
        else:
            content, enabled, updated_at = "", True, ""
        always_chars = max(0, int(RELATIONSHIP_CONFIG.get("foundation_always_chars", 4000)))
        recall_chars = max(0, int(RELATIONSHIP_CONFIG.get("foundation_recall_chars", 2200)))
        return {
            "content": content,
            "enabled": enabled,
            "updated_at": updated_at,
            "character_count": len(content),
            "token_estimate": math.ceil(len(content) / 2.4) if content else 0,
            "prompt_strategy": {
                "always_chars": always_chars,
                "recall_chars": recall_chars,
                "description": f"前 {always_chars} 字每轮常驻，其余按当前话题本机摘取",
            },
        }

    def update_foundation(
        self,
        content: str,
        *,
        enabled: bool = True,
        merge: str = "replace",
    ) -> dict[str, Any]:
        self.ensure_schema()
        text = str(content or "").replace("\x00", "").strip()
        current = self.get_foundation()
        if merge == "append" and current.get("content") and text:
            text = f"{current['content'].rstrip()}\n\n{text}"
        max_chars = max(1000, int(RELATIONSHIP_CONFIG.get("foundation_max_chars", 60000)))
        if len(text) > max_chars:
            raise ValueError(f"关系总记忆最多保存 {max_chars} 字，请先拆分或精简")
        with get_db() as db:
            db.execute(
                """INSERT INTO relationship_foundation(id, content, enabled, updated_at)
                   VALUES(1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET content=excluded.content,
                     enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (text, int(bool(enabled)), _now()),
            )
        return self.get_foundation()

    @staticmethod
    def _foundation_terms(query: str) -> list[str]:
        # Share the archive extractor so long natural-language questions keep
        # important nouns from the end instead of spending the whole budget on
        # leading Chinese n-grams.
        return query_terms(str(query or "").lower(), limit=32)

    def _foundation_prompt(self, query: str = "") -> str:
        foundation = self.get_foundation()
        content = foundation["content"]
        if not foundation["enabled"] or not content:
            return ""
        always_chars = foundation["prompt_strategy"]["always_chars"]
        recall_chars = foundation["prompt_strategy"]["recall_chars"]
        core = content[:always_chars]
        remainder = content[always_chars:]
        relevant: list[str] = []
        if remainder and recall_chars > 0:
            terms = self._foundation_terms(query)
            candidates: list[tuple[float, int, str]] = []
            for index, paragraph in enumerate(
                part.strip() for part in re.split(r"\n{2,}", remainder) if part.strip()
            ):
                lowered = paragraph.lower()
                score = sum(1 + min(4, lowered.count(term)) * .25 for term in terms if term in lowered)
                if score:
                    candidates.append((score, index, paragraph))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            used = 0
            for _score, _index, paragraph in candidates:
                remaining_chars = recall_chars - used
                if remaining_chars <= 0:
                    break
                excerpt = paragraph[:remaining_chars]
                relevant.append(excerpt)
                used += len(excerpt)
        parts = [
            "<relationship_foundation user_authored=\"true\">",
            "这是使用者亲自保存的关系总记忆；把明确陈述当作事实，但不要逐段复述。",
            core,
        ]
        if relevant:
            parts.extend(["\n与当前话题匹配的后续片段：", *relevant])
        parts.append("</relationship_foundation>")
        return "\n".join(part for part in parts if part)

    def _defaults(self) -> dict[str, Any]:
        return {
            "axes": deepcopy(DEFAULT_AXES),
            "settings": {
                **DEFAULT_SETTINGS,
                "enabled": bool(RELATIONSHIP_CONFIG.get("enabled", True)),
                "auto_threads": bool(RELATIONSHIP_CONFIG.get("auto_threads", True)),
                "shared_context": bool(RELATIONSHIP_CONFIG.get("shared_context", True)),
                "moment_capture": bool(RELATIONSHIP_CONFIG.get("moment_capture", True)),
                "context_thread_limit": int(RELATIONSHIP_CONFIG.get("context_thread_limit", 5)),
                "context_shared_limit": int(RELATIONSHIP_CONFIG.get("context_shared_limit", 5)),
            },
            "meta": {
                "interactions": 0,
                "first_seen_at": "",
                "last_interaction_at": "",
                "last_settlement": "尚未开始",
            },
        }

    def load(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._cache is not None and not refresh:
            return self._cache
        fallback = self._defaults()
        with get_db() as db:
            row = db.execute("SELECT * FROM relationship_state WHERE id=1").fetchone()
            counts = db.execute(
                "SELECT COUNT(*) total, MIN(created_at) first_at, MAX(created_at) last_at "
                "FROM messages WHERE role='assistant'"
            ).fetchone()
        if row:
            payload = {
                "axes": {**fallback["axes"], **_json(row["axes_json"], {})},
                "settings": {**fallback["settings"], **_json(row["settings_json"], {})},
                "meta": {**fallback["meta"], **_json(row["meta_json"], {})},
            }
        else:
            payload = fallback
            total = int(counts["total"] or 0)
            if total:
                payload["meta"].update({
                    "interactions": total,
                    "first_seen_at": counts["first_at"] or "",
                    "last_interaction_at": counts["last_at"] or "",
                    "last_settlement": "从已有对话接续",
                })
                payload["axes"]["familiarity"] = min(.82, .08 + math.log1p(total) / 8)
                payload["axes"]["steadiness"] = min(.72, .28 + math.log1p(total) / 15)
        for key in AXIS_KEYS:
            payload["axes"][key] = _clamp(payload["axes"].get(key, DEFAULT_AXES[key]))
        payload["settings"]["context_thread_limit"] = max(
            1, min(12, int(payload["settings"].get("context_thread_limit", 5)))
        )
        payload["settings"]["context_shared_limit"] = max(
            0, min(12, int(payload["settings"].get("context_shared_limit", 5)))
        )
        self._cache = payload
        if not row:
            self.save(payload)
        return payload

    def save(self, payload: dict[str, Any] | None = None) -> None:
        data = payload or self._cache or self._defaults()
        with get_db() as db:
            db.execute(
                """INSERT INTO relationship_state(id, axes_json, settings_json, meta_json, updated_at)
                   VALUES(1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET axes_json=excluded.axes_json,
                     settings_json=excluded.settings_json, meta_json=excluded.meta_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(data["axes"], ensure_ascii=False),
                 json.dumps(data["settings"], ensure_ascii=False),
                 json.dumps(data["meta"], ensure_ascii=False), _now()),
            )
        self._cache = data

    @staticmethod
    def _chapter(interactions: int) -> dict[str, str]:
        if interactions < 3:
            return {"key": "beginning", "label": "刚刚相遇", "note": "关系还在写下第一行"}
        if interactions < 20:
            return {"key": "learning", "label": "正在熟悉", "note": "彼此的习惯开始有了轮廓"}
        if interactions < 80:
            return {"key": "closer", "label": "渐渐靠近", "note": "共同经历正在变成默契"}
        if interactions < 220:
            return {"key": "attuned", "label": "有了默契", "note": "许多话已经不必从头解释"}
        return {"key": "enduring", "label": "长久相伴", "note": "这段关系已经拥有自己的历史"}

    @staticmethod
    def _axis_tone(key: str, value: float) -> str:
        if key == "tension":
            return "平静" if value < .18 else ("有一点余波" if value < .48 else "需要慢慢说开")
        if value < .24:
            return "刚刚萌芽"
        if value < .48:
            return "正在生长"
        if value < .74:
            return "清晰而稳定"
        return "已经很深"

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        payload = deepcopy(self.load())
        for key in ("enabled", "auto_threads", "shared_context", "moment_capture"):
            if key in patch:
                payload["settings"][key] = bool(patch[key])
        for key in ("context_thread_limit", "context_shared_limit"):
            if key in patch:
                payload["settings"][key] = max(0, min(12, int(patch[key])))
        self.save(payload)
        return self.state_view()

    def list_threads(
        self, status: str = "open", limit: int = 30, *,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        status = status if status in {"open", "paused", "resolved", "all"} else "open"
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if session_id is not None:
            clauses.append("source_session_id=?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with get_db() as db:
            rows = db.execute(
                f"SELECT * FROM continuity_threads {where} "
                "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, "
                "importance DESC, updated_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_thread(self, data: dict[str, Any], *, created_by: str = "manual") -> dict[str, Any]:
        self.ensure_schema()
        title = _clean_text(data.get("title"), 90)
        if not title:
            raise ValueError("未完之事需要一个标题")
        kind = str(data.get("kind") or "unfinished")
        if kind not in {"unfinished", "promise", "plan", "question", "care"}:
            kind = "unfinished"
        owner = str(data.get("owner") or "together")
        if owner not in {"user", "companion", "together"}:
            owner = "together"
        now = _now()
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO continuity_threads
                   (kind, title, detail, status, importance, owner,
                    source_session_id, source_message_id, source_excerpt,
                    created_by, created_at, updated_at)
                   VALUES(?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (kind, title, _clean_text(data.get("detail"), 500),
                 _clamp(data.get("importance", .65), .1, 1.0), owner,
                 _clean_text(data.get("source_session_id"), 120),
                 data.get("source_message_id"), _clean_text(data.get("source_excerpt"), 260),
                 "auto" if created_by == "auto" else "manual", now, now),
            )
            row = db.execute("SELECT * FROM continuity_threads WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def update_thread(self, thread_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_schema()
        with get_db() as db:
            current = db.execute("SELECT * FROM continuity_threads WHERE id=?", (int(thread_id),)).fetchone()
            if not current:
                return None
            item = dict(current)
            if "title" in patch:
                title = _clean_text(patch.get("title"), 90)
                if not title:
                    raise ValueError("标题不能为空")
                item["title"] = title
            if "detail" in patch:
                item["detail"] = _clean_text(patch.get("detail"), 500)
            if "status" in patch and patch.get("status") in {"open", "paused", "resolved"}:
                item["status"] = patch["status"]
            if "importance" in patch:
                item["importance"] = _clamp(patch["importance"], .1, 1.0)
            resolved_at = _now() if item["status"] == "resolved" else ""
            db.execute(
                """UPDATE continuity_threads SET title=?, detail=?, status=?, importance=?,
                   resolved_at=?, updated_at=? WHERE id=?""",
                (item["title"], item["detail"], item["status"], item["importance"],
                 resolved_at, _now(), int(thread_id)),
            )
            row = db.execute("SELECT * FROM continuity_threads WHERE id=?", (int(thread_id),)).fetchone()
        return dict(row)

    def delete_thread(self, thread_id: int) -> bool:
        self.ensure_schema()
        with get_db() as db:
            cursor = db.execute("DELETE FROM continuity_threads WHERE id=?", (int(thread_id),))
        return bool(cursor.rowcount)

    def list_shared(
        self, status: str = "active", limit: int = 40, *,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        status = status if status in {"active", "archived", "all"} else "active"
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if session_id is not None:
            clauses.append("source_session_id=?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with get_db() as db:
            rows = db.execute(
                f"SELECT * FROM shared_world_items {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def create_shared(self, data: dict[str, Any]) -> dict[str, Any]:
        self.ensure_schema()
        title = _clean_text(data.get("title"), 90)
        if not title:
            raise ValueError("共同空间里的东西需要一个名字")
        kind = str(data.get("kind") or "object")
        if kind not in {"object", "place", "ritual", "activity", "phrase", "wish"}:
            kind = "object"
        now = _now()
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO shared_world_items
                   (kind, title, description, status, source_session_id,
                    source_message_id, created_at, updated_at)
                   VALUES(?, ?, ?, 'active', ?, ?, ?, ?)""",
                (kind, title, _clean_text(data.get("description"), 600),
                 _clean_text(data.get("source_session_id"), 120), data.get("source_message_id"),
                 now, now),
            )
            row = db.execute("SELECT * FROM shared_world_items WHERE id=?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def update_shared(self, item_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_schema()
        with get_db() as db:
            current = db.execute("SELECT * FROM shared_world_items WHERE id=?", (int(item_id),)).fetchone()
            if not current:
                return None
            item = dict(current)
            if "title" in patch:
                title = _clean_text(patch.get("title"), 90)
                if not title:
                    raise ValueError("名称不能为空")
                item["title"] = title
            if "description" in patch:
                item["description"] = _clean_text(patch.get("description"), 600)
            if patch.get("status") in {"active", "archived"}:
                item["status"] = patch["status"]
            db.execute(
                "UPDATE shared_world_items SET title=?, description=?, status=?, updated_at=? WHERE id=?",
                (item["title"], item["description"], item["status"], _now(), int(item_id)),
            )
            row = db.execute("SELECT * FROM shared_world_items WHERE id=?", (int(item_id),)).fetchone()
        return dict(row)

    def delete_shared(self, item_id: int) -> bool:
        self.ensure_schema()
        with get_db() as db:
            cursor = db.execute("DELETE FROM shared_world_items WHERE id=?", (int(item_id),))
        return bool(cursor.rowcount)

    def recent_moments(
        self, limit: int = 20, *, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        where = "WHERE source_session_id=?" if session_id is not None else ""
        params: list[Any] = [session_id] if session_id is not None else []
        params.append(max(1, min(int(limit), 100)))
        with get_db() as db:
            rows = db.execute(
                f"SELECT * FROM relationship_moments {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _thread_candidate(text: str, owner: str) -> dict[str, Any] | None:
        source = _clean_text(text, 900)
        if not source:
            return None
        rules = (
            ("promise", re.compile(r"(?:答应我|我答应你|别忘了|记得要?)([^。！？!?\n]{2,58})")),
            ("plan", re.compile(r"(?:我们(?:下次|明天|以后)?(?:一起|要|得|约好)|下次我们)([^。！？!?\n]{2,58})")),
            ("plan", re.compile(r"((?:明天|下次|以后|等[^，。！？!?\n]{1,18}的时候)[^。！？!?\n]{3,58})")),
        )
        for kind, pattern in rules:
            match = pattern.search(source)
            if not match:
                continue
            raw = match.group(0).strip(" ，,。.!！?？")
            if len(raw) < 5:
                continue
            return {
                "kind": kind,
                "title": raw[:72],
                "detail": "从对话原话中捕捉，完成后可以在“我们”里收起。",
                "owner": owner,
                "importance": .72 if kind == "promise" else .62,
                "source_excerpt": exact_quote(text, 240),
            }
        return None

    def _maybe_add_thread(self, candidate: dict[str, Any], *, session_id: str,
                          message_id: int | None) -> dict[str, Any] | None:
        title = _clean_text(candidate.get("title"), 90)
        compact = re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", title)
        if len(compact) < 4:
            return None
        with get_db() as db:
            rows = db.execute(
                "SELECT title FROM continuity_threads WHERE status IN ('open','paused') ORDER BY id DESC LIMIT 40"
            ).fetchall()
        for row in rows:
            old = re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", row["title"] or "")
            if compact == old or (len(compact) >= 8 and (compact in old or old in compact)):
                return None
        return self.create_thread({
            **candidate,
            "source_session_id": session_id,
            "source_message_id": message_id,
        }, created_by="auto")

    def record_turn(self, *, session_id: str, user_text: str, assistant_text: str,
                    user_message_id: int | None = None,
                    assistant_message_id: int | None = None) -> dict[str, Any]:
        """Settle a successful conversational turn without another model call."""
        payload = deepcopy(self.load())
        if not payload["settings"].get("enabled", True):
            return {"enabled": False, "threads_added": [], "moment": None}

        combined = f"{user_text}\n{assistant_text}"
        axes = payload["axes"]
        axes["familiarity"] = _clamp(axes["familiarity"] + .0045)
        axes["steadiness"] = _clamp(axes["steadiness"] + .002)
        axes["tension"] = _clamp(axes["tension"] * .92)
        positive_hits = sum(combined.count(word) for word in _POSITIVE)
        playful_hits = sum(combined.count(word) for word in _PLAYFUL)
        tension_hits = sum(combined.count(word) for word in _TENSION)
        repair_hits = sum(combined.count(word) for word in _REPAIR)
        if positive_hits:
            axes["warmth"] = _clamp(axes["warmth"] + min(.045, positive_hits * .009))
        else:
            axes["warmth"] = _clamp(axes["warmth"] + .001)
        if playful_hits:
            axes["playfulness"] = _clamp(axes["playfulness"] + min(.05, playful_hits * .011))
        else:
            axes["playfulness"] = _clamp(axes["playfulness"] * .997)
        if tension_hits:
            axes["tension"] = _clamp(axes["tension"] + min(.16, tension_hits * .042))
            axes["steadiness"] = _clamp(axes["steadiness"] - min(.035, tension_hits * .008))
        if repair_hits:
            axes["tension"] = _clamp(axes["tension"] - min(.20, repair_hits * .055))
            axes["steadiness"] = _clamp(axes["steadiness"] + min(.05, repair_hits * .012))

        payload["meta"]["interactions"] = int(payload["meta"].get("interactions", 0)) + 1
        payload["meta"]["first_seen_at"] = payload["meta"].get("first_seen_at") or _now()
        payload["meta"]["last_interaction_at"] = _now()
        if repair_hits:
            payload["meta"]["last_settlement"] = "一次修复让关系重新安稳"
        elif tension_hits:
            payload["meta"]["last_settlement"] = "仍有一点需要被理解的余波"
        elif playful_hits:
            payload["meta"]["last_settlement"] = "刚刚一起有过玩心"
        elif positive_hits:
            payload["meta"]["last_settlement"] = "刚刚确认了彼此的靠近"
        else:
            payload["meta"]["last_settlement"] = "平常而连续的一次相处"
        self.save(payload)

        added: list[dict[str, Any]] = []
        if payload["settings"].get("auto_threads", True):
            user_candidate = self._thread_candidate(user_text, "together")
            assistant_candidate = self._thread_candidate(assistant_text, "companion")
            if user_candidate:
                item = self._maybe_add_thread(
                    user_candidate, session_id=session_id, message_id=user_message_id
                )
                if item:
                    added.append(item)
            # One successful turn should create at most one automatic open
            # loop.  A model often echoes the user's promise and adds a small
            # next step; recording both makes the shared room feel like a task
            # tracker and duplicates the same conversational intention.
            if assistant_candidate and not added:
                item = self._maybe_add_thread(
                    assistant_candidate, session_id=session_id, message_id=assistant_message_id
                )
                if item:
                    added.append(item)

        moment = None
        if payload["settings"].get("moment_capture", True):
            for event_type, title, pattern in _MILESTONE_RULES:
                if pattern.search(combined):
                    summary = exact_quote(user_text, 220) or exact_quote(assistant_text, 220)
                    with get_db() as db:
                        cursor = db.execute(
                            """INSERT INTO relationship_moments
                               (event_type, title, summary, source_session_id,
                                user_message_id, assistant_message_id, salience, created_at)
                               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                            (event_type, title, summary, session_id, user_message_id,
                             assistant_message_id, .78, _now()),
                        )
                        row = db.execute(
                            "SELECT * FROM relationship_moments WHERE id=?", (cursor.lastrowid,)
                        ).fetchone()
                    moment = dict(row)
                    break
        return {"enabled": True, "threads_added": added, "moment": moment,
                "chapter": self._chapter(payload["meta"]["interactions"])}

    def state_view(self) -> dict[str, Any]:
        payload = self.load()
        interactions = int(payload["meta"].get("interactions", 0))
        axes = {
            key: {
                "value": round(float(payload["axes"].get(key, 0)), 4),
                "label": AXIS_LABELS[key],
                "tone": self._axis_tone(key, float(payload["axes"].get(key, 0))),
            }
            for key in AXIS_KEYS
        }
        all_threads = self.list_threads("all", 60)
        threads = [item for item in all_threads if item.get("status") == "open"][:20]
        thread_history = [item for item in all_threads if item.get("status") != "open"][:24]
        shared = self.list_shared("active", 20)
        moments = self.recent_moments(12)
        foundation = self.get_foundation()
        return {
            "version": "6.1.0",
            "chapter": self._chapter(interactions),
            "axes": axes,
            "meta": deepcopy(payload["meta"]),
            "settings": deepcopy(payload["settings"]),
            "threads": threads,
            "thread_history": thread_history,
            "shared": shared,
            "moments": moments,
            "foundation": foundation,
            "counts": {
                "open_threads": len(threads),
                "thread_history": len(thread_history),
                "shared_items": len(shared),
                "moments": len(moments),
                "foundation_chars": foundation["character_count"],
            },
        }

    def prompt_context(
        self, query: str = "", *, session_id: str | None = None
    ) -> str:
        payload = self.load()
        if not payload["settings"].get("enabled", True):
            return ""
        thread_limit = int(payload["settings"].get("context_thread_limit", 5))
        shared_limit = int(payload["settings"].get("context_shared_limit", 5))
        threads = self.list_threads("open", thread_limit, session_id=session_id)
        shared = (
            self.list_shared("active", shared_limit, session_id=session_id)
            if payload["settings"].get("shared_context", True) else []
        )
        moments = self.recent_moments(3, session_id=session_id)
        lines: list[str] = []

        if session_id is None:
            # The settings/state panel can still expose the deliberately global
            # relationship view. Model prompts, however, always pass session_id
            # and therefore never inherit another window's relationship history.
            interactions = int(payload["meta"].get("interactions", 0))
            chapter = self._chapter(interactions)
            foundation_prompt = self._foundation_prompt(query)
            if foundation_prompt:
                lines.append(foundation_prompt)
            lines.extend([
                "关系具有连续性，但当前模型仍用自己的原生声音判断和表达。",
                f"关系章节：{chapter['label']}。{chapter['note']}。",
                f"最近余韵：{payload['meta'].get('last_settlement') or '平静'}。",
            ])
        elif threads or shared or moments:
            lines.append("以下连续信息只来自当前聊天窗口。")
        else:
            return ""
        if threads:
            lines.append("尚未完成或值得接续的事：")
            for item in threads:
                evidence = _clean_text(item.get("source_excerpt"), 120)
                suffix = f"；来源原话：『{evidence}』" if evidence else "；由使用者在共同空间中保存"
                lines.append(f"- [{item.get('kind','unfinished')}] {item.get('title','')}{suffix}")
        if shared:
            lines.append("共同空间中已经存在的东西：")
            for item in shared:
                detail = _clean_text(item.get("description"), 150)
                lines.append(f"- [{item.get('kind','object')}] {item.get('title','')}" + (f"：{detail}" if detail else ""))
        if moments:
            lines.append("近期关系节点：")
            for item in moments:
                lines.append(f"- {item.get('title','')}：{_clean_text(item.get('summary'), 150)}")
        lines.extend([
            "只把上面明确记录的内容当作事实，不补写不存在的共同经历。",
            "只在与当前话题真正相关时自然接续，不逐项汇报、不催办，也不要朗读关系面板。",
            "连续关系不等于永远附和；可以保留判断、边界、玩心和当下真实反应。",
        ])
        return "\n".join(lines)

    def health(self) -> dict[str, Any]:
        try:
            view = self.state_view()
            return {
                "health": "ok",
                "detail": f"{view['counts']['open_threads']} 项未完之事 · "
                          f"{view['counts']['shared_items']} 个共同内容",
            }
        except Exception as exc:
            return {"health": "error", "detail": "关系连续性组件暂不可用"}


relationship_continuity = RelationshipContinuity()
