"""Large, resumable-in-UI conversation migration for 大西瓜 v6.1.

Uploads are copied to local storage in chunks.  A background worker then reads
JSON arrays one conversation at a time (including JSON inside official export
ZIP files) and writes a compact staging SQLite database.  The model never sees
the archive as one prompt and Python never has to ``json.loads`` the full file.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import sqlite3
import threading
import time
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator

from runtime_paths import IMPORT_DIR

from config import BASE_DIR, IMPORT_CONFIG
from diagnostics import diagnostics
from conversation_import import (
    conversation_fingerprint,
    normalize_conversation_item,
    parse_conversation_bytes,
)
from memory_archive import memory_archive
from models import delete_session, get_db
from thinking_vault import thinking_vault


_JSON_CONTAINERS = {"conversations", "chats", "sessions", "items", "data"}
_CHAT_EXPORT_NAMES = {
    "conversations.json", "conversation.json", "chat_history.json",
    "chats.json", "messages.json", "claude_conversations.json",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ImportCancelled(RuntimeError):
    pass


class _JSONCursor:
    """Small incremental JSON cursor backed by a binary stream."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        chunk_size: int = 1024 * 1024,
        progress: Callable[[int], None] | None = None,
        max_value_chars: int | None = None,
    ) -> None:
        self.stream = stream
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.progress = progress
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")()
        self.json_decoder = json.JSONDecoder()
        self.buffer = ""
        self.pos = 0
        self.eof = False
        self.bytes_read = 0
        self.max_value_chars = max(1024 * 1024, int(
            max_value_chars or IMPORT_CONFIG["max_json_value_chars"]
        ))

    def _compact(self) -> None:
        if self.pos and (self.pos > self.chunk_size or self.eof):
            self.buffer = self.buffer[self.pos:]
            self.pos = 0

    def _fill(self) -> bool:
        if self.eof:
            return False
        raw = self.stream.read(self.chunk_size)
        if raw:
            self.bytes_read += len(raw)
            try:
                self.buffer += self.decoder.decode(raw)
            except UnicodeDecodeError as exc:
                raise ValueError("聊天 JSON 必须使用 UTF-8 编码") from exc
            if self.progress:
                self.progress(self.bytes_read)
            return True
        try:
            self.buffer += self.decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ValueError("聊天 JSON 的 UTF-8 结尾不完整") from exc
        self.eof = True
        return False

    def skip_ws(self) -> None:
        while True:
            while self.pos < len(self.buffer) and self.buffer[self.pos].isspace():
                self.pos += 1
            if self.pos < len(self.buffer) or self.eof:
                return
            self._compact()
            self._fill()

    def peek(self) -> str:
        self.skip_ws()
        while self.pos >= len(self.buffer) and not self.eof:
            self._fill()
            self.skip_ws()
        return self.buffer[self.pos] if self.pos < len(self.buffer) else ""

    def expect(self, char: str) -> None:
        found = self.peek()
        if found != char:
            raise ValueError(f"聊天 JSON 结构异常：应为 {char!r}，实际为 {found!r}")
        self.pos += 1

    def value(self) -> Any:
        self.skip_ws()
        while True:
            self._compact()
            try:
                value, end = self.json_decoder.raw_decode(self.buffer, self.pos)
                self.pos = end
                return value
            except json.JSONDecodeError as exc:
                # raw_decode only fails here while the current value is still
                # incomplete. Bound that one value before a malicious/single
                # gigantic conversation can consume the whole upload in RAM.
                if len(self.buffer) - self.pos > self.max_value_chars:
                    raise ValueError("单个 JSON 对象超过本机解析安全上限") from exc
                if self.eof:
                    raise ValueError(
                        f"聊天 JSON 无法解析：第 {exc.lineno} 行第 {exc.colno} 列"
                    ) from exc
                self._fill()


def _iter_array(cursor: _JSONCursor) -> Iterator[Any]:
    cursor.expect("[")
    first = True
    while True:
        found = cursor.peek()
        if found == "]":
            cursor.pos += 1
            return
        if not first:
            cursor.expect(",")
        yield cursor.value()
        first = False


def iter_json_items(
    stream: BinaryIO,
    *,
    progress: Callable[[int], None] | None = None,
) -> Iterator[Any]:
    """Yield top-level conversations without loading the complete JSON file."""
    cursor = _JSONCursor(
        stream, progress=progress,
        max_value_chars=int(IMPORT_CONFIG["max_json_value_chars"]),
    )
    first = cursor.peek()
    if first == "[":
        yield from _iter_array(cursor)
        return
    if first != "{":
        raise ValueError("聊天 JSON 顶层必须是数组或对象")

    cursor.expect("{")
    root: dict[str, Any] = {}
    used_container = False
    first_pair = True
    while True:
        found = cursor.peek()
        if found == "}":
            cursor.pos += 1
            break
        if not first_pair:
            cursor.expect(",")
        key = cursor.value()
        if not isinstance(key, str):
            raise ValueError("聊天 JSON 对象包含非文本字段名")
        cursor.expect(":")
        if key.lower() in _JSON_CONTAINERS and cursor.peek() == "[":
            used_container = True
            yield from _iter_array(cursor)
            root[key] = []
        else:
            root[key] = cursor.value()
        first_pair = False
    if not used_container:
        yield root


