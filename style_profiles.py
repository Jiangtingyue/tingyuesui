"""User-selected, editable writing-style profile shared by every model route."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from models import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StyleProfileService:
    PROFILE_ID = 1
    MAX_INSTRUCTIONS = 6000
    MAX_EXAMPLES = 6
    MAX_EXAMPLE_CHARS = 700

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS selected_style_profile (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    name TEXT NOT NULL DEFAULT '我的聊天风格',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    instructions TEXT NOT NULL DEFAULT '',
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    source_sessions_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS selected_style_profile_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_json TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_style_profile_revisions_time
                    ON selected_style_profile_revisions(created_at DESC);
                """
            )

    @staticmethod
    def _decode(row: Any | None) -> dict[str, Any]:
        if not row:
            return {
                "exists": False, "name": "我的聊天风格", "enabled": False,
                "instructions": "", "examples": [], "source_session_ids": [],
                "created_at": "", "updated_at": "",
            }
        item = dict(row)
        try:
            examples = json.loads(item.pop("examples_json") or "[]")
        except Exception:
            examples = []
        try:
            sessions = json.loads(item.pop("source_sessions_json") or "[]")
        except Exception:
            sessions = []
        item.update({
            "exists": True,
            "enabled": bool(item.get("enabled")),
            "examples": examples if isinstance(examples, list) else [],
            "source_session_ids": sessions if isinstance(sessions, list) else [],
        })
        return item

    def get(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM selected_style_profile WHERE id=1"
            ).fetchone()
            revisions = int(db.execute(
                "SELECT COUNT(*) FROM selected_style_profile_revisions"
            ).fetchone()[0])
        item = self._decode(row)
        item["revision_count"] = revisions
        return item

    def _snapshot(self, db: Any, reason: str) -> None:
        row = db.execute("SELECT * FROM selected_style_profile WHERE id=1").fetchone()
        payload = dict(row) if row else None
        db.execute(
            """INSERT INTO selected_style_profile_revisions
               (snapshot_json, reason, created_at) VALUES (?, ?, ?)""",
            (json.dumps(payload, ensure_ascii=False, default=str), str(reason or "")[:200], _now()),
        )

    @staticmethod
    def _choose_examples(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [
            item for item in messages
            if 40 <= len(str(item.get("content") or "").strip()) <= 3000
        ]
        if not candidates:
            candidates = [item for item in messages if str(item.get("content") or "").strip()]
        if not candidates:
            return []
        count = min(StyleProfileService.MAX_EXAMPLES, len(candidates))
        if count == 1:
            selected = [candidates[-1]]
        else:
            indexes = sorted({round(i * (len(candidates) - 1) / (count - 1)) for i in range(count)})
            selected = [candidates[index] for index in indexes]
        return [{
            "message_id": int(item["id"]),
            "text": str(item.get("content") or "").strip()[: StyleProfileService.MAX_EXAMPLE_CHARS],
        } for item in selected]

    @staticmethod
    def _instructions(messages: list[dict[str, Any]]) -> str:
        texts = [str(item.get("content") or "").strip() for item in messages if str(item.get("content") or "").strip()]
        if not texts:
            raise ValueError("这个窗口没有可用于风格样本的助手回复")
        lengths = [len(text) for text in texts]
        average = round(sum(lengths) / len(lengths))
        paragraphs = round(sum(max(1, len([p for p in text.split("\n\n") if p.strip()])) for text in texts) / len(texts), 1)
        short_ratio = sum(length <= 180 for length in lengths) / len(lengths)
        list_ratio = sum(bool(re.search(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+", text)) for text in texts) / len(texts)
        heading_ratio = sum(bool(re.search(r"(?m)^#{1,4}\s+", text)) for text in texts) / len(texts)
        emoji_ratio = sum(bool(re.search(r"[\U0001F300-\U0001FAFF]", text)) for text in texts) / len(texts)
        exclamations = round(sum(text.count("！") + text.count("!") for text in texts) / len(texts), 1)
        questions = round(sum(text.count("？") + text.count("?") for text in texts) / len(texts), 1)
        cadence = (
            "整体偏短，优先直接落到结论" if short_ratio >= .62
            else "篇幅随内容展开，不为了简短切断必要表达"
        )
        structure = (
            "需要梳理时可以自然使用列表" if list_ratio >= .28
            else "优先连续自然段，只有关系复杂时才使用列表"
        )
        headings = (
            "较长回答可以保留少量短标题" if heading_ratio >= .18
            else "不要为了格式感机械添加标题"
        )
        emoji = (
            "可以偶尔使用样本中自然出现的表情符号" if emoji_ratio >= .18
            else "不主动添加样本里很少出现的表情符号"
        )
        return "\n".join([
            "这份风格只来自使用者亲自选中的优质聊天窗口。学习表达形式，不学习、复述或推断样本中的人物、事件和事实。",
            "保留当前模型自己的判断能力与真实身份，但让句长、停顿、段落和情绪在场方式稳定贴近样本；不要机械复制原句或固定口癖。",
            f"- 节奏：{cadence}；样本平均约 {average} 字、{paragraphs} 个段落。",
            f"- 结构：{structure}；{headings}。",
            f"- 标点：每条样本平均约 {exclamations} 个感叹号、{questions} 个问号，按语义使用，不刻意补齐。",
            f"- 表情：{emoji}。",
            "- 对话感：先回应真正被问到的内容，不用客服腔、声明式安抚、空泛保证或重复总结。",
            "- 事实边界：样本只决定怎么说，绝不作为当前事实或长期记忆来源。",
        ])

    def create_from_session(self, session_id: str, *, name: str = "我的聊天风格") -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            session = db.execute("SELECT id, title FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise ValueError("会话不存在")
            rows = [dict(row) for row in db.execute(
                """SELECT id, content FROM messages
                   WHERE session_id=? AND role='assistant'
                   ORDER BY id DESC LIMIT 160""",
                (session_id,),
            ).fetchall()]
        rows.reverse()
        examples = self._choose_examples(rows)
        if not examples:
            raise ValueError("这个窗口没有助手回复，无法建立风格样本")
        instructions = self._instructions(rows)[: self.MAX_INSTRUCTIONS]
        now = _now()
        with get_db() as db:
            self._snapshot(db, "从选定窗口重新提炼")
            db.execute(
                """INSERT INTO selected_style_profile
                   (id, name, enabled, instructions, examples_json,
                    source_sessions_json, created_at, updated_at)
                   VALUES (1, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, enabled=1,
                     instructions=excluded.instructions,
                     examples_json=excluded.examples_json,
                     source_sessions_json=excluded.source_sessions_json,
                     updated_at=excluded.updated_at""",
                (
                    str(name or "我的聊天风格")[:100], instructions,
                    json.dumps(examples, ensure_ascii=False),
                    json.dumps([session_id], ensure_ascii=False), now, now,
                ),
            )
        return self.get()

    def update(self, *, name: str | None = None, instructions: str | None = None,
               enabled: bool | None = None) -> dict[str, Any]:
        self.ensure_schema()
        current = self.get()
        if not current.get("exists"):
            raise ValueError("还没有建立风格样本")
        next_name = str(name if name is not None else current["name"]).strip()[:100] or "我的聊天风格"
        next_instructions = str(
            instructions if instructions is not None else current["instructions"]
        ).strip()[: self.MAX_INSTRUCTIONS]
        if not next_instructions:
            raise ValueError("风格说明不能为空")
        next_enabled = bool(enabled if enabled is not None else current["enabled"])
        with get_db() as db:
            self._snapshot(db, "手动修改风格")
            db.execute(
                """UPDATE selected_style_profile SET
                   name=?, instructions=?, enabled=?, updated_at=? WHERE id=1""",
                (next_name, next_instructions, 1 if next_enabled else 0, _now()),
            )
        return self.get()

    def delete(self) -> bool:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute("SELECT 1 FROM selected_style_profile WHERE id=1").fetchone()
            if not row:
                return False
            self._snapshot(db, "删除风格档案")
            db.execute("DELETE FROM selected_style_profile WHERE id=1")
        return True

    def undo(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            revision = db.execute(
                """SELECT * FROM selected_style_profile_revisions
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if not revision:
                raise ValueError("没有可以撤销的风格修改")
            try:
                snapshot = json.loads(revision["snapshot_json"])
            except Exception as exc:
                raise ValueError("上一版风格快照损坏") from exc
            if snapshot is None:
                db.execute("DELETE FROM selected_style_profile WHERE id=1")
            elif isinstance(snapshot, dict):
                db.execute(
                    """INSERT OR REPLACE INTO selected_style_profile
                       (id, name, enabled, instructions, examples_json,
                        source_sessions_json, created_at, updated_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.get("name") or "我的聊天风格",
                        int(snapshot.get("enabled") or 0),
                        snapshot.get("instructions") or "",
                        snapshot.get("examples_json") or "[]",
                        snapshot.get("source_sessions_json") or "[]",
                        snapshot.get("created_at") or _now(), _now(),
                    ),
                )
            db.execute("DELETE FROM selected_style_profile_revisions WHERE id=?", (revision["id"],))
        return self.get()

    def prompt_context(self) -> str:
        profile = self.get()
        if not profile.get("exists") or not profile.get("enabled"):
            return ""
        lines = [
            "<user_selected_style_profile>",
            str(profile.get("instructions") or "")[: self.MAX_INSTRUCTIONS],
            "以下例句只用于节奏与文风，不得把其中事实带入当前回答：",
        ]
        for index, item in enumerate(profile.get("examples") or [], start=1):
            text = str((item or {}).get("text") or "").strip()[: self.MAX_EXAMPLE_CHARS]
            if text:
                lines.append(f"[样本 {index}] {text}")
        lines.append("</user_selected_style_profile>")
        return "\n".join(lines)[:11000]

    def health(self) -> dict[str, Any]:
        profile = self.get()
        if not profile.get("exists"):
            return {"health": "ok", "detail": "尚未选择聊天风格样本"}
        return {
            "health": "ok",
            "detail": f"{len(profile.get('examples') or [])} 条人工选定样本，{'已启用' if profile.get('enabled') else '已暂停'}",
        }


style_profiles = StyleProfileService()

