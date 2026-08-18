"""Bridge Life-space artifacts into the long-term memory pipeline.

The Life UI remains the source of truth.  This module creates searchable,
provenance-labelled derived memories for Moon Mail, diary entries and todos.
Edits replace the active derived chunks; deletes archive them instead of
silently leaving stale facts in recall.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import dwell_life

ROOT = Path(__file__).resolve().parent
MOON_MAIL_PATH = ROOT / "static" / "keepsakes" / "moon-mail.html"


def _text(value: Any, limit: int = 8000) -> str:
    return str(value or "").strip()[:limit]


def _extract_js_array(source: str, marker: str = "let mails =") -> str:
    start = source.find(marker)
    if start < 0:
        raise ValueError("Moon Mail 没有找到内置信件数组")
    start = source.find("[", start + len(marker))
    if start < 0:
        raise ValueError("Moon Mail 信件数组格式异常")
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    i = start
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {'"', "'", "`"}:
            quote = ch
            i += 1
            continue
        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise ValueError("Moon Mail 信件数组没有正常结束")


def _strip_js_comments(text: str) -> str:
    out: list[str] = []
    quote = ""
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in {'"', "'"}:
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def moon_mail_letters(path: Path = MOON_MAIL_PATH) -> list[dict[str, Any]]:
    """Read bundled Moon Mail letters without executing browser JavaScript."""
    raw = path.read_text(encoding="utf-8")
    array_text = _strip_js_comments(_extract_js_array(raw))
    try:
        data = json.loads(array_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Moon Mail 内置信件无法解析：{exc}") from exc
    if not isinstance(data, list):
        raise ValueError("Moon Mail 内置信件不是数组")
    return [item for item in data if isinstance(item, dict)]


def _mail_key(mail: dict[str, Any]) -> str:
    return f"moon_mail:{_text(mail.get('id'), 120)}"


def enqueue_mail(pipeline: Any, mail: dict[str, Any]) -> bool:
    mail_id = _text(mail.get("id"), 120)
    body = _text(mail.get("body"), 200000)
    if not mail_id or not body or _text(mail.get("status"), 32) == "draft":
        return False
    date = _text(mail.get("displayDate") or mail.get("scheduledAt") or mail.get("createdAt"), 80)
    metadata = {
        "kind": "letter",
        "kind_label": "历史信件",
        "mail_id": mail_id,
        "title": _text(mail.get("title"), 180),
        "writer": _text(mail.get("writer"), 100),
        "receiver": _text(mail.get("receiver"), 100),
        "date": date,
        "place": _text(mail.get("place"), 160),
        "status": _text(mail.get("status"), 32),
        "archive_source": _text(mail.get("archiveSource"), 240),
    }
    return pipeline.enqueue(
        body,
        source="moon_mail",
        external_key=_mail_key(mail),
        metadata=metadata,
    )


def _diary_key(item: dict[str, Any]) -> str:
    return f"dwell_diary:{_text(item.get('id'), 120)}"


def enqueue_diary(pipeline: Any, item: dict[str, Any]) -> bool:
    item_id = _text(item.get("id"), 120)
    body = _text(item.get("text"), 8000)
    if not item_id or not body:
        return False
    title = _text(item.get("title"), 100)
    at = _text(item.get("at"), 40)
    who = "小月" if item.get("who") != "gu" else "共同日记 / 对方"
    metadata = {
        "kind": "diary",
        "kind_label": "Life 日记",
        "diary_id": item_id,
        "title": title or "无标题日记",
        "writer": who,
        "date": at,
    }
    return pipeline.enqueue(
        body,
        source="dwell_diary",
        external_key=_diary_key(item),
        metadata=metadata,
    )


def _todo_key(side: str, item_id: str) -> str:
    return f"dwell_todo:{side}:{_text(item_id, 120)}"


def enqueue_todo(pipeline: Any, side: str, item: dict[str, Any]) -> bool:
    item_id = _text(item.get("id"), 120)
    text = _text(item.get("text"), 500)
    if side not in {"mine", "hers"} or not item_id or not text:
        return False
    done = bool(item.get("done"))
    who = "小月" if side == "hers" else "对方"
    clock = _text(item.get("at"), 10)
    document = f"待办：{text}\n状态：{'已完成' if done else '未完成'}"
    if clock:
        document += f"\n时间：{clock}"
    metadata = {
        "kind": "todo",
        "kind_label": "Life 待办",
        "todo_id": item_id,
        "side": side,
        "writer": who,
        "title": text,
        "date": clock,
        "done": done,
    }
    return pipeline.enqueue(
        document,
        source="dwell_todo",
        external_key=_todo_key(side, item_id),
        metadata=metadata,
    )


def archive_diary(pipeline: Any, item_id: str) -> bool:
    return pipeline.archive_external_document(f"dwell_diary:{_text(item_id, 120)}")


def archive_todo(pipeline: Any, side: str, item_id: str) -> bool:
    return pipeline.archive_external_document(_todo_key(side, item_id))


def sync_existing(pipeline: Any) -> dict[str, int]:
    """Queue the current bundled letters + existing Life diary/todos once."""
    counts = {"letters": 0, "diary": 0, "todos": 0, "errors": 0}
    try:
        for mail in moon_mail_letters():
            if enqueue_mail(pipeline, mail):
                counts["letters"] += 1
    except Exception:
        counts["errors"] += 1

    try:
        for item in dwell_life.diary_entries(limit=500):
            if enqueue_diary(pipeline, item):
                counts["diary"] += 1
    except Exception:
        counts["errors"] += 1

    try:
        todo_map = dwell_life.todos()
        for side in ("mine", "hers"):
            for item in todo_map.get(side, []):
                if enqueue_todo(pipeline, side, item):
                    counts["todos"] += 1
    except Exception:
        counts["errors"] += 1
    return counts


def status() -> dict[str, Any]:
    """Return visible sync state for the Home memory-bridge badge."""
    from models import get_db

    result: dict[str, Any] = {
        "letters": 0, "diary": 0, "todos": 0,
        "pending": 0, "errors": 0, "last_updated": "",
    }
    try:
        # The status badge may be read before the async memory worker has started.
        # Ensure only the durable bridge/job tables here; this does not enqueue work.
        from pipeline import pipeline as memory_pipeline
        memory_pipeline._ensure_job_schema()
        with get_db() as db:
            rows = db.execute(
                """SELECT source, COUNT(*) AS n, MAX(updated_at) AS last_updated
                   FROM memory_external_documents
                   WHERE active=1 AND source IN ('moon_mail','dwell_diary','dwell_todo')
                   GROUP BY source"""
            ).fetchall()
            latest = ""
            mapping = {"moon_mail": "letters", "dwell_diary": "diary", "dwell_todo": "todos"}
            for row in rows:
                key = mapping.get(str(row["source"] or ""))
                if key:
                    result[key] = int(row["n"] or 0)
                stamp = str(row["last_updated"] or "")
                if stamp > latest:
                    latest = stamp
            result["last_updated"] = latest
            queued = db.execute(
                """SELECT status, COUNT(*) AS n FROM memory_ingest_jobs
                   WHERE source IN ('moon_mail','dwell_diary','dwell_todo')
                     AND status IN ('pending','queued','processing','error')
                   GROUP BY status"""
            ).fetchall()
            for row in queued:
                n = int(row["n"] or 0)
                if str(row["status"]) == "error":
                    result["errors"] += n
                else:
                    result["pending"] += n
    except Exception:
        result["errors"] += 1
    return result
