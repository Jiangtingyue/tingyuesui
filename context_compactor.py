"""Incremental, source-linked conversation compaction for 大西瓜 v6.2.

The previous compressor called the active paid model from the recall path and
re-summarised the whole conversation on every turn after the threshold.  This
module deliberately keeps compaction local and deterministic:

* only messages that have slid out of the raw-history window are eligible;
* each fixed source range is compacted once and cached in SQLite;
* every chapter has an exact message-id source map;
* recent and query-relevant chapters are selected under a strict character
  budget, while the original messages remain untouched;
* a temporary bridge covers the newest not-yet-full chunk so no turns vanish
  between the raw window and the durable chapters.

The result is an extractive continuity ledger, not a fabricated chain of
thought and not a replacement for the source archive.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from config import CONTEXT_COMPRESSION_CONFIG
from diagnostics import diagnostics
from models import get_db, get_session_context_window_plan


VISIBLE_ROLES = ("user", "assistant")
ALGORITHM_VERSION = "source-ledger-v2"

_IMPORTANT_PHRASES = (
    "记住", "别忘", "喜欢", "不喜欢", "讨厌", "希望", "想要", "需要",
    "不要", "不能", "必须", "决定", "约定", "答应", "以后", "从现在",
    "名字", "叫我", "称呼", "生日", "纪念", "关系", "边界", "习惯",
    "最重要", "真正", "原来", "改成", "保持", "停止", "开启", "关闭",
)

_TERM_STOPWORDS = {
    "这个", "那个", "就是", "然后", "但是", "因为", "所以", "已经", "现在",
    "还是", "一个", "一下", "什么", "怎么", "可以", "可能", "真的", "觉得",
    "的话", "还有", "没有", "不是", "我们", "你们", "他们", "哥哥", "宝宝",
    "哈哈", "哈哈哈", "啊啊", "好的", "嗯嗯", "唔唔", "and", "the", "that",
    "this", "with", "from", "have", "will", "your", "you", "are",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return max(low, min(number, high))


class IncrementalContextCompactor:
    """Build and retrieve deterministic conversation chapters."""

    def __init__(self, legacy_gateway: Any = None) -> None:
        # Accept the old ``ConversationCompressor(gateway)`` constructor shape
        # for third-party imports, but never retain or call that gateway.
        del legacy_gateway
        self._lock = threading.RLock()

    # ── schema and settings ──────────────────────────────────────────────

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_compaction_state (
                    session_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_compacted_message_id INTEGER NOT NULL DEFAULT 0,
                    chapters_created INTEGER NOT NULL DEFAULT 0,
                    source_messages_compacted INTEGER NOT NULL DEFAULT 0,
                    source_chars_compacted INTEGER NOT NULL DEFAULT 0,
                    summary_chars_created INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT,
                    last_error TEXT DEFAULT '',
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS context_chapters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    start_message_id INTEGER NOT NULL,
                    end_message_id INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    source_chars INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    summary_chars INTEGER NOT NULL,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    source_hash TEXT NOT NULL,
                    algorithm_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, start_message_id, end_message_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_context_chapters_session_end
                    ON context_chapters(session_id, end_message_id DESC);
                CREATE INDEX IF NOT EXISTS idx_context_chapters_hash
                    ON context_chapters(session_id, source_hash);

                CREATE TABLE IF NOT EXISTS context_chapter_sources (
                    chapter_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(chapter_id, message_id),
                    FOREIGN KEY(chapter_id) REFERENCES context_chapters(id) ON DELETE CASCADE,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_context_source_message
                    ON context_chapter_sources(session_id, message_id);
                """
            )
            # Retire only the old derived summaries.  They remain in SQLite as
            # an audit trail but no longer compete with the source-linked lane.
            db.execute(
                """UPDATE memories SET archived=1
                   WHERE source='compressor' AND category='conversation_archive'
                     AND archived=0"""
            )

    @staticmethod
    def _session_exists(db: Any, session_id: str) -> bool:
        return bool(db.execute(
            "SELECT 1 FROM sessions WHERE id=?", (session_id,)
        ).fetchone())

    def _ensure_state(self, db: Any, session_id: str) -> None:
        if not self._session_exists(db, session_id):
            raise ValueError("会话不存在")
        db.execute(
            """INSERT OR IGNORE INTO context_compaction_state
               (session_id, enabled) VALUES (?, ?)""",
            (session_id, int(bool(CONTEXT_COMPRESSION_CONFIG.get("enabled", True)))),
        )

    def set_enabled(self, session_id: str, enabled: bool) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            self._ensure_state(db, session_id)
            db.execute(
                "UPDATE context_compaction_state SET enabled=?, last_error='' WHERE session_id=?",
                (1 if enabled else 0, session_id),
            )
        return self.status(session_id)

    def rebuild(self, session_id: str) -> dict[str, Any]:
        """Delete derived chapters only; source messages are never touched."""
        self.ensure_schema()
        with self._lock, get_db() as db:
            self._ensure_state(db, session_id)
            db.execute("DELETE FROM context_chapters WHERE session_id=?", (session_id,))
            db.execute(
                """UPDATE context_compaction_state SET
                     last_compacted_message_id=0, chapters_created=0,
                     source_messages_compacted=0, source_chars_compacted=0,
                     summary_chars_created=0, last_run_at=?, last_error=''
                   WHERE session_id=?""",
                (_now(), session_id),
            )
        self.compact_incrementally(session_id)
        return self.status(session_id, include_chapters=True)

    # ── source preparation ──────────────────────────────────────────────

    @staticmethod
    def _normalize(text: Any) -> str:
        value = str(text or "").replace("\x00", " ")
        value = re.sub(r"```[\s\S]{3000,}?```", "[较长代码块仍保存在原消息中]", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    @classmethod
    def _terms(cls, text: str) -> list[str]:
        value = cls._normalize(text).lower()
        terms: list[str] = []
        for token in re.findall(r"[a-z0-9_./-]{2,}|[\u3400-\u9fff]{2,}", value):
            token = token.strip("./-_")
            if not token or token in _TERM_STOPWORDS:
                continue
            if re.fullmatch(r"[\u3400-\u9fff]+", token):
                if len(token) <= 6:
                    terms.append(token)
                for width in (2, 3):
                    if len(token) < width:
                        continue
                    for index in range(0, len(token) - width + 1):
                        gram = token[index:index + width]
                        if gram not in _TERM_STOPWORDS:
                            terms.append(gram)
            else:
                terms.append(token[:80])
        return terms

    @classmethod
    def _keywords(cls, rows: Iterable[dict[str, Any]], limit: int = 24) -> list[str]:
        counter: Counter[str] = Counter()
        for row in rows:
            weight = 2 if row.get("role") == "user" else 1
            counter.update({term: weight for term in cls._terms(row.get("content", ""))})
        ordered = sorted(counter.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
        return [term for term, _ in ordered[:limit]]

    @classmethod
    def _sentences(cls, row: dict[str, Any], row_index: int) -> list[dict[str, Any]]:
        content = cls._normalize(row.get("content", ""))
        if not content:
            return []
        raw_parts = re.split(r"(?<=[。！？!?；;])\s*|\s*[\n\r]+\s*", content)
        if not raw_parts:
            raw_parts = [content]
        results: list[dict[str, Any]] = []
        for sentence_index, raw in enumerate(raw_parts[:24]):
            sentence = raw.strip()
            if not sentence:
                continue
            if len(sentence) > 240:
                sentence = sentence[:237].rstrip() + "…"
            score = 1.0
            if row.get("role") == "user":
                score += 1.2
            score += sum(2.8 for phrase in _IMPORTANT_PHRASES if phrase in sentence)
            if re.search(r"\b\d{1,4}[-/.年]\d{1,2}|\d{1,2}[月日点时]|[$¥￥]\s*\d", sentence):
                score += 2.0
            if any(mark in sentence for mark in ("：", ":", "“", "\"")):
                score += 0.6
            if sentence_index == 0:
                score += 0.5
            results.append({
                "row_index": row_index,
                "sentence_index": sentence_index,
                "message_id": int(row["id"]),
                "role": str(row.get("role") or "assistant"),
                "created_at": str(row.get("created_at") or ""),
                "text": sentence,
                "score": score,
            })
        return results

    @classmethod
    def _build_summary(
        cls, rows: list[dict[str, Any]], max_chars: int,
    ) -> tuple[str, list[str]]:
        if not rows:
            return "", []
        max_chars = _clamp(max_chars, 500, 6000, 1800)
        candidates: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            candidates.extend(cls._sentences(row, index))

        # Keep both ends of the chapter for narrative continuity, then fill the
        # remaining budget with high-signal, source-faithful sentences.
        chosen: dict[tuple[int, int], dict[str, Any]] = {}
        for candidate in candidates:
            if candidate["row_index"] in {0, len(rows) - 1} and candidate["sentence_index"] == 0:
                chosen[(candidate["row_index"], candidate["sentence_index"])] = candidate
        for candidate in sorted(
            candidates,
            key=lambda item: (-item["score"], -item["row_index"], item["sentence_index"]),
        ):
            key = (candidate["row_index"], candidate["sentence_index"])
            if key in chosen:
                continue
            fingerprint = re.sub(r"[^\w\u3400-\u9fff]+", "", candidate["text"].lower())[:90]
            if fingerprint and any(
                re.sub(r"[^\w\u3400-\u9fff]+", "", item["text"].lower())[:90] == fingerprint
                for item in chosen.values()
            ):
                continue
            chosen[key] = candidate
            if len(chosen) >= 14:
                break

        lines: list[str] = []
        for item in sorted(chosen.values(), key=lambda value: (value["row_index"], value["sentence_index"])):
            role = "用户" if item["role"] == "user" else "助手"
            line = f"- [{role} · 原消息#{item['message_id']}] {item['text']}"
            projected = len("\n".join([*lines, line]))
            if projected > max_chars:
                continue
            lines.append(line)
        if not lines:
            row = rows[-1]
            role = "用户" if row.get("role") == "user" else "助手"
            lines = [f"- [{role} · 原消息#{row['id']}] {cls._normalize(row.get('content'))[:max_chars - 40]}"]
        return "\n".join(lines), cls._keywords(rows)

    @staticmethod
    def _source_hash(rows: Iterable[dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for row in rows:
            digest.update(str(row.get("id") or "").encode())
            digest.update(b"\x00")
            digest.update(str(row.get("role") or "").encode())
            digest.update(b"\x00")
            digest.update(str(row.get("content") or "").encode("utf-8", "ignore"))
            digest.update(b"\x1e")
        return digest.hexdigest()

    @staticmethod
    def _public_chapter(row: Any, *, include_summary: bool = True) -> dict[str, Any]:
        item = dict(row)
        try:
            keywords = json.loads(item.pop("keywords_json", "[]") or "[]")
        except Exception:
            keywords = []
        item["keywords"] = keywords if isinstance(keywords, list) else []
        if not include_summary:
            item.pop("summary", None)
        return item

    # ── incremental writer ──────────────────────────────────────────────

    def _raw_limit(self) -> int:
        return _clamp(
            # A high emergency cap only; token/character budgets are the real
            # window limits.  This prevents routine 12-turn prefix rotations.
            CONTEXT_COMPRESSION_CONFIG.get("raw_message_limit", 500), 4, 500, 500
        )

    def _chunk_size(self) -> int:
        return _clamp(CONTEXT_COMPRESSION_CONFIG.get("chunk_messages", 24), 2, 100, 24)

    def _raw_char_limit(self) -> int:
        return _clamp(
            CONTEXT_COMPRESSION_CONFIG.get("raw_max_chars", 600000),
            12000, 1200000, 600000,
        )

    def _raw_token_limit(self) -> int:
        return _clamp(
            CONTEXT_COMPRESSION_CONFIG.get("raw_max_tokens", 158000),
            4000, 850000, 158000,
        )

    def _raw_single_message_limit(self) -> int:
        return _clamp(
            CONTEXT_COMPRESSION_CONFIG.get(
                "raw_single_message_max_chars", 240000
            ),
            4000, 600000, 240000,
        )

    def _raw_single_message_token_limit(self) -> int:
        return _clamp(
            CONTEXT_COMPRESSION_CONFIG.get(
                "raw_single_message_max_tokens", 60000
            ),
            1000, 300000, 60000,
        )

    def raw_message_limit(self) -> int:
        """The exact raw window the chat loader must keep in sync with us."""
        return self._raw_limit()

    def raw_message_char_limit(self) -> int:
        return self._raw_char_limit()

    def raw_message_token_limit(self) -> int:
        return self._raw_token_limit()

    def raw_single_message_char_limit(self) -> int:
        return self._raw_single_message_limit()

    def raw_single_message_token_limit(self) -> int:
        return self._raw_single_message_token_limit()

    def _raw_selection(self, db: Any, session_id: str) -> dict[str, Any]:
        """Mirror the chat loader's stable raw-window anchor exactly.

        The compactor must not summarize messages that the provider-facing
        loader is still keeping in its cache-stable raw prefix. Reusing the
        same planner avoids the old split-brain where the chat loader and the
        compactor disagreed about the oldest raw message.
        """
        budget = self._raw_char_limit()
        single = min(self._raw_single_message_limit(), budget)
        plan = get_session_context_window_plan(
            session_id,
            limit=self._raw_limit(),
            max_chars=budget,
            single_message_max_chars=single,
            max_tokens=self._raw_token_limit(),
            single_message_max_tokens=self._raw_single_message_token_limit(),
            db=db,
        )
        ids = [int(value) for value in plan.get("ids") or []]
        selected_source_chars = 0
        truncated = 0
        if ids:
            marks = ",".join("?" for _ in ids)
            rows = [dict(row) for row in db.execute(
                f"""SELECT id, LENGTH(content) AS content_chars FROM messages
                    WHERE session_id=? AND id IN ({marks})
                    ORDER BY id ASC""",
                [session_id, *ids],
            ).fetchall()]
            selected_source_chars = sum(
                max(0, int(row.get("content_chars") or 0)) for row in rows
            )
            truncated = sum(
                1 for row in rows
                if max(0, int(row.get("content_chars") or 0)) > single
            )
        candidate = db.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(content_chars), 0) AS chars
               FROM (
                   SELECT LENGTH(content) AS content_chars FROM messages
                   WHERE session_id=? AND role IN ('user','assistant')
                   ORDER BY id DESC LIMIT ?
               )""",
            (session_id, self._raw_limit()),
        ).fetchone()
        total = int(candidate["n"] or 0)
        total_source_chars = int(candidate["chars"] or 0)
        return {
            "items": [{"id": value} for value in ids],
            "stats": {
                "candidate_messages": total,
                "selected_messages": len(ids),
                "dropped_messages": max(0, total - len(ids)),
                "truncated_messages": truncated,
                "selected_chars": int(plan.get("used_chars") or 0),
                "selected_tokens_estimate": int(plan.get("used_tokens") or 0),
                "selected_source_chars": selected_source_chars,
                "candidate_source_chars": total_source_chars,
                "cache_window_anchor_id": int(plan.get("anchor_message_id") or 0),
                "cache_window_generation": int(plan.get("generation") or 0),
                "cache_window_rotated": bool(plan.get("rotated")),
                "cache_window_stable": bool(plan.get("stable")),
                "max_tokens": int(plan.get("max_tokens") or self._raw_token_limit()),
            },
        }

    def _eligible_cutoff(self, db: Any, session_id: str) -> int | None:
        selection = self._raw_selection(db, session_id)
        selected = selection.get("items") or []
        if not selected:
            return None
        cutoff = int(selected[0]["id"])
        older_exists = db.execute(
            """SELECT 1 FROM messages
               WHERE session_id=? AND role IN ('user','assistant') AND id<?
               LIMIT 1""",
            (session_id, cutoff),
        ).fetchone()
        return cutoff if older_exists else None

    def _insert_chapter(
        self, db: Any, session_id: str, rows: list[dict[str, Any]],
    ) -> tuple[int, bool, int, int]:
        summary, keywords = self._build_summary(
            rows, CONTEXT_COMPRESSION_CONFIG.get("chapter_max_chars", 1800)
        )
        source_chars = sum(len(str(row.get("content") or "")) for row in rows)
        created_at = _now()
        existing = db.execute(
            """SELECT id FROM context_chapters
               WHERE session_id=? AND start_message_id=? AND end_message_id=?""",
            (session_id, int(rows[0]["id"]), int(rows[-1]["id"])),
        ).fetchone()
        if existing:
            return int(existing["id"]), False, source_chars, len(summary)
        cursor = db.execute(
            """INSERT INTO context_chapters
               (session_id, start_message_id, end_message_id, message_count,
                source_chars, summary, summary_chars, keywords_json,
                source_hash, algorithm_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id, int(rows[0]["id"]), int(rows[-1]["id"]), len(rows),
                source_chars, summary, len(summary),
                json.dumps(keywords, ensure_ascii=False), self._source_hash(rows),
                ALGORITHM_VERSION, created_at,
            ),
        )
        chapter_id = int(cursor.lastrowid)
        db.executemany(
            """INSERT INTO context_chapter_sources
               (chapter_id, session_id, message_id, role, ordinal)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (chapter_id, session_id, int(row["id"]), str(row["role"]), index)
                for index, row in enumerate(rows)
            ],
        )
        return chapter_id, True, source_chars, len(summary)

    def compact_incrementally(
        self, session_id: str, *, max_chapters: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema()
        max_chapters = _clamp(
            max_chapters if max_chapters is not None else
            CONTEXT_COMPRESSION_CONFIG.get("max_new_chapters_per_request", 48),
            1, 500, 48,
        )
        created = 0
        compacted_messages = 0
        source_chars = 0
        summary_chars = 0
        disabled = False
        with self._lock:
            try:
                with get_db() as db:
                    self._ensure_state(db, session_id)
                    state = db.execute(
                        "SELECT * FROM context_compaction_state WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                    if not state or not bool(state["enabled"]):
                        # Do not call status() while this write transaction is
                        # still open: a second SQLite writer would wait for the
                        # busy timeout. Leave the context first, then inspect.
                        disabled = True
                    else:
                        last_id = int(state["last_compacted_message_id"] or 0)
                        cutoff = self._eligible_cutoff(db, session_id)
                        if cutoff is not None:
                            for _ in range(max_chapters):
                                rows = [dict(row) for row in db.execute(
                                    """SELECT id, role, content, created_at FROM messages
                                       WHERE session_id=? AND role IN ('user','assistant')
                                         AND id>? AND id<?
                                       ORDER BY id ASC LIMIT ?""",
                                    (session_id, last_id, cutoff, self._chunk_size()),
                                ).fetchall()]
                                if len(rows) < self._chunk_size():
                                    break
                                _, was_created, row_chars, chapter_chars = self._insert_chapter(
                                    db, session_id, rows
                                )
                                last_id = int(rows[-1]["id"])
                                if was_created:
                                    created += 1
                                    compacted_messages += len(rows)
                                    source_chars += row_chars
                                    summary_chars += chapter_chars
                            db.execute(
                                """UPDATE context_compaction_state SET
                                     last_compacted_message_id=?,
                                     chapters_created=chapters_created+?,
                                     source_messages_compacted=source_messages_compacted+?,
                                     source_chars_compacted=source_chars_compacted+?,
                                     summary_chars_created=summary_chars_created+?,
                                     last_run_at=?, last_error=''
                                   WHERE session_id=?""",
                                (
                                    last_id, created, compacted_messages, source_chars,
                                    summary_chars, _now(), session_id,
                                ),
                            )
            except Exception as exc:
                diagnostics.record_error(
                    "context_compaction", exc, metadata={"session_id": session_id}
                )
                try:
                    with get_db() as db:
                        if self._session_exists(db, session_id):
                            self._ensure_state(db, session_id)
                            db.execute(
                                """UPDATE context_compaction_state
                                   SET last_error=?, last_run_at=? WHERE session_id=?""",
                                ("上下文整理失败，详情已写入本机诊断记录", _now(), session_id),
                            )
                except Exception:
                    pass
        result = self.status(session_id)
        result["new_chapters"] = created
        result["new_source_messages"] = compacted_messages
        if disabled:
            result["status"] = "off"
        return result

    # ── retrieval and rendering ─────────────────────────────────────────

    def _candidate_chapters(self, db: Any, session_id: str, query: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in db.execute(
            """SELECT * FROM context_chapters WHERE session_id=?
               ORDER BY end_message_id DESC LIMIT 140""",
            (session_id,),
        ).fetchall()]
        terms = list(dict.fromkeys(self._terms(query)))[:5]
        if terms:
            clauses: list[str] = []
            params: list[Any] = [session_id]
            for term in terms:
                clauses.extend(["summary LIKE ?", "keywords_json LIKE ?"])
                pattern = f"%{term}%"
                params.extend([pattern, pattern])
            params.append(80)
            relevant = [dict(row) for row in db.execute(
                f"""SELECT * FROM context_chapters WHERE session_id=? AND ({' OR '.join(clauses)})
                    ORDER BY end_message_id DESC LIMIT ?""",
                params,
            ).fetchall()]
            seen = {int(row["id"]) for row in rows}
            rows.extend(row for row in relevant if int(row["id"]) not in seen)
        return rows

    def _score(self, chapter: dict[str, Any], query_terms: set[str], newest_id: int) -> float:
        chapter_terms = set(self._terms(
            f"{chapter.get('summary', '')} {chapter.get('keywords_json', '')}"
        ))
        overlap = len(query_terms & chapter_terms)
        recency = (int(chapter.get("end_message_id") or 0) / max(1, newest_id))
        return overlap * 5.0 + recency

    def _bridge_rows(
        self, db: Any, session_id: str, *, last_id: int, cutoff: int | None,
    ) -> list[dict[str, Any]]:
        if cutoff is None:
            return []
        rows = [dict(row) for row in db.execute(
            """SELECT id, role, content, created_at FROM messages
               WHERE session_id=? AND role IN ('user','assistant')
                 AND id>? AND id<? ORDER BY id DESC LIMIT ?""",
            (session_id, last_id, cutoff, self._chunk_size()),
        ).fetchall()]
        rows.reverse()
        return rows

    def prepare_context(self, session_id: str, query: str) -> dict[str, Any]:
        """Compact new ranges and return a budgeted continuity block."""
        try:
            run_status = self.compact_incrementally(session_id)
            if not bool(run_status.get("enabled", True)):
                return {
                    "context": "",
                    "status": run_status,
                    "selected_chapters": [],
                }
            self.ensure_schema()
            with get_db() as db:
                self._ensure_state(db, session_id)
                state = db.execute(
                    "SELECT * FROM context_compaction_state WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if not state or not bool(state["enabled"]):
                    # A concurrent toggle may have changed the state after the
                    # first status read. Return without opening a nested writer.
                    disabled_status = dict(run_status)
                    disabled_status.update({"enabled": False, "status": "off", "label": "已关闭"})
                    return {"context": "", "status": disabled_status, "selected_chapters": []}
                candidates = self._candidate_chapters(db, session_id, query)
                query_terms = set(self._terms(query))
                newest_id = max((int(row["end_message_id"]) for row in candidates), default=1)
                recent_count = _clamp(
                    CONTEXT_COMPRESSION_CONFIG.get("recent_chapters", 2), 0, 10, 2
                )
                relevant_count = _clamp(
                    CONTEXT_COMPRESSION_CONFIG.get("relevant_chapters", 4), 0, 20, 4
                )
                selected: dict[int, dict[str, Any]] = {}
                for row in sorted(candidates, key=lambda item: int(item["end_message_id"]), reverse=True)[:recent_count]:
                    selected[int(row["id"])] = row
                ranked = sorted(
                    candidates,
                    key=lambda item: self._score(item, query_terms, newest_id),
                    reverse=True,
                )
                for row in ranked:
                    if len(selected) >= recent_count + relevant_count:
                        break
                    selected.setdefault(int(row["id"]), row)

                cutoff = self._eligible_cutoff(db, session_id)
                bridge_rows = self._bridge_rows(
                    db, session_id,
                    last_id=int(state["last_compacted_message_id"] or 0),
                    cutoff=cutoff,
                )
                bridge_summary, _ = self._build_summary(
                    bridge_rows, min(
                        1500,
                        _clamp(CONTEXT_COMPRESSION_CONFIG.get("chapter_max_chars", 1800), 500, 6000, 1800),
                    ),
                ) if bridge_rows else ("", [])

            budget = _clamp(
                CONTEXT_COMPRESSION_CONFIG.get("context_max_chars", 7600),
                1200, 24000, 7600,
            )
            rendered: list[str] = [
                '<older_conversation_chapters version="2" mode="source-linked">',
                "这些是滑出原文窗口的旧对话摘录。方括号内的原消息编号可回到本机原文核对；若与原文冲突，以原文为准。",
            ]
            used = len("\n".join(rendered))
            public_selected: list[dict[str, Any]] = []
            for row in sorted(selected.values(), key=lambda item: int(item["start_message_id"])):
                block = (
                    f'<chapter id="{row["id"]}" source_messages="{row["start_message_id"]}-'
                    f'{row["end_message_id"]}" count="{row["message_count"]}">\n'
                    f'{row["summary"]}\n</chapter>'
                )
                if used + len(block) + 40 > budget:
                    continue
                rendered.append(block)
                used += len(block)
                public_selected.append(self._public_chapter(row, include_summary=False))
            if bridge_summary:
                bridge = (
                    f'<recent_bridge source_messages="{bridge_rows[0]["id"]}-'
                    f'{bridge_rows[-1]["id"]}" count="{len(bridge_rows)}">\n'
                    f'{bridge_summary}\n</recent_bridge>'
                )
                if used + len(bridge) + 40 <= budget:
                    rendered.append(bridge)
                    used += len(bridge)
            rendered.append("</older_conversation_chapters>")
            context = "\n\n".join(rendered) if len(rendered) > 3 else ""
            status = self.status(session_id)
            status.update({
                "new_chapters": int(run_status.get("new_chapters") or 0),
                "new_source_messages": int(run_status.get("new_source_messages") or 0),
                "selected_chapters": len(public_selected),
                "selected_context_chars": len(context),
                "bridge_messages": len(bridge_rows) if bridge_summary else 0,
            })
            return {
                "context": context,
                "status": status,
                "selected_chapters": public_selected,
            }
        except Exception as exc:
            diagnostics.record_error(
                "context_retrieval", exc, metadata={"session_id": session_id}
            )
            return {
                "context": "",
                "status": {
                    "enabled": bool(CONTEXT_COMPRESSION_CONFIG.get("enabled", True)),
                    "status": "error",
                    "error": "上下文读取失败，详情已写入本机诊断记录",
                },
                "selected_chapters": [],
            }

    # ── inspection ──────────────────────────────────────────────────────

    def status(self, session_id: str, *, include_chapters: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            self._ensure_state(db, session_id)
            state_row = db.execute(
                "SELECT * FROM context_compaction_state WHERE session_id=?", (session_id,)
            ).fetchone()
            state = dict(state_row) if state_row else {}
            cutoff = self._eligible_cutoff(db, session_id)
            last_id = int(state.get("last_compacted_message_id") or 0)
            if cutoff is None:
                backlog = 0
            else:
                backlog = int(db.execute(
                    """SELECT COUNT(*) FROM messages
                       WHERE session_id=? AND role IN ('user','assistant')
                         AND id>? AND id<?""",
                    (session_id, last_id, cutoff),
                ).fetchone()[0])
            visible_count = int(db.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE session_id=? AND role IN ('user','assistant')""",
                (session_id,),
            ).fetchone()[0])
            raw_stats = self._raw_selection(db, session_id).get("stats") or {}
            chapter_rows = db.execute(
                """SELECT * FROM context_chapters WHERE session_id=?
                   ORDER BY end_message_id DESC LIMIT ?""",
                (session_id, 12 if include_chapters else 1),
            ).fetchall()

        source_chars = int(state.get("source_chars_compacted") or 0)
        summary_chars = int(state.get("summary_chars_created") or 0)
        saved_chars = max(0, source_chars - summary_chars)
        enabled = bool(state.get("enabled", 1))
        last_error = str(state.get("last_error") or "")
        if last_error:
            label = "上次整理失败，聊天已自动跳过压缩层"
            status_name = "error"
        elif not enabled:
            label = "已关闭"
            status_name = "off"
        elif backlog >= self._chunk_size():
            label = "正在增量整理"
            status_name = "catching_up"
        elif cutoff is None:
            label = "原文窗口充足，暂不压缩"
            status_name = "waiting"
        else:
            label = "旧历史已接续"
            status_name = "ready"
        result: dict[str, Any] = {
            "session_id": session_id,
            "enabled": enabled,
            "status": status_name,
            "label": label,
            "algorithm": ALGORITHM_VERSION,
            "raw_message_limit": self._raw_limit(),
            "raw_max_chars": self._raw_char_limit(),
            "raw_single_message_max_chars": self._raw_single_message_limit(),
            "raw_selected_messages": int(raw_stats.get("selected_messages") or 0),
            "raw_selected_chars": int(raw_stats.get("selected_chars") or 0),
            "raw_dropped_messages": int(raw_stats.get("dropped_messages") or 0),
            "raw_truncated_messages": int(raw_stats.get("truncated_messages") or 0),
            "chunk_messages": self._chunk_size(),
            "visible_messages": visible_count,
            "chapters": int(state.get("chapters_created") or 0),
            "source_messages_compacted": int(state.get("source_messages_compacted") or 0),
            "source_chars": source_chars,
            "summary_chars": summary_chars,
            "saved_chars": saved_chars,
            "estimated_saved_tokens": int(math.floor(saved_chars / 2.4)),
            "compression_ratio": round(source_chars / max(1, summary_chars), 2) if source_chars else 1.0,
            "backlog_messages": backlog,
            "last_run_at": state.get("last_run_at"),
            "error": last_error,
        }
        if include_chapters:
            result["recent_chapters"] = [
                self._public_chapter(row, include_summary=True) for row in chapter_rows
            ]
        return result

    def source_messages(self, session_id: str, chapter_id: int) -> list[dict[str, Any]]:
        self.ensure_schema()
        with get_db() as db:
            rows = db.execute(
                """SELECT m.id, m.role, m.content, m.created_at
                   FROM context_chapter_sources s
                   JOIN messages m ON m.id=s.message_id AND m.session_id=s.session_id
                   WHERE s.session_id=? AND s.chapter_id=?
                   ORDER BY s.ordinal ASC""",
                (session_id, int(chapter_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def precompact_sessions(
        self,
        session_ids: Iterable[str],
        *,
        should_stop: Callable[[], bool] | None = None,
        max_chapters_per_pass: int = 500,
        max_passes_per_session: int = 1000,
    ) -> dict[str, Any]:
        """Drain imported-history backlogs outside the chat request path."""
        ordered = list(dict.fromkeys(str(value or "") for value in session_ids if value))
        completed = 0
        failed: list[str] = []
        chapters = 0
        source_messages = 0
        for session_id in ordered:
            if should_stop and should_stop():
                return {
                    "cancelled": True,
                    "sessions_total": len(ordered),
                    "sessions_completed": completed,
                    "failed_sessions": failed,
                    "new_chapters": chapters,
                    "source_messages": source_messages,
                }
            for _ in range(max(1, int(max_passes_per_session))):
                result = self.compact_incrementally(
                    session_id, max_chapters=max_chapters_per_pass
                )
                chapters += int(result.get("new_chapters") or 0)
                source_messages += int(result.get("new_source_messages") or 0)
                if result.get("status") == "error" or result.get("error"):
                    failed.append(session_id)
                    break
                backlog = int(result.get("backlog_messages") or 0)
                if backlog < self._chunk_size():
                    completed += 1
                    break
                if int(result.get("new_chapters") or 0) <= 0:
                    failed.append(session_id)
                    break
                if should_stop and should_stop():
                    return {
                        "cancelled": True,
                        "sessions_total": len(ordered),
                        "sessions_completed": completed,
                        "failed_sessions": failed,
                        "new_chapters": chapters,
                        "source_messages": source_messages,
                    }
            else:
                failed.append(session_id)
        return {
            "cancelled": False,
            "sessions_total": len(ordered),
            "sessions_completed": completed,
            "failed_sessions": failed,
            "new_chapters": chapters,
            "source_messages": source_messages,
        }

    def health(self) -> dict[str, Any]:
        try:
            self.ensure_schema()
            with get_db() as db:
                chapters = int(db.execute("SELECT COUNT(*) FROM context_chapters").fetchone()[0])
                sources = int(db.execute("SELECT COUNT(*) FROM context_chapter_sources").fetchone()[0])
            return {
                "health": "ok",
                "detail": f"增量章节 {chapters} 个 · 原文来源 {sources} 条 · 默认不调用付费模型",
            }
        except Exception as exc:
            return {"health": "error", "detail": "上下文整理组件暂不可用"}


context_compactor = IncrementalContextCompactor()
