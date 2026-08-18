"""Transactional storage for the Dwell-style life space.

Every life item is stored as its own SQLite row.  This avoids whole-file JSON
rewrites, makes concurrent Uvicorn/Gunicorn workers safe, and lets the mobile
summary read one consistent snapshot.  Existing ``dwell-life/*.json`` files
are imported once and left untouched as a recovery copy.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import models
from models import get_db
from runtime_paths import DATA_DIR


ROOT = (DATA_DIR / "dwell-life").resolve()
_SCHEMA_LOCK = threading.RLock()
_SCHEMA_KEY = ""

MAX_WHISPERS = 500
MAX_TODOS_PER_SIDE = 1000
MAX_CALENDAR_ITEMS = 2000
MAX_DIARY_ENTRIES = 500
MAX_BOOKS = 1000
MAX_MUSIC = 500


class DwellLifeError(RuntimeError):
    pass


class DwellDataCorruptionError(DwellLifeError):
    pass


class DwellStorageError(DwellLifeError):
    pass


def _ensure_root() -> None:
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DwellStorageError(f"生活区旧数据目录不可读写：{exc}") from exc


def _backup_corrupt(path: Path) -> Path:
    """Back up unchanged corrupt legacy bytes only once."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DwellStorageError(f"损坏数据无法读取：{path.name}") from exc
    digest = hashlib.sha256(raw).hexdigest()[:16]
    target = path.with_name(f"{path.name}.corrupt-{digest}.bak")
    if target.exists():
        try:
            if target.read_bytes() == raw:
                return target
        except OSError:
            pass
        counter = 1
        while target.exists():
            target = path.with_name(f"{path.name}.corrupt-{digest}-{counter}.bak")
            counter += 1
    try:
        target.write_bytes(raw)
    except OSError as exc:
        raise DwellStorageError(f"损坏数据无法备份：{path.name}") from exc
    return target


def _load_legacy(name: str, default: Any) -> Any:
    path = ROOT / name
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return deepcopy(default)
    except OSError as exc:
        raise DwellStorageError(f"读取旧生活数据 {name} 失败：{exc}") from exc
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        backup = _backup_corrupt(path)
        raise DwellDataCorruptionError(
            f"{name} 已损坏，原文件未覆盖；备份：{backup.name}"
        ) from exc
    if not isinstance(value, type(default)):
        backup = _backup_corrupt(path)
        raise DwellDataCorruptionError(
            f"{name} 数据结构异常，原文件未覆盖；备份：{backup.name}"
        )
    return value


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _now() -> int:
    return int(time.time())


def _id() -> str:
    return secrets.token_hex(6)