def _iter_jsonl(
    stream: BinaryIO,
    *,
    progress: Callable[[int], None] | None = None,
) -> Iterator[Any]:
    bytes_read = 0
    for line_no, raw_line in enumerate(stream, start=1):
        bytes_read += len(raw_line)
        if progress:
            progress(bytes_read)
        try:
            line = raw_line.decode("utf-8-sig" if line_no == 1 else "utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"JSONL 第 {line_no} 行不是 UTF-8 文本") from exc
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL 第 {line_no} 行无法解析") from exc


def _is_message_like(value: Any) -> bool:
    return isinstance(value, dict) and any(
        key in value for key in ("role", "sender", "speaker", "author")
    ) and any(
        key in value for key in ("content", "text", "parts", "message")
    )


def _iter_jsonl_candidates(
    stream: BinaryIO, *, progress: Callable[[int], None] | None = None,
) -> Iterator[Any]:
    """Support both conversation-per-line and the common message-per-line JSONL."""
    iterator = _iter_jsonl(stream, progress=progress)
    try:
        first = next(iterator)
    except StopIteration:
        return
    if not _is_message_like(first):
        yield first
        yield from iterator
        return
    # One JSONL file containing message objects is one conversation.
    messages = [first]
    for value in iterator:
        if not _is_message_like(value):
            raise ValueError("JSONL 同一文件不能混用逐消息与逐对话格式")
        messages.append(value)
        if len(messages) > int(IMPORT_CONFIG["max_messages_per_conversation"]):
            raise ValueError("单个窗口消息数量超过本机安全上限")
    yield messages


class HistoryMigrationManager:
    def __init__(self) -> None:
        self.root = IMPORT_DIR
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def ensure_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_import_batches (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    source_path TEXT NOT NULL,
                    staging_path TEXT NOT NULL,
                    bytes_total INTEGER NOT NULL DEFAULT 0,
                    processed_bytes INTEGER NOT NULL DEFAULT 0,
                    source_format TEXT NOT NULL DEFAULT '',
                    conversation_count INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    character_count INTEGER NOT NULL DEFAULT 0,
                    reasoning_count INTEGER NOT NULL DEFAULT 0,
                    reasoning_character_count INTEGER NOT NULL DEFAULT 0,
                    imported_conversations INTEGER NOT NULL DEFAULT 0,
                    imported_messages INTEGER NOT NULL DEFAULT 0,
                    imported_reasoning_traces INTEGER NOT NULL DEFAULT 0,
                    imported_reasoning_characters INTEGER NOT NULL DEFAULT 0,
                    skipped_duplicates INTEGER NOT NULL DEFAULT 0,
                    first_session_id TEXT NOT NULL DEFAULT '',
                    samples_json TEXT NOT NULL DEFAULT '[]',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_import_status
                    ON conversation_import_batches(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS conversation_import_batch_sessions (
                    batch_id TEXT NOT NULL,
                    session_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, session_id),
                    FOREIGN KEY(batch_id) REFERENCES conversation_import_batches(id)
                );
                CREATE INDEX IF NOT EXISTS idx_import_batch_sessions_batch
                    ON conversation_import_batch_sessions(batch_id);
                CREATE TABLE IF NOT EXISTS conversation_import_batch_messages (
                    batch_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_import_batch_messages_batch
                    ON conversation_import_batch_messages(batch_id);
                CREATE TABLE IF NOT EXISTS conversation_import_batch_reasoning_changes (
                    batch_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    previous_metadata TEXT NOT NULL DEFAULT '{}',
                    previous_trace_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_import_batch_reasoning_changes_batch
                    ON conversation_import_batch_reasoning_changes(batch_id);
                """
            )
            columns = {
                str(row["name"]) for row in db.execute(
                    "PRAGMA table_info(conversation_import_batches)"
                ).fetchall()
            }
            for name in (
                "reasoning_count", "reasoning_character_count",
                "imported_reasoning_traces", "imported_reasoning_characters",
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE conversation_import_batches ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                    )
        memory_archive.ensure_schema()
        thinking_vault.ensure_schema()

    def recover_interrupted(self) -> None:
        self.ensure_schema()
        with get_db() as db:
            interrupted_batches = [
                row["id"] for row in db.execute(
                    """SELECT id FROM conversation_import_batches
                       WHERE status IN ('queued', 'scanning', 'importing', 'preparing')"""
                ).fetchall()
            ]
            interrupted_imports = [
                row["id"] for row in db.execute(
                    """SELECT id FROM conversation_import_batches
                       WHERE status IN ('importing', 'preparing')"""
                ).fetchall()
            ]
        # Imports commit in small chunks for responsiveness.  If the process
        # was terminated between chunks, restore the all-or-nothing guarantee
        # before exposing the failed task in the UI.
        for batch_id in interrupted_imports:
            self._remove_batch_sessions(batch_id)
        with get_db() as db:
            db.execute(
                """UPDATE conversation_import_batches
                   SET status='failed', error='上次运行在迁移过程中退出，请重新选择文件',
                       updated_at=?
                   WHERE status IN ('queued', 'scanning', 'importing', 'preparing')""",
                (_now(),),
            )
        for batch_id in interrupted_batches:
            self._cleanup_files(batch_id)

    def paths_for(self, batch_id: str, suffix: str) -> tuple[Path, Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        safe_suffix = suffix.lower() if suffix.lower() in {
            ".zip", ".json", ".jsonl", ".txt", ".md", ".markdown",
        } else ".upload"
        return (
            self.root / f"{batch_id}{safe_suffix}",
            self.root / f"{batch_id}.stage.sqlite",
        )

    def create_batch(
        self, *, batch_id: str, filename: str, source_path: Path,
        staging_path: Path, bytes_total: int,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = _now()
        with get_db() as db:
            db.execute(
                """INSERT INTO conversation_import_batches
                   (id, filename, status, source_path, staging_path, bytes_total,
                    created_at, updated_at)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)""",
                (
                    batch_id, filename[:240], str(source_path), str(staging_path),
                    int(bytes_total), now, now,
                ),
            )
        return self.get_batch(batch_id) or {}

    def _lock_for(self, batch_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(batch_id, threading.Lock())

    def _row(self, batch_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM conversation_import_batches WHERE id=?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def _update(self, batch_id: str, **values: Any) -> None:
        allowed = {
            "status", "processed_bytes", "source_format", "conversation_count",
            "message_count", "character_count", "reasoning_count",
            "reasoning_character_count", "imported_conversations",
            "imported_messages", "imported_reasoning_traces",
            "imported_reasoning_characters", "skipped_duplicates", "first_session_id",
            "samples_json", "warnings_json", "error", "cancel_requested",
            "completed_at",
        }
        clean = {key: value for key, value in values.items() if key in allowed}
        if not clean:
            return
        clean["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in clean)
        with get_db() as db:
            db.execute(
                f"UPDATE conversation_import_batches SET {assignments} WHERE id=?",
                [*clean.values(), batch_id],
            )

    def _cancelled(self, batch_id: str) -> bool:
        row = self._row(batch_id)
        return bool(row and row.get("cancel_requested"))

    @staticmethod
    def _stage_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_format TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'imported',
                model TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                character_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source_format, fingerprint)
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                reasoning TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            );
            CREATE INDEX idx_stage_messages_conversation
                ON messages(conversation_id, position);
            """
        )

    def _stage_items(
        self,
        batch_id: str,
        conn: sqlite3.Connection,
        items: Iterable[Any],
        *,
        start_index: int,
        counts: dict[str, int],
        formats: Counter[str],
    ) -> int:
        candidate_index = start_index
        last_report = time.monotonic()
        for item in items:
            if candidate_index % 20 == 0 and self._cancelled(batch_id):
                raise ImportCancelled("导入已由使用者取消")
            conversation, source_format = normalize_conversation_item(item, candidate_index)
            candidate_index += 1
            if not conversation:
                continue
            messages = conversation.get("messages") or []
            message_count = len(messages)
            char_count = sum(len(str(message.get("content") or "")) for message in messages)
            reasoning_count = sum(
                1 for message in messages if str(message.get("reasoning") or "").strip()
            )
            reasoning_chars = sum(
                len(str(message.get("reasoning") or "")) for message in messages
            )
            if message_count > int(IMPORT_CONFIG["max_messages_per_conversation"]):
                raise ValueError("单个窗口消息数量超过本机安全上限")
            if char_count + reasoning_chars > int(IMPORT_CONFIG["max_chars_per_conversation"]):
                raise ValueError("单个窗口文字数量超过本机安全上限")
            if counts["conversations"] >= int(IMPORT_CONFIG["max_conversations"]):
                raise ValueError("窗口数量超过本机安全上限")
            if counts["messages"] + message_count > int(IMPORT_CONFIG["max_messages"]):
                raise ValueError("消息数量超过本机安全上限")
            if counts["chars"] + counts["reasoning_chars"] + char_count + reasoning_chars > int(IMPORT_CONFIG["max_total_chars"]):
                raise ValueError("聊天文字总量超过本机安全上限")

            fingerprint = conversation_fingerprint(conversation)
            cursor = conn.execute(
                """INSERT OR IGNORE INTO conversations
                   (source_format, fingerprint, source_id, title, created_at,
                    updated_at, provider, model, message_count, character_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_format, fingerprint,
                    str(conversation.get("source_id") or "")[:240],
                    str(conversation.get("title") or "导入对话")[:120],
                    str(conversation.get("created_at") or "")[:100],
                    str(conversation.get("updated_at") or "")[:100],
                    str(conversation.get("provider") or "imported")[:40],
                    str(conversation.get("model") or "")[:120],
                    message_count, char_count,
                ),
            )
            if not cursor.rowcount:
                continue
            conversation_id = int(cursor.lastrowid)
            chunk = []
            for position, message in enumerate(messages):
                if position % 250 == 0 and self._cancelled(batch_id):
                    raise ImportCancelled("导入已由使用者取消")
                chunk.append((
                    conversation_id, position, message["role"],
                    str(message.get("content") or ""),
                    str(message.get("reasoning") or "")[: thinking_vault.MAX_CHARS],
                    str(message.get("created_at") or "")[:100],
                    str(message.get("source_id") or "")[:240],
                ))
                if len(chunk) >= 250:
                    conn.executemany(
                        """INSERT INTO messages
                           (conversation_id, position, role, content, reasoning, created_at, source_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""", chunk
                    )
                    chunk.clear()
            if chunk:
                conn.executemany(
                    """INSERT INTO messages
                       (conversation_id, position, role, content, reasoning, created_at, source_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""", chunk
                )
            counts["conversations"] += 1
            counts["messages"] += message_count
            counts["chars"] += char_count
            counts["reasoning"] += reasoning_count
            counts["reasoning_chars"] += reasoning_chars
            formats[source_format] += 1
            if counts["conversations"] % 25 == 0:
                conn.commit()
            now = time.monotonic()
            if now - last_report >= 0.5:
                self._update(
                    batch_id,
                    conversation_count=counts["conversations"],
                    message_count=counts["messages"],
                    character_count=counts["chars"],
                    reasoning_count=counts["reasoning"],
                    reasoning_character_count=counts["reasoning_chars"],
                )
                last_report = now
        return candidate_index

    @staticmethod
    def _zip_candidates(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        candidates = []
        for info in archive.infolist():
            if info.is_dir() or info.flag_bits & 0x1:
                continue
            base = Path(info.filename).name.lower()
            suffix = Path(base).suffix
            if suffix not in {".json", ".jsonl", ".txt", ".md", ".markdown"}:
                continue
            priority = 0 if base in _CHAT_EXPORT_NAMES else (
                1 if "conversation" in base or "chat" in base else 2
            )
            candidates.append((priority, -int(info.file_size), info.filename, info))
        candidates.sort(key=lambda item: item[:3])
        # Unknown JSON files in an export are usually account metadata.  Keep a
        # few fallbacks, while all explicitly chat-named chunks remain eligible.
        preferred = [item[3] for item in candidates if item[0] < 2]
        return preferred or [item[3] for item in candidates[:8]]

    def _scan_stream(
        self,
        batch_id: str,
        conn: sqlite3.Connection,
        stream: BinaryIO,
        filename: str,
        *,
        start_index: int,
        counts: dict[str, int],
        formats: Counter[str],
        progress_offset: int = 0,
    ) -> int:
        suffix = Path(filename).suffix.lower()
        last_bytes = 0
        last_report = 0.0

        def report(value: int) -> None:
            nonlocal last_bytes, last_report
            if self._cancelled(batch_id):
                raise ImportCancelled("导入已由使用者取消")
            last_bytes = value
            now = time.monotonic()
            if now - last_report >= 0.5:
                self._update(batch_id, processed_bytes=progress_offset + value)
                last_report = now

        if suffix == ".json":
            items: Iterable[Any] = iter_json_items(stream, progress=report)
        elif suffix == ".jsonl":
            items = _iter_jsonl_candidates(stream, progress=report)
        else:
            raw = stream.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError("超大型纯文本请先转换为 JSON/JSONL 或放入 ZIP")
            bundle = parse_conversation_bytes(filename, raw)
            items = [
                {
                    **conversation,
                    "messages": conversation.get("messages") or [],
                }
                for conversation in bundle.get("conversations") or []
            ]
        result = self._stage_items(
            batch_id, conn, items, start_index=start_index,
            counts=counts, formats=formats,
        )
        if last_bytes:
            self._update(batch_id, processed_bytes=progress_offset + last_bytes)
        return result

    def scan(self, batch_id: str) -> None:
        lock = self._lock_for(batch_id)
        if not lock.acquire(blocking=False):
            return
        try:
            row = self._row(batch_id)
            # A queued background task may begin just after the user pressed
            # cancel.  Never resurrect a batch that has already left queued.
            if not row or row.get("status") != "queued":
                return
            source_path = Path(row["source_path"])
            stage_path = Path(row["staging_path"])
            self._safe_unlink(stage_path)
            self._update(
                batch_id, status="scanning", error="", processed_bytes=0,
                conversation_count=0, message_count=0, character_count=0,
                reasoning_count=0, reasoning_character_count=0,
            )
            counts = {
                "conversations": 0, "messages": 0, "chars": 0,
                "reasoning": 0, "reasoning_chars": 0,
            }
            formats: Counter[str] = Counter()
            candidate_index = 0
            with sqlite3.connect(stage_path) as stage:
                self._stage_schema(stage)
                suffix = source_path.suffix.lower()
                if suffix == ".zip":
                    with zipfile.ZipFile(source_path) as archive:
                        members = self._zip_candidates(archive)
                        if not members:
                            raise ValueError("ZIP 中没有找到受支持的聊天 JSON")
                        total_uncompressed = sum(int(member.file_size) for member in members)
                        if total_uncompressed > int(IMPORT_CONFIG["max_uncompressed_bytes"]):
                            raise ValueError("ZIP 解压后的聊天数据超过本机安全上限")
                        offset = 0
                        for member in members:
                            before = counts["conversations"]
                            with archive.open(member) as stream:
                                candidate_index = self._scan_stream(
                                    batch_id, stage, stream, member.filename,
                                    start_index=candidate_index, counts=counts,
                                    formats=formats, progress_offset=offset,
                                )
                            offset += int(member.file_size)
                            # Exact official export names contain the complete
                            # history; avoid scanning unrelated account JSON.
                            if Path(member.filename).name.lower() in _CHAT_EXPORT_NAMES and counts["conversations"] > before:
                                break
                else:
                    with source_path.open("rb") as stream:
                        candidate_index = self._scan_stream(
                            batch_id, stage, stream, source_path.name,
                            start_index=0, counts=counts, formats=formats,
                        )
                stage.commit()
                if not counts["conversations"]:
                    raise ValueError("没有识别到可见的用户/助手对话")
                rows = stage.execute(
                    """SELECT c.title, c.message_count,
                              COALESCE((SELECT content FROM messages m
                                        WHERE m.conversation_id=c.id
                                        ORDER BY m.position LIMIT 1), '') AS preview
                       FROM conversations c ORDER BY c.id LIMIT 5"""
                ).fetchall()
                samples = [
                    {"title": item[0], "messages": int(item[1]), "preview": str(item[2])[:180]}
                    for item in rows
                ]
            detected = formats.most_common(1)[0][0] if formats else "generic"
            self._update(
                batch_id, status="ready", source_format=detected,
                conversation_count=counts["conversations"],
                message_count=counts["messages"], character_count=counts["chars"],
                reasoning_count=counts["reasoning"],
                reasoning_character_count=counts["reasoning_chars"],
                processed_bytes=int(row.get("bytes_total") or 0),
                samples_json=json.dumps(samples, ensure_ascii=False),
                warnings_json="[]", error="",
            )
        except ImportCancelled:
            self._update(batch_id, status="cancelled", error="导入已取消", completed_at=_now())
            self._cleanup_files(batch_id)
        except (ValueError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
            self._update(batch_id, status="failed", error=str(exc)[:800], completed_at=_now())
            self._cleanup_files(batch_id)
        except Exception as exc:
            diagnostics.record_error(
                "history_migration_scan", exc, metadata={"batch_id": batch_id}
            )
            self._update(
                batch_id, status="failed",
                error="历史扫描失败，详情已写入本机诊断记录",
                completed_at=_now(),
            )
            self._cleanup_files(batch_id)
        finally:
            lock.release()

    def apply(self, batch_id: str) -> None:
        lock = self._lock_for(batch_id)
        if not lock.acquire(blocking=False):
            return
        try:
            row = self._row(batch_id)
            if not row or row.get("status") != "ready":
                return
            stage_path = Path(row["staging_path"])
            if not stage_path.exists():
                raise ValueError("迁移暂存数据不存在，请重新选择文件")
            self._update(
                batch_id, status="importing", error="",
                imported_conversations=0, imported_messages=0,
                imported_reasoning_traces=0, imported_reasoning_characters=0,
                skipped_duplicates=0,
            )
            imported = 0
            imported_messages = 0
            imported_reasoning = 0
            imported_reasoning_chars = 0
            skipped = 0
            first_session_id = ""
            with sqlite3.connect(stage_path) as stage:
                stage.row_factory = sqlite3.Row
                with get_db() as db:
                    # Iterate both conversations and messages as SQLite
                    # cursors. A single years-long window must not be loaded
                    # into RAM as one Python list.
                    conversations = stage.execute(
                        "SELECT * FROM conversations ORDER BY id"
                    )
                    for index, conversation in enumerate(conversations):
                        if index % 20 == 0 and self._cancelled(batch_id):
                            raise ImportCancelled("导入已由使用者取消")
                        source_format = str(conversation["source_format"] or "generic")
                        # The fingerprint is already stable; this avoids reading
                        # the first messages merely to derive the same UUID.
                        session_id = str(uuid.uuid5(
                            uuid.UUID("0f06d733-dbb6-49d6-a769-7c5f64f5d749"),
                            f"{source_format}:{conversation['fingerprint']}",
                        ))
                        existing_session = db.execute(
                            "SELECT id, message_count FROM sessions WHERE id=?", (session_id,)
                        ).fetchone()
                        first_message = stage.execute(
                            """SELECT created_at FROM messages
                               WHERE conversation_id=?
                               ORDER BY position ASC LIMIT 1""",
                            (conversation["id"],),
                        ).fetchone()
                        last_message = stage.execute(
                            """SELECT created_at FROM messages
                               WHERE conversation_id=?
                               ORDER BY position DESC LIMIT 1""",
                            (conversation["id"],),
                        ).fetchone()
                        message_count = max(0, int(conversation["message_count"] or 0))
                        created_at = (
                            conversation["created_at"]
                            or (first_message["created_at"] if first_message else "")
                            or _now()
                        )
                        updated_at = (
                            conversation["updated_at"]
                            or (last_message["created_at"] if last_message else "")
                            or created_at
                        )
                        if not existing_session:
                            db.execute(
                                """INSERT INTO sessions
                                   (id, title, created_at, updated_at, provider, model,
                                    total_cost, message_count)
                                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                                (
                                    session_id, conversation["title"], created_at, updated_at,
                                    conversation["provider"], conversation["model"], message_count,
                                ),
                            )
                            db.execute(
                                """INSERT OR IGNORE INTO conversation_import_batch_sessions
                                   (batch_id, session_id, created_at) VALUES (?, ?, ?)""",
                                (batch_id, session_id, _now()),
                            )
                        existing_source_ids: dict[str, tuple[int, dict[str, Any], str]] = {}
                        existing_signatures: dict[
                            tuple[str, str, str], tuple[int, dict[str, Any], str]
                        ] = {}
                        if existing_session:
                            for old_message in db.execute(
                                """SELECT id, role, content, created_at, metadata
                                   FROM messages WHERE session_id=?""",
                                (session_id,),
                            ).fetchall():
                                raw_meta = str(old_message["metadata"] or "{}")
                                try:
                                    meta = json.loads(raw_meta)
                                except Exception:
                                    meta = {}
                                if not isinstance(meta, dict):
                                    meta = {}
                                existing = (int(old_message["id"]), meta, raw_meta)
                                source_mid = str(meta.get("source_message_id") or "")
                                if source_mid:
                                    existing_source_ids[source_mid] = existing
                                existing_signatures[(
                                    str(old_message["role"] or ""),
                                    str(old_message["content"] or ""),
                                    str(old_message["created_at"] or ""),
                                )] = existing
                        added_here = 0
                        upgraded_here = 0
                        messages = stage.execute(
                            """SELECT * FROM messages
                               WHERE conversation_id=? ORDER BY position""",
                            (conversation["id"],),
                        )
                        for msg_index, message in enumerate(messages):
                            if msg_index % 250 == 0 and self._cancelled(batch_id):
                                raise ImportCancelled("导入已由使用者取消")
                            source_mid = str(message["source_id"] or "")
                            signature = (
                                str(message["role"] or ""),
                                str(message["content"] or ""),
                                str(message["created_at"] or created_at),
                            )
                            duplicate = (
                                existing_source_ids.get(source_mid)
                                if source_mid else existing_signatures.get(signature)
                            ) if existing_session else None
                            reasoning = str(message["reasoning"] or "").strip()[: thinking_vault.MAX_CHARS]
                            if duplicate:
                                if reasoning:
                                    message_id, old_meta, raw_meta = duplicate
                                    merged_meta = dict(old_meta)
                                    merged_meta.setdefault("imported", True)
                                    merged_meta.setdefault("import_format", source_format)
                                    if source_mid:
                                        merged_meta.setdefault("source_message_id", source_mid)
                                    merged_meta.update({
                                        "thinking_available": True,
                                        "thinking_chars": len(reasoning),
                                        "thinking_label": "导出文件中明确提供的思考",
                                        "thinking_provider": conversation["provider"],
                                    })
                                    prior_trace = db.execute(
                                        "SELECT * FROM reasoning_traces WHERE message_id=?",
                                        (message_id,),
                                    ).fetchone()
                                    trace_changed = (
                                        not prior_trace
                                        or str(prior_trace["content"] or "") != reasoning
                                        or str(prior_trace["provider"] or "")
                                           != str(conversation["provider"] or "")
                                        or str(prior_trace["model"] or "")
                                           != str(conversation["model"] or "")
                                    )
                                    metadata_changed = merged_meta != old_meta
                                    if metadata_changed or trace_changed:
                                        previous_trace_json = (
                                            json.dumps(dict(prior_trace), ensure_ascii=False)
                                            if prior_trace else ""
                                        )
                                        db.execute(
                                            """INSERT OR IGNORE INTO
                                               conversation_import_batch_reasoning_changes
                                               (batch_id, message_id, session_id,
                                                previous_metadata, previous_trace_json, created_at)
                                               VALUES (?, ?, ?, ?, ?, ?)""",
                                            (
                                                batch_id, message_id, session_id, raw_meta,
                                                previous_trace_json, _now(),
                                            ),
                                        )
                                        if metadata_changed:
                                            db.execute(
                                                "UPDATE messages SET metadata=? WHERE id=?",
                                                (json.dumps(merged_meta, ensure_ascii=False), message_id),
                                            )
                                        if trace_changed:
                                            db.execute(
                                                """INSERT INTO reasoning_traces
                                                   (message_id, session_id, provider, model, content,
                                                    char_count, token_estimate, created_at)
                                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                                   ON CONFLICT(message_id) DO UPDATE SET
                                                     provider=excluded.provider,
                                                     model=excluded.model,
                                                     content=excluded.content,
                                                     char_count=excluded.char_count,
                                                     token_estimate=excluded.token_estimate,
                                                     created_at=excluded.created_at""",
                                                (
                                                    message_id, session_id,
                                                    conversation["provider"], conversation["model"],
                                                    reasoning, len(reasoning),
                                                    thinking_vault.estimate_tokens(reasoning), _now(),
                                                ),
                                            )
                                            imported_reasoning += 1
                                            imported_reasoning_chars += len(reasoning)
                                        upgraded_here += 1
                                continue
                            metadata_dict = {
                                "imported": True,
                                "import_batch_id": batch_id,
                                "import_format": source_format,
                                "source_message_id": message["source_id"] or "",
                            }
                            if reasoning:
                                metadata_dict.update({
                                    "thinking_available": True,
                                    "thinking_chars": len(reasoning),
                                    "thinking_label": "导出文件中明确提供的思考",
                                    "thinking_provider": conversation["provider"],
                                })
                            metadata = json.dumps(metadata_dict, ensure_ascii=False)
                            cursor = db.execute(
                                """INSERT INTO messages
                                   (session_id, role, content, created_at, provider, model, metadata)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    session_id, message["role"], message["content"],
                                    message["created_at"] or created_at,
                                    conversation["provider"], conversation["model"], metadata,
                                ),
                            )
                            message_id = int(cursor.lastrowid)
                            if reasoning:
                                db.execute(
                                    """INSERT INTO reasoning_traces
                                       (message_id, session_id, provider, model, content,
                                        char_count, token_estimate, created_at)
                                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                    (
                                        message_id, session_id, conversation["provider"],
                                        conversation["model"], reasoning, len(reasoning),
                                        thinking_vault.estimate_tokens(reasoning), _now(),
                                    ),
                                )
                                imported_reasoning += 1
                                imported_reasoning_chars += len(reasoning)
                            db.execute(
                                """INSERT OR IGNORE INTO conversation_import_batch_messages
                                   (batch_id, message_id, session_id, created_at) VALUES (?, ?, ?, ?)""",
                                (batch_id, message_id, session_id, _now()),
                            )
                            content = str(message["content"] or "")
                            db.execute(
                                """INSERT OR IGNORE INTO raw_archive
                                   (session_id, message_id, role, content, content_hash,
                                    created_at, metadata)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    session_id, message_id, message["role"], content,
                                    hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest(),
                                    message["created_at"] or created_at, metadata,
                                ),
                            )
                            imported_messages += 1
                            added_here += 1
                            if source_mid:
                                existing_source_ids[source_mid] = (
                                    message_id, metadata_dict, metadata
                                )
                            existing_signatures[signature] = (
                                message_id, metadata_dict, metadata
                            )
                        if existing_session:
                            if not added_here and not upgraded_here:
                                skipped += 1
                                continue
                            if added_here:
                                db.execute(
                                    """UPDATE sessions SET title=?, updated_at=?, provider=?, model=?,
                                       message_count=message_count+? WHERE id=?""",
                                    (conversation["title"], updated_at, conversation["provider"],
                                     conversation["model"], added_here, session_id),
                                )
                        imported += 1
                        first_session_id = first_session_id or session_id
                        if imported % 10 == 0:
                            db.commit()
                            self._update(
                                batch_id, imported_conversations=imported,
                                imported_messages=imported_messages,
                                imported_reasoning_traces=imported_reasoning,
                                imported_reasoning_characters=imported_reasoning_chars,
                                skipped_duplicates=skipped,
                                first_session_id=first_session_id,
                            )
            # The upload/apply task is already off the chat request path.  Use
            # that same background task to drain every affected window's
            # context backlog now, instead of charging the next dozens of chat
            # turns 1,152 source messages at a time.
            with get_db() as db:
                affected_session_ids = [
                    str(row["session_id"])
                    for row in db.execute(
                        """SELECT DISTINCT session_id
                           FROM conversation_import_batch_messages
                           WHERE batch_id=? ORDER BY session_id""",
                        (batch_id,),
                    ).fetchall()
                ]
            if affected_session_ids:
                from context_compactor import context_compactor

                self._update(
                    batch_id,
                    status="preparing",
                    imported_conversations=imported,
                    imported_messages=imported_messages,
                    imported_reasoning_traces=imported_reasoning,
                    imported_reasoning_characters=imported_reasoning_chars,
                    skipped_duplicates=skipped,
                    first_session_id=first_session_id,
                )
                preparation = context_compactor.precompact_sessions(
                    affected_session_ids,
                    should_stop=lambda: self._cancelled(batch_id),
                    max_chapters_per_pass=500,
                )
                if preparation.get("cancelled"):
                    raise ImportCancelled("导入已由使用者取消")
                if preparation.get("failed_sessions"):
                    diagnostics.record_error(
                        "history_import_precompact",
                        RuntimeError("部分导入窗口未能完成后台上下文整理"),
                        metadata={
                            "batch_id": batch_id,
                            "failed_sessions": len(preparation["failed_sessions"]),
                        },
                    )
            self._update(
                batch_id, status="completed", imported_conversations=imported,
                imported_messages=imported_messages, skipped_duplicates=skipped,
                imported_reasoning_traces=imported_reasoning,
                imported_reasoning_characters=imported_reasoning_chars,
                first_session_id=first_session_id, completed_at=_now(), error="",
            )
            self._cleanup_files(batch_id)
        except ImportCancelled:
            # Keep cancellation atomic from the user's point of view.  Some
            # chunks may already have been committed for long imports, so
            # remove exactly the sessions tracked for this batch.
            self._remove_batch_sessions(batch_id)
            self._update(batch_id, status="cancelled", error="导入已取消", completed_at=_now())
            self._cleanup_files(batch_id)
        except ValueError as exc:
            # A failed apply must not leave a half-restored history that the
            # next upload can neither see nor undo.
            self._remove_batch_sessions(batch_id)
            self._update(batch_id, status="failed", error=str(exc)[:800], completed_at=_now())
            self._cleanup_files(batch_id)
        except Exception as exc:
            self._remove_batch_sessions(batch_id)
            diagnostics.record_error(
                "history_migration_apply", exc, metadata={"batch_id": batch_id}
            )
            self._update(
                batch_id, status="failed",
                error="历史导入失败，详情已写入本机诊断记录",
                completed_at=_now(),
            )
            self._cleanup_files(batch_id)
        finally:
            lock.release()

    def request_cancel(self, batch_id: str) -> dict[str, Any] | None:
        lock = self._lock_for(batch_id)
        if not lock.acquire(blocking=False):
            # The scanner/importer owns the batch.  It checks this durable flag
            # between windows and performs its own atomic cleanup.
            self._update(batch_id, cancel_requested=1)
            return self.get_batch(batch_id)
        try:
            row = self._row(batch_id)
            if not row:
                return None
            status = str(row.get("status") or "")
            if status in {"queued", "ready"}:
                self._update(
                    batch_id, status="cancelled", cancel_requested=1,
                    error="导入已取消", completed_at=_now(),
                )
                self._cleanup_files(batch_id)
            elif status in {"failed", "cancelled"}:
                self._cleanup_files(batch_id)
                self._update(
                    batch_id, status="cancelled", error="导入已取消",
                    completed_at=_now(),
                )
            elif status in {"scanning", "importing", "preparing"}:
                # This can occur after an unclean shutdown left stale state.
                self._update(batch_id, cancel_requested=1)
            return self.get_batch(batch_id)
        finally:
            lock.release()

    def _remove_batch_sessions(self, batch_id: str) -> int:
        """Undo exactly this batch, including messages appended to existing sessions."""
        with get_db() as db:
            reasoning_changes = db.execute(
                """SELECT * FROM conversation_import_batch_reasoning_changes
                   WHERE batch_id=? ORDER BY message_id""",
                (batch_id,),
            ).fetchall()
            for change in reasoning_changes:
                message_id = int(change["message_id"] or 0)
                db.execute(
                    "UPDATE messages SET metadata=? WHERE id=?",
                    (str(change["previous_metadata"] or "{}"), message_id),
                )
                previous_trace_json = str(change["previous_trace_json"] or "")
                if not previous_trace_json:
                    db.execute(
                        "DELETE FROM reasoning_traces WHERE message_id=?", (message_id,)
                    )
                    continue
                try:
                    trace = json.loads(previous_trace_json)
                except Exception as exc:
                    raise ValueError("思考链条撤销快照损坏") from exc
                if not isinstance(trace, dict):
                    raise ValueError("思考链条撤销快照损坏")
                db.execute(
                    """INSERT INTO reasoning_traces
                       (message_id, session_id, provider, model, content,
                        char_count, token_estimate, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(message_id) DO UPDATE SET
                         session_id=excluded.session_id,
                         provider=excluded.provider, model=excluded.model,
                         content=excluded.content, char_count=excluded.char_count,
                         token_estimate=excluded.token_estimate,
                         created_at=excluded.created_at""",
                    (
                        message_id, trace.get("session_id") or change["session_id"],
                        trace.get("provider") or "", trace.get("model") or "",
                        trace.get("content") or "", int(trace.get("char_count") or 0),
                        int(trace.get("token_estimate") or 0), trace.get("created_at") or _now(),
                    ),
                )
            session_ids = [
                item["session_id"] for item in db.execute(
                    """SELECT session_id FROM conversation_import_batch_sessions
                       WHERE batch_id=? ORDER BY created_at""",
                    (batch_id,),
                ).fetchall()
            ]
            new_sessions = set(session_ids)
            appended = db.execute(
                """SELECT message_id, session_id FROM conversation_import_batch_messages
                   WHERE batch_id=? ORDER BY message_id""", (batch_id,)
            ).fetchall()
            affected_existing = set()
            for item in appended:
                sid = str(item["session_id"] or "")
                if sid in new_sessions:
                    continue
                mid = int(item["message_id"] or 0)
                db.execute("DELETE FROM raw_archive WHERE message_id=?", (mid,))
                db.execute("DELETE FROM messages WHERE id=? AND session_id=?", (mid, sid))
                affected_existing.add(sid)
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for sid in affected_existing:
                # Imported messages may already have produced derived context
                # chapters during background preparation.  Undo must clear
                # that derived state before removing the source messages.
                if "context_chapters" in tables:
                    db.execute("DELETE FROM context_chapters WHERE session_id=?", (sid,))
                if "context_compaction_state" in tables:
                    db.execute(
                        "DELETE FROM context_compaction_state WHERE session_id=?", (sid,)
                    )
                count = db.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (sid,)
                ).fetchone()["n"]
                latest = db.execute(
                    "SELECT MAX(created_at) AS at FROM messages WHERE session_id=?", (sid,)
                ).fetchone()["at"]
                db.execute(
                    "UPDATE sessions SET message_count=?, updated_at=COALESCE(?, updated_at) WHERE id=?",
                    (int(count or 0), latest, sid),
                )
            db.execute(
                "DELETE FROM conversation_import_batch_messages WHERE batch_id=?", (batch_id,)
            )
            db.execute(
                "DELETE FROM conversation_import_batch_reasoning_changes WHERE batch_id=?",
                (batch_id,),
            )
        removed = sum(1 for session_id in session_ids if delete_session(session_id))
        with get_db() as db:
            db.execute(
                "DELETE FROM conversation_import_batch_sessions WHERE batch_id=?",
                (batch_id,),
            )
        return removed

    def undo_completed(self, batch_id: str) -> dict[str, Any] | None:
        row = self._row(batch_id)
        if not row:
            return None
        if row.get("status") != "completed":
            raise ValueError("只有已经完成的迁移批次可以撤销")
        removed = self._remove_batch_sessions(batch_id)
        self._update(
            batch_id, status="reverted",
            error=f"已撤销本批恢复的 {removed} 个窗口",
            completed_at=_now(),
        )
        item = self.get_batch(batch_id) or {}
        item["reverted_sessions"] = removed
        return item

    def _safe_unlink(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            if resolved.parent != self.root.resolve():
                return
            resolved.unlink(missing_ok=True)
            Path(f"{resolved}-wal").unlink(missing_ok=True)
            Path(f"{resolved}-shm").unlink(missing_ok=True)
        except OSError:
            pass

    def _cleanup_files(self, batch_id: str) -> None:
        row = self._row(batch_id)
        if not row:
            return
        self._safe_unlink(Path(row["source_path"]))
        self._safe_unlink(Path(row["staging_path"]))

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self._row(batch_id)
        if not row:
            return None
        for key in ("samples_json", "warnings_json"):
            try:
                row[key.removesuffix("_json")] = json.loads(row.get(key) or "[]")
            except Exception:
                row[key.removesuffix("_json")] = []
            row.pop(key, None)
        row.pop("source_path", None)
        row.pop("staging_path", None)
        row["cancel_requested"] = bool(row.get("cancel_requested"))
        total = max(0, int(row.get("bytes_total") or 0))
        processed = max(0, int(row.get("processed_bytes") or 0))
        row["progress_percent"] = (
            100 if row.get("status") in {"ready", "preparing", "completed"}
            else min(99, round(processed * 100 / total)) if total else 0
        )
        row["can_apply"] = row.get("status") == "ready"
        row["can_cancel"] = row.get("status") in {
            "queued", "scanning", "ready", "importing", "preparing"
        }
        if row.get("status") == "completed":
            with get_db() as db:
                tracked = db.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM conversation_import_batch_sessions WHERE batch_id=?) +
                         (SELECT COUNT(*) FROM conversation_import_batch_messages WHERE batch_id=?) +
                         (SELECT COUNT(*) FROM conversation_import_batch_reasoning_changes
                          WHERE batch_id=?) AS count""",
                    (batch_id, batch_id, batch_id),
                ).fetchone()
            row["can_undo"] = bool(tracked and tracked["count"])
        else:
            row["can_undo"] = False
        return row

    def health(self) -> dict[str, Any]:
        try:
            self.ensure_schema()
            with get_db() as db:
                row = db.execute(
                    """SELECT COUNT(*) AS batches,
                              SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
                       FROM conversation_import_batches"""
                ).fetchone()
            return {
                "health": "ok",
                "detail": f"{int(row['completed'] or 0)} 个大型历史迁移已完成",
            }
        except Exception as exc:
            return {"health": "error", "detail": "大型历史迁移组件不可用，请查看本机诊断记录"}


history_migration = HistoryMigrationManager()
