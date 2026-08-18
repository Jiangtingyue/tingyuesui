"""Source-faithful memory archive for 大西瓜 v5.8.

Messages remain immutable evidence.  Derived memories are indexes only and keep
an exact quote plus a source message id.  Search can therefore return the raw
words instead of trusting a model summary as the final truth.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from models import get_db

_CJK = re.compile(r"[\u3400-\u9fff]{2,}")
_LATIN = re.compile(r"[A-Za-z0-9_\-]{3,}")
_STOP = {
    "这个", "那个", "什么", "怎么", "可以", "就是", "还是", "然后", "因为", "所以",
    "我们", "你们", "他们", "自己", "一个", "一下", "已经", "真的", "觉得", "但是",
    "不是", "没有", "不要", "还有", "现在", "今天", "哥哥", "宝宝", "大西瓜",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def exact_quote(text: str, max_chars: int = 220) -> str:
    """Pick a mechanically exact, human-readable quote from source text."""
    source = (text or "").strip()
    if not source:
        return ""
    pieces = [p.strip() for p in re.split(r"(?<=[。！？!?\n])", source) if p.strip()]
    quote = next((p for p in pieces if 8 <= len(p) <= max_chars), pieces[0] if pieces else source)
    return quote[:max_chars]


def query_terms(query: str, limit: int = 32) -> list[str]:
    """Extract useful Chinese/Latin terms without starving the end of a query.

    v6.8 generated every n-gram from the beginning and stopped after 12 items,
    so an important noun near the end of a natural Chinese sentence was never
    searched.  Jieba terms are preferred; the dependency-free fallback samples
    the whole sentence, including its tail.
    """
    text = (query or "").strip()
    terms: list[str] = []

    try:
        import jieba
        jieba_terms = [
            token.strip()
            for token in jieba.cut_for_search(text)
            if len(token.strip()) > 1
        ]
        if len(jieba_terms) > 20:
            last = len(jieba_terms) - 1
            positions = sorted({
                0, 1, last - 1, last,
                *(round(last * index / 15) for index in range(1, 15)),
            })
            jieba_terms = [jieba_terms[index] for index in positions]
        terms.extend(jieba_terms)
    except ImportError:
        pass

    sampled: list[str] = []
    for segment in _CJK.findall(text):
        if segment in _STOP:
            continue
        if 2 <= len(segment) <= 12:
            sampled.append(segment)
        for width in (4, 3, 2):
            if len(segment) >= width:
                positions = list(range(len(segment) - width + 1))
                if len(positions) > 8:
                    last = len(positions) - 1
                    positions = sorted({
                        0, last,
                        round(last * 0.2), round(last * 0.4),
                        round(last * 0.6), round(last * 0.8),
                    })
                sampled.extend(segment[index:index + width] for index in positions)
    # Jieba gives semantic words first; reversed fallback sampling guarantees
    # late nouns still receive slots when jieba is unavailable.
    terms.extend(reversed(sampled))
    terms.extend(_LATIN.findall(text))
    unique: list[str] = []
    for term in terms:
        term = term.strip()
        if not term or term in _STOP or term in unique:
            continue
        unique.append(term)
        if len(unique) >= max(4, min(int(limit), 64)):
            break
    return unique


def _query_terms(query: str) -> list[str]:
    """Backward-compatible name used by older tests and extensions."""
    return query_terms(query)


class MemoryArchive:
    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_raw_archive_session ON raw_archive(session_id, message_id);
                CREATE INDEX IF NOT EXISTS idx_raw_archive_created ON raw_archive(created_at);

                CREATE TABLE IF NOT EXISTS memory_source_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER,
                    session_id TEXT,
                    message_id INTEGER,
                    exact_quote TEXT NOT NULL,
                    verified INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(memory_id, message_id, exact_quote)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_source_message ON memory_source_links(message_id);

                CREATE TABLE IF NOT EXISTS memory_recall_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    query TEXT,
                    source_message_id INTEGER,
                    score REAL DEFAULT 0,
                    lane TEXT DEFAULT 'raw',
                    recalled_at TEXT NOT NULL
                );
                """
            )

    def archive_existing_messages(self) -> int:
        self.ensure_schema()
        inserted = 0
        with get_db() as db:
            rows = db.execute(
                """SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.metadata
                   FROM messages m
                   LEFT JOIN raw_archive r ON r.message_id = m.id
                   WHERE r.message_id IS NULL
                   ORDER BY m.id ASC"""
            ).fetchall()
            for row in rows:
                try:
                    metadata_value = json.loads(row["metadata"] or "{}")
                except Exception:
                    metadata_value = {}
                # A branch physically copies its visible prefix so the new
                # window can stand alone.  Those copies are navigation state,
                # not new evidence; indexing them again would duplicate every
                # search result and distort memory frequency.
                if isinstance(metadata_value, dict) and metadata_value.get("branch_copy"):
                    continue
                content = row["content"] or ""
                db.execute(
                    """INSERT OR IGNORE INTO raw_archive
                       (session_id, message_id, role, content, content_hash, created_at, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["session_id"], row["id"], row["role"], content,
                        hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest(),
                        row["created_at"] or _now(), row["metadata"] or "{}",
                    ),
                )
                inserted += 1
        return inserted

    def archive_message(
        self,
        *,
        message_id: int,
        session_id: str,
        role: str,
        content: str,
        created_at: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.ensure_schema()
        source = content or ""
        with get_db() as db:
            db.execute(
                """INSERT OR IGNORE INTO raw_archive
                   (session_id, message_id, role, content, content_hash, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, int(message_id), role, source,
                    hashlib.sha256(source.encode("utf-8", "ignore")).hexdigest(),
                    created_at or _now(),
                    json.dumps(metadata or {}, ensure_ascii=False, default=str),
                ),
            )

    def link_memory(
        self,
        *,
        memory_id: int,
        session_id: str | None,
        message_id: int | None,
        original_text: str,
    ) -> dict:
        self.ensure_schema()
        quote = exact_quote(original_text)
        verified = int(bool(quote and quote in (original_text or "")))
        if message_id is not None and quote:
            with get_db() as db:
                raw = db.execute("SELECT content FROM raw_archive WHERE message_id = ?", (message_id,)).fetchone()
                if raw is not None:
                    verified = int(quote in (raw["content"] or ""))
                db.execute(
                    """INSERT OR IGNORE INTO memory_source_links
                       (memory_id, session_id, message_id, exact_quote, verified, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (memory_id, session_id, message_id, quote, verified, _now()),
                )
        return {"quote": quote, "verified": bool(verified), "message_id": message_id}

    def get_source(self, message_id: int) -> dict | None:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM raw_archive WHERE message_id = ?", (int(message_id),)
            ).fetchone()
            link = None
            if row is None:
                link = db.execute(
                    """SELECT id, memory_id, session_id, message_id, exact_quote,
                              verified, created_at
                       FROM memory_source_links
                       WHERE message_id = ?
                       ORDER BY verified DESC, id DESC
                       LIMIT 1""",
                    (int(message_id),),
                ).fetchone()
        if row is None and link is None:
            return None
        if row is None:
            quote = link["exact_quote"] or ""
            return {
                "id": link["id"],
                "memory_id": link["memory_id"],
                "session_id": link["session_id"],
                "message_id": link["message_id"],
                "role": "deleted_conversation",
                "content": quote,
                "quote": quote,
                "content_hash": hashlib.sha256(
                    quote.encode("utf-8", "ignore")
                ).hexdigest(),
                "created_at": link["created_at"],
                "metadata": {
                    "partial": True,
                    "conversation_deleted": True,
                    "verified_when_linked": bool(link["verified"]),
                },
                "partial": True,
                "conversation_deleted": True,
            }

        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except Exception:
            item["metadata"] = {}
        return item

    def search(self, query: str, *, session_id: str | None = None, limit: int = 5) -> list[dict]:
        self.ensure_schema()
        terms = query_terms(query)
        if not terms:
            return []
        where = ["(" + " OR ".join("content LIKE ?" for _ in terms) + ")"]
        params: list[Any] = [f"%{term}%" for term in terms]
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        params.append(max(20, min(300, limit * 30)))
        with get_db() as db:
            rows = db.execute(
                f"""SELECT * FROM raw_archive
                    WHERE {' AND '.join(where)}
                    ORDER BY message_id DESC LIMIT ?""",
                params,
            ).fetchall()
            # If a conversation was deleted but a derived memory was
            # deliberately retained, search its small verified source quote
            # instead of leaving the memory provenance-less.
            link_where = "(" + " OR ".join(
                "l.exact_quote LIKE ?" for _ in terms
            ) + ")"
            link_params: list[Any] = [f"%{term}%" for term in terms]
            if session_id:
                link_where += " AND l.session_id = ?"
                link_params.append(session_id)
            link_params.append(max(20, min(300, limit * 30)))
            link_rows = db.execute(
                f"""SELECT MAX(l.id) AS id, l.session_id, l.message_id,
                           'deleted_conversation' AS role,
                           l.exact_quote AS content,
                           MIN(l.created_at) AS created_at,
                           MAX(l.verified) AS verified
                    FROM memory_source_links l
                    JOIN memories m ON m.id = l.memory_id
                    WHERE {link_where}
                      AND m.archived = 0
                      AND NOT EXISTS (
                        SELECT 1 FROM raw_archive r
                        WHERE r.message_id = l.message_id
                      )
                    GROUP BY l.message_id, l.exact_quote
                    ORDER BY l.message_id DESC
                    LIMIT ?""",
                link_params,
            ).fetchall()
        scored: list[dict] = []
        normalized_query = (query or "").strip()
        for row in [*rows, *link_rows]:
            item = dict(row)
            content = item.get("content") or ""
            if normalized_query and content.strip() == normalized_query:
                continue
            hits = sum(content.count(term) for term in terms)
            distinct = sum(1 for term in terms if term in content)
            score = distinct * 2.0 + min(hits, 8) * 0.3
            item["score"] = round(score, 3)
            item["quote"] = exact_quote(content, 320)
            if item.get("role") == "deleted_conversation":
                item["partial"] = True
                item["conversation_deleted"] = True
            scored.append(item)
        scored.sort(key=lambda x: (x["score"], x["message_id"]), reverse=True)
        return scored[: max(1, min(limit, 20))]

    def build_context(
        self, query: str, *, session_id: str | None = None, echo: str = "", limit: int = 3,
        exclude_message_ids: set[int] | None = None,
    ) -> str:
        # Automatic chat recall is strictly window-local. An explicit memory
        # search tool may still search globally, but a new chat never does.
        if not session_id:
            return ""
        combined = "\n".join(part for part in (query, echo) if part).strip()
        candidates = self.search(combined, session_id=session_id, limit=max(limit + 6, 9))
        current = (query or "").strip()
        excluded = {int(value) for value in (exclude_message_ids or set()) if value is not None}
        results = [
            item for item in candidates
            if (item.get("content") or "").strip() != current
            and (not item.get("message_id") or int(item.get("message_id")) not in excluded)
        ][:limit]
        if not results:
            return ""
        lines = [
            "<source_faithful_memory>",
            "下面是从不可改写的聊天原文中机械检索出的片段。摘要记忆与原文冲突时，以这里的原话为准。",
        ]
        for item in results:
            lines.append(
                f"- [消息#{item['message_id']} · {item.get('created_at','')}] "
                f"{item.get('role','')}: {item.get('quote','')}"
            )
        lines.append("</source_faithful_memory>")
        with get_db() as db:
            for item in results:
                db.execute(
                    """INSERT INTO memory_recall_log
                       (session_id, query, source_message_id, score, lane, recalled_at)
                       VALUES (?, ?, ?, ?, 'raw', ?)""",
                    (session_id, query[:500], item["message_id"], item["score"], _now()),
                )
        return "\n".join(lines)

    def stats(self) -> dict:
        self.ensure_schema()
        with get_db() as db:
            raw = db.execute("SELECT COUNT(*) c FROM raw_archive").fetchone()["c"]
            links = db.execute("SELECT COUNT(*) c FROM memory_source_links").fetchone()["c"]
            recalls = db.execute("SELECT COUNT(*) c FROM memory_recall_log").fetchone()["c"]
        return {"raw_messages": raw, "source_links": links, "recalls": recalls}

    def health(self) -> dict:
        try:
            stats = self.stats()
            return {"health": "ok", "detail": f"原文 {stats['raw_messages']} 条 · 来源链接 {stats['source_links']} 条"}
        except Exception as exc:
            return {"health": "error", "detail": "记忆归档组件暂不可用"}


memory_archive = MemoryArchive()