def _payload(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


def _decode_payload(raw: str, *, item_id: str = "") -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise DwellDataCorruptionError(
            f"生活区 SQLite 记录 {item_id or '未知'} 已损坏，已停止修改"
        ) from exc
    if not isinstance(value, dict):
        raise DwellDataCorruptionError(
            f"生活区 SQLite 记录 {item_id or '未知'} 结构异常，已停止修改"
        )
    return value


def _insert_row(
    db: Any,
    kind: str,
    item: dict[str, Any],
    *,
    session_id: str = "",
    bucket: str = "",
    sort_key: str = "",
    replace: bool = False,
) -> None:
    record = dict(item)
    record["id"] = _text(record.get("id"), 64) or _id()
    created_at = int(record.get("at") or record.get("made") or _now())
    command = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    db.execute(
        f"""{command} INTO dwell_items
            (kind, id, session_id, bucket, sort_key, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            kind,
            record["id"],
            _text(session_id, 120),
            _text(bucket, 24),
            _text(sort_key, 32),
            created_at,
            _payload(record),
        ),
    )


def _migrate_legacy_json(db: Any) -> None:
    marker = db.execute(
        "SELECT value FROM dwell_meta WHERE key='legacy_json_migration_v1'"
    ).fetchone()
    if marker:
        return

    _ensure_root()
    whispers_data = _load_legacy("whispers.json", [])
    todos_data = _load_legacy("todos.json", {"mine": [], "hers": []})
    calendar_data = _load_legacy("calendar.json", [])
    diary_data = _load_legacy("diary.json", [])
    books_data = _load_legacy("books.json", [])
    music_data = _load_legacy("music.json", [])

    for raw in whispers_data:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        _insert_row(
            db,
            "whisper",
            item,
            session_id=_text(item.get("session_id"), 120),
        )
    for side in ("mine", "hers"):
        values = todos_data.get(side, []) if isinstance(todos_data, dict) else []
        for raw in values:
            if isinstance(raw, dict):
                _insert_row(db, "todo", dict(raw), bucket=side)
    for raw in calendar_data:
        if isinstance(raw, dict):
            item = dict(raw)
            _insert_row(db, "calendar", item, sort_key=_text(item.get("date"), 10))
    for raw in diary_data:
        if isinstance(raw, dict):
            _insert_row(db, "diary", dict(raw))
    for raw in books_data:
        if isinstance(raw, dict):
            _insert_row(db, "book", dict(raw))
    for raw in music_data:
        if isinstance(raw, dict):
            _insert_row(db, "music", dict(raw))

    db.execute(
        "INSERT INTO dwell_meta(key, value) VALUES('legacy_json_migration_v1', ?)",
        (json.dumps({"migrated_at": _now()}, ensure_ascii=False),),
    )


def ensure_schema() -> None:
    """Create the transactional store and import legacy JSON exactly once."""
    global _SCHEMA_KEY
    key = str(models.DB_PATH)
    with _SCHEMA_LOCK:
        if _SCHEMA_KEY == key:
            return
        try:
            with get_db() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS dwell_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS dwell_items (
                        kind TEXT NOT NULL,
                        id TEXT NOT NULL,
                        session_id TEXT NOT NULL DEFAULT '',
                        bucket TEXT NOT NULL DEFAULT '',
                        sort_key TEXT NOT NULL DEFAULT '',
                        created_at INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY(kind, id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_dwell_kind_created
                        ON dwell_items(kind, created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_dwell_whisper_session
                        ON dwell_items(kind, session_id, created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_dwell_bucket
                        ON dwell_items(kind, bucket, created_at ASC, id ASC);
                    CREATE INDEX IF NOT EXISTS idx_dwell_sort
                        ON dwell_items(kind, sort_key ASC, created_at ASC, id ASC);
                    """
                )
            # BEGIN IMMEDIATE serializes the one-time migration across workers.
            with get_db() as db:
                db.execute("BEGIN IMMEDIATE")
                _migrate_legacy_json(db)
        except DwellLifeError:
            raise
        except Exception as exc:
            raise DwellStorageError(f"生活区 SQLite 初始化失败：{exc}") from exc
        _SCHEMA_KEY = key


def _begin_write(db: Any) -> None:
    db.execute("BEGIN IMMEDIATE")


def _rows(kind: str, *, order: str = "created_at ASC, id ASC") -> list[dict[str, Any]]:
    ensure_schema()
    with get_db() as db:
        values = db.execute(
            f"SELECT id, payload_json FROM dwell_items WHERE kind=? ORDER BY {order}",
            (kind,),
        ).fetchall()
    return [_decode_payload(row["payload_json"], item_id=row["id"]) for row in values]


def _item_for_update(db: Any, kind: str, item_id: str, *, bucket: str | None = None) -> dict[str, Any] | None:
    where = "kind=? AND id=?"
    params: list[Any] = [kind, _text(item_id, 64)]
    if bucket is not None:
        where += " AND bucket=?"
        params.append(_text(bucket, 24))
    row = db.execute(
        f"SELECT id, payload_json FROM dwell_items WHERE {where}", params
    ).fetchone()
    if not row:
        return None
    return _decode_payload(row["payload_json"], item_id=row["id"])


def _replace_item(
    db: Any,
    kind: str,
    item: dict[str, Any],
    *,
    session_id: str = "",
    bucket: str = "",
    sort_key: str = "",
) -> None:
    _insert_row(
        db,
        kind,
        item,
        session_id=session_id,
        bucket=bucket,
        sort_key=sort_key,
        replace=True,
    )


def health() -> dict[str, Any]:
    try:
        ensure_schema()
        with get_db() as db:
            rows = db.execute(
                "SELECT id, payload_json FROM dwell_items ORDER BY kind, id"
            ).fetchall()
            for row in rows:
                _decode_payload(row["payload_json"], item_id=row["id"])
        return {
            "health": "ok",
            "detail": f"生活空间使用 SQLite 事务存储；{len(rows)} 条记录可读取",
            "path": str(models.DB_PATH),
        }
    except DwellLifeError as exc:
        return {"health": "error", "detail": str(exc), "path": str(models.DB_PATH)}
    except Exception:
        return {
            "health": "error",
            "detail": "生活空间 SQLite 暂不可用",
            "path": str(models.DB_PATH),
        }


# ── Whispers ────────────────────────────────────────────────────────────────

def whispers(limit: int = 200, session_id: str | None = None) -> list[dict[str, Any]]:
    ensure_schema()
    size = max(1, min(int(limit or 200), MAX_WHISPERS))
    where = "kind='whisper'"
    params: list[Any] = []
    if session_id is not None:
        where += " AND session_id=?"
        params.append(_text(session_id, 120))
    params.append(size)
    with get_db() as db:
        rows = db.execute(
            f"""SELECT id, payload_json FROM dwell_items
                WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?""",
            params,
        ).fetchall()
    items = [_decode_payload(row["payload_json"], item_id=row["id"]) for row in rows]
    items.reverse()
    return items


def add_whisper(text: str, who: str = "her", session_id: str = "") -> dict[str, Any]:
    body = _text(text, 2000)
    if not body:
        raise ValueError("悄悄话不能为空")
    sid = _text(session_id, 120)
    if not sid:
        raise ValueError("请先进入一个聊天窗口，再放入悄悄话")
    role = "gu" if who == "gu" else "her"
    item = {"id": _id(), "who": role, "text": body, "at": _now(), "session_id": sid}
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        _insert_row(db, "whisper", item, session_id=sid)
        overflow = db.execute(
            """SELECT id FROM dwell_items WHERE kind='whisper'
               ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?""",
            (MAX_WHISPERS,),
        ).fetchall()
        db.executemany(
            "DELETE FROM dwell_items WHERE kind='whisper' AND id=?",
            [(row["id"],) for row in overflow],
        )
    return item


def delete_whisper(item_id: str, session_id: str) -> bool:
    target = _text(item_id, 64)
    sid = _text(session_id, 120)
    if not sid:
        raise ValueError("删除悄悄话时缺少当前聊天窗口")
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        cursor = db.execute(
            "DELETE FROM dwell_items WHERE kind='whisper' AND id=? AND session_id=?",
            (target, sid),
        )
        return cursor.rowcount > 0


def whisper_context(session_id: str, limit: int = 5) -> str:
    """Return only this conversation's recent user whispers."""
    sid = _text(session_id, 120)
    if not sid:
        return ""
    items = [
        item for item in whispers(40, session_id=sid)
        if item.get("who") == "her" and item.get("text")
    ]
    if not items:
        return ""
    lines = []
    now = _now()
    for item in items[-max(1, min(limit, 8)):]:
        age = max(0, now - int(item.get("at") or now))
        if age < 3600:
            label = f"{max(1, age // 60)} 分钟前"
        elif age < 86400:
            label = f"{max(1, age // 3600)} 小时前"
        else:
            label = f"{max(1, age // 86400)} 天前"
        lines.append(f"- {label}：{_text(item.get('text'), 800)}")
    return (
        "【她放在悄悄话抽屉里的话】\n"
        "这些内容是她主动写下、允许你知道的背景。不要说‘我看到你写的了’，"
        "不要逐字引用，也不要为了证明你知道而主动点破；只让它自然影响你接下来的表达。\n"
        + "\n".join(lines)
    )


# ── Todos ───────────────────────────────────────────────────────────────────

def todos() -> dict[str, list[dict[str, Any]]]:
    ensure_schema()
    result = {"mine": [], "hers": []}
    with get_db() as db:
        rows = db.execute(
            """SELECT id, bucket, payload_json FROM dwell_items
               WHERE kind='todo' ORDER BY created_at ASC, id ASC"""
        ).fetchall()
    for row in rows:
        side = row["bucket"] if row["bucket"] in result else "hers"
        result[side].append(_decode_payload(row["payload_json"], item_id=row["id"]))
    return result


def mutate_todo(
    action: str,
    side: str,
    *,
    text: str = "",
    at: str = "",
    item_id: str = "",
    include_change: bool = False,
) -> dict[str, Any]:
    if side not in {"mine", "hers"}:
        raise ValueError("待办分栏无效")
    action = _text(action, 24)
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        if action == "add":
            body = _text(text, 500)
            if not body:
                raise ValueError("待办内容不能为空")
            clock = _text(at, 5)
            if clock:
                try:
                    datetime.strptime(clock, "%H:%M")
                except ValueError as exc:
                    raise ValueError("时间必须是有效的 HH:MM（00:00–23:59）") from exc
            count = int(db.execute(
                "SELECT COUNT(*) FROM dwell_items WHERE kind='todo' AND bucket=?",
                (side,),
            ).fetchone()[0])
            if count >= MAX_TODOS_PER_SIDE:
                raise ValueError(f"每个待办分栏最多 {MAX_TODOS_PER_SIDE} 条")
            item = {"id": _id(), "text": body, "done": False, "at": clock, "made": _now()}
            _insert_row(db, "todo", item, bucket=side)
        elif action == "toggle":
            item = _item_for_update(db, "todo", item_id, bucket=side)
            if not item:
                raise KeyError("没有找到这条待办")
            item["done"] = not bool(item.get("done"))
            _replace_item(db, "todo", item, bucket=side)
        elif action == "del":
            cursor = db.execute(
                "DELETE FROM dwell_items WHERE kind='todo' AND id=? AND bucket=?",
                (_text(item_id, 64), side),
            )
            if cursor.rowcount <= 0:
                raise KeyError("没有找到这条待办")
        else:
            raise ValueError("未知待办操作")
    result = todos()
    if include_change:
        if action == "del":
            result["_change"] = {"action": "del", "side": side, "id": _text(item_id, 64)}
        else:
            result["_change"] = {"action": action, "side": side, "item": dict(item)}
    return result


# ── Calendar ────────────────────────────────────────────────────────────────

def _valid_day(value: Any) -> str:
    day = _text(value, 10)
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD") from exc
    return day


def calendar_items() -> list[dict[str, Any]]:
    return _rows("calendar", order="sort_key ASC, created_at ASC, id ASC")


def add_calendar(date: str, title: str, note: str = "", mood: str = "") -> dict[str, Any]:
    day = _valid_day(date)
    name = _text(title, 160)
    if not name:
        raise ValueError("日程标题不能为空")
    item = {
        "id": _id(), "date": day, "title": name,
        "note": _text(note, 1200), "mood": _text(mood, 24), "made": _now(),
    }
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        count = int(db.execute(
            "SELECT COUNT(*) FROM dwell_items WHERE kind='calendar'"
        ).fetchone()[0])
        if count >= MAX_CALENDAR_ITEMS:
            raise ValueError(f"日历最多保存 {MAX_CALENDAR_ITEMS} 条")
        _insert_row(db, "calendar", item, sort_key=day)
    return item


def update_calendar(
    item_id: str,
    *,
    date: Any = None,
    title: Any = None,
    note: Any = None,
    mood: Any = None,
) -> dict[str, Any]:
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        item = _item_for_update(db, "calendar", item_id)
        if not item:
            raise KeyError("没有找到这条日程")
        if date is not None:
            item["date"] = _valid_day(date)
        if title is not None:
            name = _text(title, 160)
            if not name:
                raise ValueError("日程标题不能为空")
            item["title"] = name
        if note is not None:
            item["note"] = _text(note, 1200)
        if mood is not None:
            item["mood"] = _text(mood, 24)
        _replace_item(db, "calendar", item, sort_key=_text(item.get("date"), 10))
    return item


def delete_calendar(item_id: str) -> bool:
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        cursor = db.execute(
            "DELETE FROM dwell_items WHERE kind='calendar' AND id=?",
            (_text(item_id, 64),),
        )
        return cursor.rowcount > 0


# ── Diary ───────────────────────────────────────────────────────────────────

def diary_entries(limit: int = 200) -> list[dict[str, Any]]:
    ensure_schema()
    size = max(1, min(int(limit or 200), MAX_DIARY_ENTRIES))
    with get_db() as db:
        rows = db.execute(
            """SELECT id, payload_json FROM dwell_items WHERE kind='diary'
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (size,),
        ).fetchall()
    return [_decode_payload(row["payload_json"], item_id=row["id"]) for row in rows]


def add_diary(text: str, title: str = "", who: str = "her") -> dict[str, Any]:
    body = _text(text, 8000)
    if not body:
        raise ValueError("日记内容不能为空")
    item = {
        "id": _id(), "title": _text(title, 100), "text": body,
        "who": "gu" if who == "gu" else "her", "at": _now(),
    }
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        _insert_row(db, "diary", item)
        overflow = db.execute(
            """SELECT id FROM dwell_items WHERE kind='diary'
               ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?""",
            (MAX_DIARY_ENTRIES,),
        ).fetchall()
        db.executemany(
            "DELETE FROM dwell_items WHERE kind='diary' AND id=?",
            [(row["id"],) for row in overflow],
        )
    return item


def update_diary(item_id: str, *, text: Any = None, title: Any = None) -> dict[str, Any]:
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        item = _item_for_update(db, "diary", item_id)
        if not item:
            raise KeyError("没有找到这篇日记")
        if text is not None:
            body = _text(text, 8000)
            if not body:
                raise ValueError("日记内容不能为空")
            item["text"] = body
        if title is not None:
            item["title"] = _text(title, 100)
        _replace_item(db, "diary", item)
    return item


def delete_diary(item_id: str) -> bool:
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        cursor = db.execute(
            "DELETE FROM dwell_items WHERE kind='diary' AND id=?",
            (_text(item_id, 64),),
        )
        return cursor.rowcount > 0


# ── Reading nook ────────────────────────────────────────────────────────────

def books() -> list[dict[str, Any]]:
    return _rows("book", order="created_at ASC, id ASC")


def mutate_book(
    action: str,
    *,
    item_id: str = "",
    title: Any = None,
    author: Any = None,
    progress: Any = None,
    note: Any = None,
) -> list[dict[str, Any]]:
    action = _text(action, 24)
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        if action == "add":
            name = _text(title, 180)
            if not name:
                raise ValueError("书名不能为空")
            try:
                pct = max(0, min(100, int(progress or 0)))
            except (TypeError, ValueError):
                pct = 0
            count = int(db.execute(
                "SELECT COUNT(*) FROM dwell_items WHERE kind='book'"
            ).fetchone()[0])
            if count >= MAX_BOOKS:
                raise ValueError(f"共读最多保存 {MAX_BOOKS} 本")
            item = {
                "id": _id(), "title": name, "author": _text(author, 120),
                "progress": pct, "note": _text(note, 2000), "made": _now(),
            }
            _insert_row(db, "book", item)
        elif action == "update":
            item = _item_for_update(db, "book", item_id)
            if not item:
                raise KeyError("没有找到这本书")
            if title is not None:
                clean_title = _text(title, 180)
                if not clean_title:
                    raise ValueError("书名不能为空或纯空白")
                item["title"] = clean_title
            if author is not None:
                item["author"] = _text(author, 120)
            if note is not None:
                item["note"] = _text(note, 2000)
            if progress is not None:
                try:
                    item["progress"] = max(0, min(100, int(progress)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("阅读进度必须是 0–100 的数字") from exc
            _replace_item(db, "book", item)
        elif action == "del":
            cursor = db.execute(
                "DELETE FROM dwell_items WHERE kind='book' AND id=?",
                (_text(item_id, 64),),
            )
            if cursor.rowcount <= 0:
                raise KeyError("没有找到这本书")
        else:
            raise ValueError("未知共读操作")
    return books()


# ── Music cards ─────────────────────────────────────────────────────────────

def _valid_music_url(value: Any) -> tuple[str, str]:
    link = _text(value, 2048)
    parsed = urlparse(link)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("音乐链接需要是 http 或 https 地址")
    return link, parsed.netloc


def music_cards() -> list[dict[str, Any]]:
    return _rows("music", order="created_at DESC, id DESC")[:MAX_MUSIC]


def mutate_music(
    action: str,
    *,
    item_id: str = "",
    url: Any = None,
    title: Any = None,
    artist: Any = None,
    note: Any = None,
) -> list[dict[str, Any]]:
    action = _text(action, 24)
    ensure_schema()
    with get_db() as db:
        _begin_write(db)
        if action == "add":
            link, host = _valid_music_url(url)
            count = int(db.execute(
                "SELECT COUNT(*) FROM dwell_items WHERE kind='music'"
            ).fetchone()[0])
            if count >= MAX_MUSIC:
                raise ValueError(f"音乐卡片最多保存 {MAX_MUSIC} 张")
            item = {
                "id": _id(), "url": link, "title": _text(title, 180) or host,
                "artist": _text(artist, 160), "note": _text(note, 1200),
                "made": _now(),
            }
            _insert_row(db, "music", item)
        elif action == "update":
            item = _item_for_update(db, "music", item_id)
            if not item:
                raise KeyError("没有找到这张音乐卡片")
            host = urlparse(str(item.get("url") or "")).netloc
            if url is not None:
                item["url"], host = _valid_music_url(url)
            if title is not None:
                item["title"] = _text(title, 180) or host
            if artist is not None:
                item["artist"] = _text(artist, 160)
            if note is not None:
                item["note"] = _text(note, 1200)
            _replace_item(db, "music", item)
        elif action == "del":
            cursor = db.execute(
                "DELETE FROM dwell_items WHERE kind='music' AND id=?",
                (_text(item_id, 64),),
            )
            if cursor.rowcount <= 0:
                raise KeyError("没有找到这张音乐卡片")
        else:
            raise ValueError("未知音乐卡片操作")
    return music_cards()


def summary(session_id: str = "") -> dict[str, Any]:
    """Return counts and the mobile previews from one SQLite snapshot."""
    ensure_schema()
    sid = _text(session_id, 120)
    grouped: dict[str, list[dict[str, Any]]] = {
        "whisper": [], "todo": [], "calendar": [], "diary": [], "book": [], "music": [],
    }
    todo_sides: dict[str, list[dict[str, Any]]] = {"mine": [], "hers": []}
    with get_db() as db:
        rows = db.execute(
            """SELECT kind, id, session_id, bucket, payload_json
               FROM dwell_items
               WHERE kind IN ('whisper','todo','calendar','diary','book','music')
               ORDER BY created_at ASC, id ASC"""
        ).fetchall()
    for row in rows:
        item = _decode_payload(row["payload_json"], item_id=row["id"])
        kind = str(row["kind"])
        if kind == "todo":
            side = row["bucket"] if row["bucket"] in todo_sides else "hers"
            todo_sides[side].append(item)
        elif kind == "whisper":
            if sid and str(row["session_id"] or "") == sid:
                grouped[kind].append(item)
        elif kind in grouped:
            grouped[kind].append(item)

    diaries = sorted(grouped["diary"], key=lambda item: int(item.get("at") or 0), reverse=True)
    whisper_items = sorted(grouped["whisper"], key=lambda item: int(item.get("at") or 0), reverse=True)
    book_items = sorted(grouped["book"], key=lambda item: int(item.get("made") or 0), reverse=True)
    music_items = sorted(grouped["music"], key=lambda item: int(item.get("made") or 0), reverse=True)
    current_book = next(
        (item for item in book_items if int(item.get("progress") or 0) < 100),
        book_items[0] if book_items else None,
    )
    return {
        "todos": {
            "mine_total": len(todo_sides["mine"]),
            "mine_open": sum(1 for item in todo_sides["mine"] if not item.get("done")),
            "hers_total": len(todo_sides["hers"]),
            "hers_open": sum(1 for item in todo_sides["hers"] if not item.get("done")),
        },
        "calendar_count": len(grouped["calendar"]),
        "diary_count": len(grouped["diary"]),
        "whisper_count": len(grouped["whisper"]),
        "book_count": len(grouped["book"]),
        "music_count": len(grouped["music"]),
        "previews": {
            "recent_diary": diaries[0] if diaries else None,
            "recent_whisper": whisper_items[0] if whisper_items else None,
            "current_book": current_book,
            "current_music": music_items[0] if music_items else None,
        },
    }
