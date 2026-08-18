"""Source-linked, low-friction memory for short first-person facts.

The regular memory pipeline is excellent at indexing substantial messages, but
older versions intentionally skipped very short text.  That made a sentence
such as ``我喜欢吃草莓`` visible in the raw archive while still being absent
from same-session recall.  This module keeps a small, deterministic fact layer
beside the archive:

* only the user's own explicit first-person statements are eligible;
* jokes, examples and hypotheticals are not promoted to stable facts;
* a later correction supersedes the previous active value;
* every fact keeps the session/message id and the exact source quote;
* extraction and recall are local and spend no model tokens or API credits.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from config import MEMORY_CONFIG
from models import get_db


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


class NaturalMemoryService:
    """Capture and recall concise, source-faithful user facts."""

    _SKIP_MARKERS = (
        "开玩笑", "逗你的", "逗你玩", "骗你的", "只是举例", "举个例子",
        "比如说", "例如", "假设", "假如", "要是说", "如果我",
    )
    _UNCERTAIN_MARKERS = (
        "可能", "也许", "大概", "好像", "说不定", "不确定", "暂时",
    )
    _PREFIX = (
        r"^(?:(?:哥哥|宝宝|亲爱的|小机)[，,、\s]*)?"
        r"(?:(?:嗯+|唔+|欸+|诶+|那个|就是|其实|话说|对了)[，,、\s]*)?"
    )
    _CLAUSE_SPLIT = re.compile(r"[\n。！？!?；;]+")
    _TAIL_SPLIT = re.compile(
        r"(?:，|,|但是|不过|可是|所以|然后|而且|只是|虽然|同时|顺便)"
    )
    _VAGUE_VALUES = {
        "", "这个", "那个", "这些", "那些", "这样", "那样", "这种",
        "那种", "它", "你", "哥哥", "宝宝", "小机", "他", "她", "他们",
        "这件事", "那件事", "东西", "什么",
    }
    _FOOD_HINTS = (
        "吃", "喝", "水果", "食物", "饭", "菜", "口味", "甜品", "零食",
        "饮料", "早餐", "午餐", "晚餐", "推荐",
    )
    _PROFILE_HINTS = (
        "名字", "叫什", "生日", "住哪", "哪里人", "来自", "过敏", "不能吃",
        "习惯", "偏好", "喜欢", "讨厌", "记得我",
    )

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS natural_memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_key TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    value TEXT NOT NULL,
                    polarity TEXT NOT NULL DEFAULT 'affirmed',
                    display_text TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT .8,
                    importance REAL NOT NULL DEFAULT .7,
                    status TEXT NOT NULL DEFAULT 'active',
                    source_session_id TEXT,
                    source_message_id INTEGER,
                    exact_quote TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    mention_count INTEGER NOT NULL DEFAULT 1,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_natural_memory_status
                    ON natural_memory_facts(status, confidence, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_natural_memory_key
                    ON natural_memory_facts(canonical_key, status);
                CREATE INDEX IF NOT EXISTS idx_natural_memory_source
                    ON natural_memory_facts(source_session_id, source_message_id);
                DROP INDEX IF EXISTS idx_natural_memory_one_active;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_natural_memory_one_active_per_session
                    ON natural_memory_facts(source_session_id, canonical_key)
                    WHERE status = 'active' AND source_session_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS natural_memory_processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    extractor_version TEXT NOT NULL DEFAULT 'local-v2'
                );
                CREATE TABLE IF NOT EXISTS natural_memory_backfill_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS natural_memory_fact_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '手动修改',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(fact_id) REFERENCES natural_memory_facts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_natural_memory_fact_revisions
                    ON natural_memory_fact_revisions(fact_id, id DESC);
                """
            )

    @staticmethod
    def _normalise_value(raw: str) -> str:
        value = str(raw or "").strip()
        value = NaturalMemoryService._TAIL_SPLIT.split(value, maxsplit=1)[0]
        value = re.sub(r"^[：:、，,\s]+", "", value)
        value = re.sub(r"[。！？!?；;，,\s]+$", "", value)
        value = re.sub(r"(?:了|啦|呀|啊|哦|噢|呢|嘛|吧)+$", "", value)
        value = re.sub(r"\s+", " ", value)
        return value[:48].strip()

    @staticmethod
    def _preference_category(verb: str, value: str) -> tuple[str, str]:
        action = ""
        category = "general"
        if "吃" in verb:
            action, category = "吃", "food"
        elif "喝" in verb:
            action, category = "喝", "food"
        elif "看" in verb:
            action, category = "看", "media"
        elif "听" in verb:
            action, category = "听", "media"
        elif "玩" in verb:
            action, category = "玩", "activity"
        elif value.startswith(("吃", "喝", "看", "听", "玩")):
            action = value[0]
            value = value[1:].strip()
            category = "food" if action in {"吃", "喝"} else (
                "media" if action in {"看", "听"} else "activity"
            )
        return category, f"{action}{value}" if action else value

    @staticmethod
    def _fact(
        *,
        fact_type: str,
        category: str,
        key: str,
        value: str,
        polarity: str,
        display: str,
        quote: str,
        confidence: float,
        importance: float,
    ) -> dict[str, Any]:
        return {
            "fact_type": fact_type,
            "category": category,
            "canonical_key": key,
            "value": value,
            "polarity": polarity,
            "display_text": display,
            "exact_quote": quote[:500],
            "confidence": round(_clamp(confidence, 0, 1), 3),
            "importance": round(_clamp(importance, 0, 1), 3),
        }

    def _extract_clause(self, clause: str) -> list[dict[str, Any]]:
        original = str(clause or "").strip()
        if not original or len(original) > 180:
            return []
        if any(
            marker in original
            and f"不是{marker}" not in original
            and f"并非{marker}" not in original
            for marker in self._SKIP_MARKERS
        ):
            return []
        confidence = 0.64 if any(
            marker in original for marker in self._UNCERTAIN_MARKERS
        ) else 0.92
        prefix = self._PREFIX

        # Negative preference must run before the positive pattern because
        # phrases such as “不喜欢” contain “喜欢”.
        preference_patterns = (
            (
                re.compile(
                    prefix
                    + r"我(?:现在|最近)?(?:可能|也许|大概|好像)?(?:真的|很|特别|最|挺|超|蛮)?"
                    r"(?P<verb>不喜欢吃|不爱吃|不喜欢喝|不爱喝|不喜欢看|不爱看|"
                    r"不喜欢听|不爱听|不喜欢玩|不爱玩|不喜欢|不爱|讨厌)"
                    r"(?P<value>.+)$"
                ),
                "negative",
            ),
            (
                re.compile(
                    prefix
                    + r"我(?:现在|最近)?(?:可能|也许|大概|好像)?(?:真的|很|特别|最|挺|超|蛮)?"
                    r"(?P<verb>喜欢吃|爱吃|喜欢喝|爱喝|喜欢看|爱看|"
                    r"喜欢听|爱听|喜欢玩|爱玩|喜欢|爱)"
                    r"(?P<value>.+)$"
                ),
                "positive",
            ),
        )
        for pattern, polarity in preference_patterns:
            match = pattern.match(original)
            if not match:
                continue
            verb = match.group("verb")
            value = self._normalise_value(match.group("value"))
            if not value or value in self._VAGUE_VALUES:
                return []
            category, spoken_value = self._preference_category(verb, value)
            canonical_value = re.sub(r"^(?:吃|喝|看|听|玩)", "", spoken_value)
            canonical_value = re.sub(r"\s+", "", canonical_value).lower()
            if not canonical_value:
                return []
            if polarity == "negative":
                display = (
                    f"用户不喜欢{spoken_value}"
                    if verb != "讨厌" else f"用户讨厌{spoken_value}"
                )
            else:
                display = f"用户喜欢{spoken_value}"
            return [self._fact(
                fact_type="preference",
                category=category,
                key=f"preference:{canonical_value}",
                value=value,
                polarity=polarity,
                display=display,
                quote=original,
                confidence=confidence,
                importance=0.82,
            )]

        allergy_reversal = re.compile(
            prefix
            + r"我(?:现在|已经)?(?:对)?(?P<value>.+?)(?:不过敏|不再过敏)(?:了)?$"
        ).match(original)
        if allergy_reversal:
            value = self._normalise_value(allergy_reversal.group("value"))
            if value and value not in self._VAGUE_VALUES:
                compact = re.sub(r"\s+", "", value).lower()
                return [self._fact(
                    fact_type="constraint",
                    category="health",
                    key=f"constraint:allergy:{compact}",
                    value=value,
                    polarity="negative",
                    display=f"用户对{value}不过敏",
                    quote=original,
                    confidence=0.92,
                    importance=0.96,
                )]

        habit_reversal = re.compile(
            prefix
            + r"我(?:现在|最近)?(?:已经)?不再"
              r"(?:每天|经常|通常|习惯)?(?P<value>.+?)(?:了)?$"
        ).match(original)
        if habit_reversal:
            value = self._normalise_value(habit_reversal.group("value"))
            if value and value not in self._VAGUE_VALUES:
                compact = re.sub(r"\s+", "", value).lower()
                return [self._fact(
                    fact_type="habit",
                    category="routine",
                    key=f"habit:custom:{compact}",
                    value=value,
                    polarity="negative",
                    display=f"用户已经不再{value}",
                    quote=original,
                    confidence=0.9,
                    importance=0.76,
                )]

        named_patterns = (
            (
                "profile", "identity", "profile:name",
                re.compile(prefix + r"我叫(?P<value>[\w\u3400-\u9fff·]{1,24})$"),
                lambda value: f"用户的名字是{value}",
                0.95, 0.95,
            ),
            (
                "profile", "identity", "profile:birthday",
                re.compile(prefix + r"我的生日(?:是|在)(?P<value>.+)$"),
                lambda value: f"用户的生日是{value}",
                0.94, 0.93,
            ),
            (
                "profile", "identity", "profile:location",
                re.compile(prefix + r"我(?:住在|来自)(?P<value>.+)$"),
                lambda value: f"用户住在或来自{value}",
                0.88, 0.78,
            ),
            (
                "constraint", "food", "constraint:cannot_eat",
                re.compile(prefix + r"我(?:不能吃|不可以吃)(?P<value>.+)$"),
                lambda value: f"用户不能吃{value}",
                0.96, 0.98,
            ),
            (
                "constraint", "health", "constraint:allergy",
                re.compile(prefix + r"我(?:对)?(?P<value>.+?)(?:过敏|会过敏)$"),
                lambda value: f"用户对{value}过敏",
                0.96, 0.98,
            ),
            (
                "habit", "routine", "habit:custom",
                re.compile(prefix + r"我(?:平时|每天|经常|通常|习惯)(?P<value>.+)$"),
                lambda value: f"用户平时会{value}",
                0.82, 0.7,
            ),
        )
        for fact_type, category, base_key, pattern, renderer, conf, importance in named_patterns:
            match = pattern.match(original)
            if not match:
                continue
            value = self._normalise_value(match.group("value"))
            if not value or value in self._VAGUE_VALUES:
                return []
            key = base_key
            if fact_type in {"constraint", "habit"}:
                compact_value = re.sub(r"\s+", "", value).lower()
                key += f":{compact_value}"
            return [self._fact(
                fact_type=fact_type,
                category=category,
                key=key,
                value=value,
                polarity="affirmed",
                display=renderer(value),
                quote=original,
                confidence=min(conf, confidence) if confidence < 0.8 else conf,
                importance=importance,
            )]
        return []

    def extract(self, text: str) -> list[dict[str, Any]]:
        """Return conservative facts from a user's message without API calls."""
        value = str(text or "").strip()
        if not value:
            return []
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        candidates: list[str] = []
        for clause in self._CLAUSE_SPLIT.split(value):
            candidates.append(clause)
            unreliable = any(
                marker in clause
                and f"不是{marker}" not in clause
                and f"并非{marker}" not in clause
                for marker in self._SKIP_MARKERS
            )
            if not unreliable:
                candidates.extend(re.split(r"[，,]+", clause))
        for clause in candidates:
            for fact in self._extract_clause(clause.strip()):
                marker = f"{fact['canonical_key']}|{fact['value']}|{fact['polarity']}"
                if marker not in seen:
                    seen.add(marker)
                    found.append(fact)
        return found[:6]

    def _store(
        self,
        fact: dict[str, Any],
        *,
        session_id: str | None,
        message_id: int | None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = _utcnow()
        key = str(fact["canonical_key"])
        with get_db() as db:
            active = db.execute(
                """SELECT * FROM natural_memory_facts
                   WHERE canonical_key = ? AND status = 'active'
                     AND source_session_id IS ?
                   ORDER BY id DESC LIMIT 1""",
                (key, session_id),
            ).fetchone()
            if active and (
                str(active["value"]) == str(fact["value"])
                and str(active["polarity"]) == str(fact["polarity"])
            ):
                try:
                    active_metadata = json.loads(active["metadata"] or "{}")
                except Exception:
                    active_metadata = {}
                preserved_display = (
                    str(active["display_text"])
                    if isinstance(active_metadata, dict) and active_metadata.get("owner_edited")
                    else fact["display_text"]
                )
                confidence = max(float(active["confidence"]), float(fact["confidence"]))
                importance = max(float(active["importance"]), float(fact["importance"]))
                db.execute(
                    """UPDATE natural_memory_facts
                       SET confidence = ?, importance = ?, last_seen_at = ?,
                           mention_count = mention_count + 1,
                           source_session_id = ?, source_message_id = ?,
                           exact_quote = ?, display_text = ?
                       WHERE id = ?""",
                    (
                        confidence, importance, now, session_id, message_id,
                        fact["exact_quote"], preserved_display, active["id"],
                    ),
                )
                fact_id = int(active["id"])
                action = "reinforced"
            else:
                if active:
                    db.execute(
                        """UPDATE natural_memory_facts
                           SET status = 'superseded', last_seen_at = ?
                           WHERE id = ?""",
                        (now, active["id"]),
                    )
                cursor = db.execute(
                    """INSERT INTO natural_memory_facts
                       (canonical_key, fact_type, category, value, polarity,
                        display_text, confidence, importance, status,
                        source_session_id, source_message_id, exact_quote,
                        first_seen_at, last_seen_at, mention_count, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        key,
                        fact["fact_type"],
                        fact["category"],
                        fact["value"],
                        fact["polarity"],
                        fact["display_text"],
                        fact["confidence"],
                        fact["importance"],
                        session_id,
                        message_id,
                        fact["exact_quote"],
                        now,
                        now,
                        json.dumps({"extractor": "local-v2"}, ensure_ascii=False),
                    ),
                )
                fact_id = int(cursor.lastrowid)
                action = "superseded" if active else "created"
        return {**fact, "id": fact_id, "action": action}

    def capture(
        self,
        text: str,
        *,
        session_id: str | None = None,
        message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Extract and persist natural facts from one user utterance."""
        self.ensure_schema()
        if message_id is not None:
            with get_db() as db:
                seen = db.execute(
                    """SELECT 1 FROM natural_memory_processed_messages
                       WHERE message_id=?""",
                    (int(message_id),),
                ).fetchone()
            if seen:
                return []
        facts = self.extract(text)
        # “我不过敏了” can safely revoke one unambiguous active allergy.
        if not facts and re.fullmatch(
            self._PREFIX + r"我(?:现在|已经)?不过敏(?:了)?",
            str(text or "").strip(),
        ):
            with get_db() as db:
                rows = db.execute(
                    """SELECT * FROM natural_memory_facts
                       WHERE status='active'
                         AND canonical_key LIKE 'constraint:allergy:%'
                         AND source_session_id IS ?""",
                    (session_id,),
                ).fetchall()
            if len(rows) == 1:
                old = dict(rows[0])
                facts = [self._fact(
                    fact_type="constraint",
                    category="health",
                    key=str(old["canonical_key"]),
                    value=str(old["value"]),
                    polarity="negative",
                    display=f"用户对{old['value']}不过敏",
                    quote=str(text or "").strip(),
                    confidence=0.84,
                    importance=0.96,
                )]
        stored = [
            self._store(fact, session_id=session_id, message_id=message_id)
            for fact in facts
        ]
        if message_id is not None:
            with get_db() as db:
                db.execute(
                    """INSERT OR REPLACE INTO natural_memory_processed_messages
                       (message_id, processed_at, extractor_version)
                       VALUES (?, ?, 'local-v2')""",
                    (int(message_id), _utcnow()),
                )
        return stored

    def backfill_batch(self, limit: int = 500) -> dict[str, int]:
        """Incrementally scan old/imported user turns without blocking startup."""
        self.ensure_schema()
        batch_size = max(1, min(int(limit), 2000))
        with get_db() as db:
            state = db.execute(
                "SELECT last_message_id FROM natural_memory_backfill_state WHERE id=1"
            ).fetchone()
            cursor = int(state["last_message_id"] if state else 0)
            rows = db.execute(
                """SELECT id, session_id, content FROM messages
                   WHERE role='user' AND id > ?
                   ORDER BY id ASC LIMIT ?""",
                (cursor, batch_size),
            ).fetchall()
        facts = 0
        last_id = cursor
        for row in rows:
            last_id = int(row["id"])
            facts += len(self.capture(
                str(row["content"] or ""),
                session_id=str(row["session_id"] or ""),
                message_id=last_id,
            ))
        if rows:
            with get_db() as db:
                db.execute(
                    """INSERT INTO natural_memory_backfill_state
                       (id, last_message_id, updated_at)
                       VALUES (1, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         last_message_id=excluded.last_message_id,
                         updated_at=excluded.updated_at""",
                    (last_id, _utcnow()),
                )
        return {
            "scanned": len(rows),
            "facts": facts,
            "last_message_id": last_id,
        }

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except Exception:
            item["metadata"] = {}
        return item

    def list_facts(
        self, *, status: str = "active", limit: int = 100,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        safe_status = status if status in {"active", "superseded", "all"} else "active"
        clauses: list[str] = []
        params: list[Any] = []
        if safe_status != "all":
            clauses.append("status = ?")
            params.append(safe_status)
        if session_id is not None:
            clauses.append("source_session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with get_db() as db:
            rows = db.execute(
                f"""SELECT f.*,
                           (SELECT COUNT(*) FROM natural_memory_fact_revisions r
                            WHERE r.fact_id=f.id) AS revision_count
                    FROM natural_memory_facts f {where}
                    ORDER BY status = 'active' DESC, importance DESC,
                             confidence DESC, last_seen_at DESC
                    LIMIT ?""",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._row_dict(row) for row in rows]

    def update_fact(
        self,
        fact_id: int,
        *,
        display_text: str | None = None,
        value: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
    ) -> dict[str, Any]:
        """Apply an owner-authored correction while retaining one-step history."""
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM natural_memory_facts WHERE id=?", (int(fact_id),)
            ).fetchone()
            if not row:
                raise ValueError("没有找到这条自然记忆")
            snapshot = dict(row)
            next_display = str(
                display_text if display_text is not None else row["display_text"]
            ).strip()[:500]
            next_value = str(value if value is not None else row["value"]).strip()[:120]
            next_category = str(
                category if category is not None else row["category"]
            ).strip()[:60] or "general"
            if not next_display or not next_value:
                raise ValueError("记忆内容和值都不能为空")
            next_confidence = _clamp(
                confidence if confidence is not None else row["confidence"], 0, 1
            )
            next_importance = _clamp(
                importance if importance is not None else row["importance"], 0, 1
            )
            next_key = str(row["canonical_key"] or "")
            compact_value = re.sub(r"\s+", "", next_value).lower()
            if next_key.startswith("preference:"):
                compact_value = re.sub(r"^(?:吃|喝|看|听|玩)", "", compact_value)
                next_key = f"preference:{compact_value}"
            elif next_key.startswith("constraint:allergy:"):
                next_key = f"constraint:allergy:{compact_value}"
            elif next_key.startswith("constraint:cannot_eat:"):
                next_key = f"constraint:cannot_eat:{compact_value}"
            elif next_key.startswith("habit:custom:"):
                next_key = f"habit:custom:{compact_value}"
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except Exception:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update({"owner_edited": True, "owner_edited_at": _utcnow()})
            db.execute(
                """INSERT INTO natural_memory_fact_revisions
                   (fact_id, snapshot_json, reason, created_at)
                   VALUES (?, ?, '手动修改', ?)""",
                (int(fact_id), json.dumps(snapshot, ensure_ascii=False, default=str), _utcnow()),
            )
            db.execute(
                """UPDATE natural_memory_facts SET
                   canonical_key=?, display_text=?, value=?, category=?, confidence=?, importance=?,
                   metadata=?, last_seen_at=? WHERE id=?""",
                (
                    next_key, next_display, next_value, next_category,
                    next_confidence, next_importance,
                    json.dumps(metadata, ensure_ascii=False), _utcnow(), int(fact_id),
                ),
            )
            updated = db.execute(
                """SELECT f.*,
                          (SELECT COUNT(*) FROM natural_memory_fact_revisions r
                           WHERE r.fact_id=f.id) AS revision_count
                   FROM natural_memory_facts f WHERE f.id=?""",
                (int(fact_id),),
            ).fetchone()
        return self._row_dict(updated)

    def undo_fact(self, fact_id: int) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            revision = db.execute(
                """SELECT * FROM natural_memory_fact_revisions
                   WHERE fact_id=? ORDER BY id DESC LIMIT 1""",
                (int(fact_id),),
            ).fetchone()
            if not revision:
                raise ValueError("这条记忆没有可以撤销的修改")
            try:
                snapshot = json.loads(revision["snapshot_json"])
            except Exception as exc:
                raise ValueError("上一版记忆快照损坏") from exc
            fields = (
                "canonical_key", "fact_type", "category", "value", "polarity",
                "display_text", "confidence", "importance", "status",
                "source_session_id", "source_message_id", "exact_quote",
                "first_seen_at", "last_seen_at", "mention_count", "metadata",
            )
            if not isinstance(snapshot, dict):
                raise ValueError("上一版记忆快照损坏")
            assignments = ", ".join(f"{field}=?" for field in fields)
            db.execute(
                f"UPDATE natural_memory_facts SET {assignments} WHERE id=?",
                [*(snapshot.get(field) for field in fields), int(fact_id)],
            )
            db.execute(
                "DELETE FROM natural_memory_fact_revisions WHERE id=?", (revision["id"],)
            )
            restored = db.execute(
                """SELECT f.*,
                          (SELECT COUNT(*) FROM natural_memory_fact_revisions r
                           WHERE r.fact_id=f.id) AS revision_count
                   FROM natural_memory_facts f WHERE f.id=?""",
                (int(fact_id),),
            ).fetchone()
        return self._row_dict(restored)

    def forget(self, fact_id: int) -> bool:
        self.ensure_schema()
        with get_db() as db:
            cursor = db.execute(
                "DELETE FROM natural_memory_facts WHERE id = ?", (int(fact_id),)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _query_score(query: str, fact: dict[str, Any]) -> float:
        text = re.sub(r"\s+", "", str(query or "")).lower()
        value = re.sub(r"\s+", "", str(fact.get("value") or "")).lower()
        display = re.sub(r"\s+", "", str(fact.get("display_text") or "")).lower()
        score = 0.0
        if value and value in text:
            score += 8.0
        common = {char for char in text if "\u3400" <= char <= "\u9fff"} & {
            char for char in f"{value}{display}" if "\u3400" <= char <= "\u9fff"
        }
        score += min(2.0, len(common) * 0.22)
        category = str(fact.get("category") or "")
        if category == "food" and any(token in text for token in NaturalMemoryService._FOOD_HINTS):
            score += 4.0
        if fact.get("fact_type") in {"profile", "preference", "constraint"} and any(
            token in text for token in NaturalMemoryService._PROFILE_HINTS
        ):
            score += 2.0
        return score

    def build_context(
        self, query: str, *, session_id: str | None = None, limit: int | None = None,
        exclude_message_ids: set[int] | None = None,
    ) -> str:
        """Build a bounded fact block from this conversation only."""
        if not MEMORY_CONFIG.get("natural_memory_enabled", True) or not session_id:
            return ""
        facts = self.list_facts(
            status="active",
            limit=MEMORY_CONFIG.get("natural_memory_scan_limit", 240),
            session_id=session_id,
        )
        if not facts:
            return ""
        excluded = {int(value) for value in (exclude_message_ids or set()) if value is not None}
        if excluded:
            facts = [
                fact for fact in facts
                if not fact.get("source_message_id") or int(fact.get("source_message_id")) not in excluded
            ]
        if not facts:
            return ""
        for fact in facts:
            fact["_relevance"] = self._query_score(query, fact)
            fact["_rank"] = (
                fact["_relevance"] * 10
                + float(fact.get("importance") or 0) * 3
                + float(fact.get("confidence") or 0) * 2
                + min(2.0, int(fact.get("mention_count") or 1) * 0.2)
            )
        relevant = sorted(
            (fact for fact in facts if fact["_relevance"] >= 2.0),
            key=lambda item: item["_rank"],
            reverse=True,
        )
        core = sorted(
            (
                fact for fact in facts
                if float(fact.get("confidence") or 0) >= 0.78
                and fact not in relevant
            ),
            key=lambda item: item["_rank"],
            reverse=True,
        )
        max_items = max(
            1,
            min(
                int(limit or MEMORY_CONFIG.get("natural_memory_core_facts", 12)),
                30,
            ),
        )
        selected = [*relevant, *core][:max_items]
        if not selected:
            return ""
        lines = [
            "<natural_user_memory>",
            "以下事实只来自当前聊天窗口，并保留原消息来源。"
            "只在自然相关时使用；若用户本轮改口，以本轮为准，不要反复背诵这些条目。",
        ]
        for fact in selected:
            source = str(fact.get("source_session_id") or "未知窗口")
            message_id = fact.get("source_message_id")
            source_label = f"{source} / 消息 {message_id}" if message_id else source
            uncertain = "（尚不确定）" if float(fact.get("confidence") or 0) < 0.75 else ""
            lines.append(f"- {fact['display_text']}{uncertain} [来源: {source_label}]")
        lines.append("</natural_user_memory>")
        max_chars = max(
            400,
            min(int(MEMORY_CONFIG.get("natural_memory_context_chars", 1600)), 6000),
        )
        return "\n".join(lines)[:max_chars]

    def health(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            active = db.execute(
                "SELECT COUNT(*) AS c FROM natural_memory_facts WHERE status = 'active'"
            ).fetchone()["c"]
            superseded = db.execute(
                "SELECT COUNT(*) AS c FROM natural_memory_facts WHERE status = 'superseded'"
            ).fetchone()["c"]
        return {
            "health": "ok",
            "detail": f"{active} 条当前自然事实，{superseded} 条已被改口替代",
            "active": int(active),
            "superseded": int(superseded),
        }


natural_memory = NaturalMemoryService()
