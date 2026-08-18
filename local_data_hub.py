"""Local-only data ownership tools: backup, restore, search, favorites and export."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config
from local_restore_runtime import (
    BACKUP_KIND,
    BACKUP_SCHEMA,
    pending_paths,
    utcnow,
    validate_backup,
)
from models import get_db
from runtime_paths import DATA_DIR


_SECRET_NAMES = {".jtyhome.env", "access-token.txt"}
_TRANSIENT_ROOTS = {
    "conversation-imports", "diagnostics", "restore-rollbacks", "logs",
    "last-restore.json",
}
_EXPORT_FORMATS = {"json", "txt", "md"}


def _safe_name(value: str, fallback: str = "conversation") -> str:
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", str(value or "")).strip(" .-")
    text = re.sub(r"\s+", " ", text)[:80]
    return text or fallback


def _like_pattern(value: str) -> str:
    return "%" + str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(content: str, query: str, radius: int = 110) -> str:
    text = str(content or "").replace("\r", " ").replace("\n", " ")
    lowered = text.casefold()
    at = lowered.find(str(query or "").casefold())
    if at < 0:
        return text[: radius * 2]
    start = max(0, at - radius)
    end = min(len(text), at + len(query) + radius)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


class LocalDataHub:
    @staticmethod
    def _data_root_and_db() -> tuple[Path, Path]:
        data_root = Path(DATA_DIR).resolve()
        configured_db = Path(config.DB_PATH).resolve()
        if configured_db.parent != data_root:
            raise ValueError(
                "当前主数据库不在 JTYHOME_DATA_DIR 内，暂不能安全执行整机备份或恢复"
            )
        return data_root, configured_db

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS message_favorites (
                    message_id INTEGER PRIMARY KEY,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_message_favorites_updated
                    ON message_favorites(updated_at DESC);
                """
            )

    def search_messages(self, query: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.ensure_schema()
        text = str(query or "").strip()[:300]
        if not text:
            return {"items": [], "total": 0, "query": "", "limit": limit, "offset": offset}
        size = max(1, min(int(limit), 100))
        skip = max(0, int(offset))
        pattern = _like_pattern(text)
        with get_db() as db:
            total = int(db.execute(
                """SELECT COUNT(*) FROM messages m
                   WHERE m.role IN ('user','assistant')
                     AND m.content LIKE ? ESCAPE '\\' COLLATE NOCASE""",
                (pattern,),
            ).fetchone()[0])
            rows = db.execute(
                """SELECT m.id, m.session_id, m.role, m.content, m.created_at,
                          s.title, EXISTS(
                            SELECT 1 FROM message_favorites f WHERE f.message_id=m.id
                          ) AS is_favorite
                   FROM messages m JOIN sessions s ON s.id=m.session_id
                   WHERE m.role IN ('user','assistant')
                     AND m.content LIKE ? ESCAPE '\\' COLLATE NOCASE
                   ORDER BY m.id DESC LIMIT ? OFFSET ?""",
                (pattern, size, skip),
            ).fetchall()
            items = []
            for row in rows:
                position = int(db.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id=? AND id<=?",
                    (row["session_id"], row["id"]),
                ).fetchone()[0])
                items.append({
                    "message_id": int(row["id"]),
                    "session_id": str(row["session_id"]),
                    "session_title": str(row["title"] or "新对话"),
                    "role": str(row["role"]),
                    "snippet": _snippet(str(row["content"] or ""), text),
                    "created_at": str(row["created_at"] or ""),
                    "history_position": position,
                    "is_favorite": bool(row["is_favorite"]),
                })
        return {"items": items, "total": total, "query": text, "limit": size, "offset": skip}

    def favorite(self, message_id: int, note: str = "") -> dict[str, Any]:
        self.ensure_schema()
        now = utcnow()
        with get_db() as db:
            row = db.execute(
                "SELECT id, session_id FROM messages WHERE id=?", (int(message_id),)
            ).fetchone()
            if not row:
                raise ValueError("没有找到这条消息")
            db.execute(
                """INSERT INTO message_favorites(message_id, note, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     note=excluded.note, updated_at=excluded.updated_at""",
                (int(message_id), str(note or "")[:500], now, now),
            )
        return {"ok": True, "message_id": int(message_id), "is_favorite": True}

    def unfavorite(self, message_id: int) -> bool:
        self.ensure_schema()
        with get_db() as db:
            return db.execute(
                "DELETE FROM message_favorites WHERE message_id=?", (int(message_id),)
            ).rowcount > 0

    def list_favorites(self, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        self.ensure_schema()
        size = max(1, min(int(limit), 200))
        skip = max(0, int(offset))
        with get_db() as db:
            total = int(db.execute("SELECT COUNT(*) FROM message_favorites").fetchone()[0])
            rows = db.execute(
                """SELECT f.message_id, f.note, f.created_at AS favorited_at,
                          m.session_id, m.role, m.content, m.created_at,
                          s.title
                   FROM message_favorites f
                   JOIN messages m ON m.id=f.message_id
                   JOIN sessions s ON s.id=m.session_id
                   ORDER BY f.updated_at DESC LIMIT ? OFFSET ?""",
                (size, skip),
            ).fetchall()
            items = []
            for row in rows:
                position = int(db.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id=? AND id<=?",
                    (row["session_id"], row["message_id"]),
                ).fetchone()[0])
                items.append({
                    "message_id": int(row["message_id"]),
                    "session_id": str(row["session_id"]),
                    "session_title": str(row["title"] or "新对话"),
                    "role": str(row["role"]),
                    "snippet": _snippet(str(row["content"] or ""), ""),
                    "note": str(row["note"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "favorited_at": str(row["favorited_at"] or ""),
                    "history_position": position,
                })
        return {"items": items, "total": total, "limit": size, "offset": skip}

    @staticmethod
    def _reasoning_ready() -> None:
        from thinking_vault import thinking_vault

        thinking_vault.ensure_schema()

    def _session_export_rows(self, session_id: str) -> tuple[dict[str, Any], Iterable[sqlite3.Row]]:
        self._reasoning_ready()
        with get_db() as db:
            session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise ValueError("会话不存在")
            rows = db.execute(
                """SELECT m.*, r.content AS reasoning_content,
                          r.provider AS reasoning_provider, r.model AS reasoning_model,
                          EXISTS(SELECT 1 FROM message_favorites f WHERE f.message_id=m.id)
                            AS is_favorite
                   FROM messages m
                   LEFT JOIN reasoning_traces r ON r.message_id=m.id
                   WHERE m.session_id=? ORDER BY m.id ASC""",
                (session_id,),
            ).fetchall()
        return dict(session), rows

    @staticmethod
    def _export_message(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except Exception:
            metadata = {}
        item = {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "created_at": str(row["created_at"] or ""),
            "provider": str(row["provider"] or ""),
            "model": str(row["model"] or ""),
            "favorite": bool(row["is_favorite"]),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        reasoning = str(row["reasoning_content"] or "")
        if reasoning:
            item["thinking"] = {
                "content": reasoning,
                "provider": str(row["reasoning_provider"] or ""),
                "model": str(row["reasoning_model"] or ""),
                "source": "provider_explicit_reasoning",
            }
        return item

    def export_session(self, session_id: str, fmt: str = "json") -> tuple[Path, str]:
        output_format = str(fmt or "json").lower()
        if output_format not in _EXPORT_FORMATS:
            raise ValueError("导出格式只支持 JSON、TXT 或 Markdown")
        session, rows = self._session_export_rows(session_id)
        suffix = ".md" if output_format == "md" else f".{output_format}"
        handle = tempfile.NamedTemporaryFile(prefix="jtyhome-export-", suffix=suffix, delete=False)
        path = Path(handle.name)
        handle.close()
        title = _safe_name(str(session.get("title") or "conversation"))
        filename = f"{title}-{session_id[:8]}{suffix}"
        with path.open("w", encoding="utf-8", newline="\n") as target:
            if output_format == "json":
                target.write("{\n")
                target.write('  "format": "jtyhome-conversation-v1",\n')
                target.write('  "exported_at": ' + json.dumps(utcnow(), ensure_ascii=False) + ",\n")
                target.write('  "session": ' + json.dumps(session, ensure_ascii=False, default=str) + ",\n")
                target.write('  "messages": [\n')
                first = True
                for row in rows:
                    if not first:
                        target.write(",\n")
                    target.write("    " + json.dumps(self._export_message(row), ensure_ascii=False, default=str))
                    first = False
                target.write("\n  ]\n}\n")
            else:
                if output_format == "md":
                    target.write(f"# {session.get('title') or '新对话'}\n\n")
                    target.write(f"- 会话 ID：`{session_id}`\n- 导出时间：{utcnow()}\n\n")
                else:
                    target.write(f"{session.get('title') or '新对话'}\n会话 ID：{session_id}\n导出时间：{utcnow()}\n\n")
                for row in rows:
                    item = self._export_message(row)
                    speaker = "你" if item["role"] == "user" else "大西瓜"
                    if output_format == "md":
                        target.write(f"## {speaker} · {item['created_at']}\n\n{item['content']}\n\n")
                        if item.get("thinking"):
                            target.write("<details><summary>导出文件中明确提供的思考</summary>\n\n")
                            target.write(item["thinking"]["content"] + "\n\n</details>\n\n")
                    else:
                        target.write(f"[{speaker} · {item['created_at']}]\n{item['content']}\n")
                        if item.get("thinking"):
                            target.write(f"[显式思考]\n{item['thinking']['content']}\n")
                        target.write("\n")
        return path, filename

    def export_all(self, fmt: str = "json") -> tuple[Path, str]:
        output_format = str(fmt or "json").lower()
        if output_format not in _EXPORT_FORMATS:
            raise ValueError("导出格式只支持 JSON、TXT 或 Markdown")
        with get_db() as db:
            sessions = [dict(row) for row in db.execute(
                "SELECT * FROM sessions ORDER BY created_at ASC, rowid ASC"
            ).fetchall()]
        handle = tempfile.NamedTemporaryFile(prefix="jtyhome-all-conversations-", suffix=".zip", delete=False)
        archive_path = Path(handle.name)
        handle.close()
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", json.dumps({
                "format": "jtyhome-conversation-export-v1",
                "exported_at": utcnow(),
                "app_version": config.APP_VERSION,
                "conversation_count": len(sessions),
                "file_format": output_format,
            }, ensure_ascii=False, indent=2))
            for index, session in enumerate(sessions, start=1):
                path, filename = self.export_session(str(session["id"]), output_format)
                try:
                    archive.write(path, f"conversations/{index:05d}-{filename}")
                finally:
                    path.unlink(missing_ok=True)
        date = datetime.now().strftime("%Y%m%d-%H%M%S")
        return archive_path, f"jtyhome-conversations-{date}.zip"

    @staticmethod
    def _snapshot_sqlite(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        target_conn = sqlite3.connect(destination)
        try:
            source_conn.execute("PRAGMA busy_timeout=30000")
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()

    @staticmethod
    def _write_entry(archive: zipfile.ZipFile, source: Path, arcname: str) -> dict[str, Any]:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as incoming, archive.open(arcname, "w") as outgoing:
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        return {"sha256": digest.hexdigest(), "size": size}

    def create_backup(self) -> tuple[Path, str, dict[str, Any]]:
        data_root, configured_db = self._data_root_and_db()
        data_root.mkdir(parents=True, exist_ok=True)
        snapshot_root = Path(tempfile.mkdtemp(prefix="jtyhome-db-snapshots-"))
        handle = tempfile.NamedTemporaryFile(prefix="jtyhome-backup-", suffix=".zip", delete=False)
        archive_path = Path(handle.name)
        handle.close()
        entries: dict[str, Any] = {}
        replace_roots: set[str] = set()
        try:
            db_sources: dict[str, Path] = {}
            if configured_db.exists():
                db_sources[configured_db.name] = configured_db
            for item in data_root.glob("*.db"):
                if item.is_file():
                    db_sources[item.name] = item
            db_snapshots: dict[str, Path] = {}
            for name, source in db_sources.items():
                snapshot = snapshot_root / name
                self._snapshot_sqlite(source, snapshot)
                db_snapshots[name] = snapshot

            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name, snapshot in sorted(db_snapshots.items()):
                    arcname = f"data/{name}"
                    entries[arcname] = self._write_entry(archive, snapshot, arcname)
                    replace_roots.add(name)
                for source in sorted(data_root.rglob("*")):
                    if not source.is_file() or source.is_symlink():
                        continue
                    relative = source.relative_to(data_root)
                    root_name = relative.parts[0]
                    if root_name in _SECRET_NAMES or root_name in _TRANSIENT_ROOTS:
                        continue
                    if root_name.startswith("."):
                        continue
                    if root_name.startswith(".jtyhome-restore-"):
                        continue
                    if source.name.endswith(("-wal", "-shm")) or source.suffix == ".db":
                        continue
                    if source.name == ".gitkeep":
                        continue
                    arcname = "data/" + relative.as_posix()
                    entries[arcname] = self._write_entry(archive, source, arcname)
                    replace_roots.add(root_name)
                # Runtime owns these directories even when they are currently
                # empty, so a restore removes newer files that are absent here.
                replace_roots.update({"uploads", "stickers", "memory"})
                manifest = {
                    "kind": BACKUP_KIND,
                    "schema": BACKUP_SCHEMA,
                    "app_version": config.APP_VERSION,
                    "created_at": utcnow(),
                    "entries": entries,
                    "replace_roots": sorted(replace_roots),
                    "credentials_included": False,
                    "credentials_note": "API Key 与当前设备配对码未写入备份",
                }
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            date = datetime.now().strftime("%Y%m%d-%H%M%S")
            return archive_path, f"jtyhome-local-backup-{date}.zip", manifest
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(snapshot_root, ignore_errors=True)

    def stage_restore(self, uploaded_path: Path, *, original_name: str = "backup.zip") -> dict[str, Any]:
        data_root, configured_db = self._data_root_and_db()
        source = Path(uploaded_path)
        manifest = validate_backup(source, required_db_name=configured_db.name)
        # A fully independent rollback is created before the pending restore is
        # accepted. It lives beside the data directory, outside restore scope.
        rollback_temp, rollback_name, _ = self.create_backup()
        rollback_dir = data_root.parent / f"{data_root.name}-restore-rollbacks"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        rollback_path = rollback_dir / rollback_name
        os.replace(rollback_temp, rollback_path)

        pending_archive, marker = pending_paths(data_root)
        staged = pending_archive.with_suffix(".tmp")
        shutil.copy2(source, staged)
        os.replace(staged, pending_archive)
        marker_payload = {
            "staged_at": utcnow(),
            "original_name": str(original_name or "backup.zip")[:240],
            "backup_created_at": manifest.get("created_at", ""),
            "backup_app_version": manifest.get("app_version", ""),
            "rollback_file": str(rollback_path),
        }
        marker.write_text(json.dumps(marker_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ok": True,
            "restart_required": True,
            "backup_created_at": manifest.get("created_at", ""),
            "backup_app_version": manifest.get("app_version", ""),
            "rollback_file": rollback_path.name,
            "credentials_preserved": True,
        }

    def restore_status(self) -> dict[str, Any]:
        data_root = Path(DATA_DIR).resolve()
        pending_archive, marker = pending_paths(Path(config.DB_PATH).resolve().parent)
        receipt_path = data_root / "last-restore.json"
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            receipt = None
        return {
            "pending": pending_archive.exists() and marker.exists(),
            "last_restore": receipt if isinstance(receipt, dict) else None,
            "credentials_in_backup": False,
        }

    def health(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            favorites = int(db.execute("SELECT COUNT(*) FROM message_favorites").fetchone()[0])
        return {"health": "ok", "detail": f"本机备份、导出、全文搜索与 {favorites} 条收藏可用"}


local_data_hub = LocalDataHub()
