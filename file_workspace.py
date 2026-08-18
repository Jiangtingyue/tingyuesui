"""v6.0.2 本地资料工作室。

附件二进制仍由 AttachmentService 安全落盘；这里仅建立可搜索索引，并
记录每段会话如何使用资料。文件正文不会写进索引表。

会话模式分为三档：
- off：只留在资料库，不进入模型输入；
- retrieval：按当前问题抽取少量相关片段；
- pinned：每一轮只注入短索引与当前问题相关片段；全文仍留在本机。

``active`` 字段继续保留给 6.0.1 数据与旧 API；它表示 mode != off。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import DATA_DIR, UPLOAD_DIR
from typing import Any, Iterable

from models import get_db


_NATIVE_DELIVERY_PATH = DATA_DIR / "workspace-native-delivery.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileWorkspace:
    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS workspace_files (
                    attachment_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT DEFAULT 'file',
                    mime_type TEXT DEFAULT 'application/octet-stream',
                    size INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'ready',
                    tags_json TEXT DEFAULT '[]',
                    note TEXT DEFAULT '',
                    pinned INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_files_recent
                    ON workspace_files(pinned DESC, last_used_at DESC, created_at DESC);

                CREATE TABLE IF NOT EXISTS workspace_session_files (
                    session_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    active INTEGER DEFAULT 1,
                    mode TEXT NOT NULL DEFAULT 'pinned',
                    added_at TEXT NOT NULL,
                    last_used_at TEXT,
                    PRIMARY KEY(session_id, attachment_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY(attachment_id) REFERENCES workspace_files(attachment_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_session_active
                    ON workspace_session_files(session_id, active, last_used_at DESC);
                """
            )
            columns = {
                str(row[1]) for row in db.execute(
                    "PRAGMA table_info(workspace_session_files)"
                ).fetchall()
            }
            if "mode" not in columns:
                db.execute(
                    "ALTER TABLE workspace_session_files "
                    "ADD COLUMN mode TEXT NOT NULL DEFAULT 'off'"
                )
                # 6.0.1 only had active=1/0. Preserve the user's explicit
                # persistent choices during migration.
                db.execute(
                    "UPDATE workspace_session_files "
                    "SET mode=CASE WHEN active=1 THEN 'pinned' ELSE 'off' END"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_session_mode "
                "ON workspace_session_files(session_id, mode, last_used_at DESC)"
            )

    def register(self, item: dict[str, Any]) -> bool:
        attachment_id = str(item.get("id") or "")
        if not attachment_id:
            return False
        self.ensure_schema()
        created_at = str(item.get("created_at") or _now())
        with get_db() as db:
            db.execute(
                """INSERT INTO workspace_files
                   (attachment_id, name, kind, mime_type, size, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(attachment_id) DO UPDATE SET
                     name=excluded.name, kind=excluded.kind,
                     mime_type=excluded.mime_type, size=excluded.size,
                     status=excluded.status""",
                (
                    attachment_id,
                    str(item.get("name") or "attachment"),
                    str(item.get("kind") or "file"),
                    str(item.get("mime_type") or "application/octet-stream"),
                    int(item.get("size") or 0),
                    str(item.get("status") or "ready"),
                    created_at,
                ),
            )
        return True

    def migrate_existing(self) -> int:
        """把旧版本已经存在的附件加入索引；不移动也不重写原文件。"""
        self.ensure_schema()
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        added = 0
        for path in UPLOAD_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text("utf-8"))
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                with get_db() as db:
                    exists = db.execute(
                        "SELECT 1 FROM workspace_files WHERE attachment_id=?",
                        (str(item["id"]),),
                    ).fetchone()
                if not exists and self.register(item):
                    added += 1
            except Exception:
                continue
        return added

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        item = dict(row)
        try:
            tags = json.loads(item.pop("tags_json", "[]") or "[]")
        except Exception:
            tags = []
        attachment_id = str(item.pop("attachment_id"))
        item.update({
            "id": attachment_id,
            "tags": tags if isinstance(tags, list) else [],
            "pinned": bool(item.get("pinned")),
            "mode": str(item.get("mode") or ("pinned" if item.get("active") else "off")),
            "active": str(item.get("mode") or "off") != "off" or bool(item.get("active", 0)),
            "preview_url": f"/api/attachments/{attachment_id}/content",
        })
        return item

    def list(
        self,
        *,
        session_id: str = "",
        query: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        limit = max(1, min(int(limit or 100), 200))
        query = str(query or "").strip()
        where = ""
        params: list[Any] = []
        if query:
            where = "WHERE (w.name LIKE ? OR w.note LIKE ? OR w.tags_json LIKE ?)"
            pattern = f"%{query}%"
            params.extend([pattern, pattern, pattern])
        if session_id:
            sql = f"""SELECT w.*, COALESCE(s.active, 0) AS active,
                              COALESCE(s.mode, 'off') AS mode,
                              s.added_at AS session_added_at,
                              s.last_used_at AS session_last_used_at
                       FROM workspace_files w
                       LEFT JOIN workspace_session_files s
                         ON s.attachment_id=w.attachment_id AND s.session_id=?
                       {where}
                       ORDER BY CASE COALESCE(s.mode, 'off')
                                  WHEN 'pinned' THEN 0 WHEN 'retrieval' THEN 1 ELSE 2 END,
                                w.pinned DESC,
                                COALESCE(s.last_used_at, w.last_used_at, w.created_at) DESC
                       LIMIT ?"""
            params = [session_id, *params, limit]
        else:
            sql = f"""SELECT w.*, 0 AS active, 'off' AS mode
                       FROM workspace_files w
                       {where}
                       ORDER BY w.pinned DESC,
                                COALESCE(w.last_used_at, w.created_at) DESC
                       LIMIT ?"""
            params.append(limit)
        with get_db() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._public(row) for row in rows]

    def bind_session(
        self,
        session_id: str,
        attachment_ids: Iterable[str],
        *,
        active: bool = True,
        mode: str | None = None,
    ) -> int:
        self.ensure_schema()
        ids = list(dict.fromkeys(str(value or "") for value in attachment_ids if value))
        if not session_id or not ids:
            return 0
        now = _now()
        selected_mode = str(mode or ("pinned" if active else "off")).lower()
        if selected_mode not in {"off", "retrieval", "pinned"}:
            selected_mode = "off"
        selected_active = int(selected_mode != "off")
        changed = 0
        with get_db() as db:
            for attachment_id in ids:
                exists = db.execute(
                    "SELECT 1 FROM workspace_files WHERE attachment_id=?",
                    (attachment_id,),
                ).fetchone()
                if not exists:
                    continue
                db.execute(
                    """INSERT INTO workspace_session_files
                       (session_id, attachment_id, active, mode, added_at, last_used_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(session_id, attachment_id) DO UPDATE SET
                         active=excluded.active, mode=excluded.mode,
                         last_used_at=excluded.last_used_at""",
                    (session_id, attachment_id, selected_active, selected_mode, now, now),
                )
                changed += 1
        return changed

    def set_active(self, session_id: str, attachment_id: str, active: bool) -> bool:
        return self.bind_session(session_id, [attachment_id], active=active) == 1

    def set_mode(self, session_id: str, attachment_id: str, mode: str) -> bool:
        mode = str(mode or "off").lower()
        if mode not in {"off", "retrieval", "pinned"}:
            raise ValueError("资料模式必须是 off、retrieval 或 pinned")
        return self.bind_session(
            session_id, [attachment_id], active=mode != "off", mode=mode
        ) == 1

    def mode_ids(self, session_id: str, mode: str, limit: int = 8) -> list[str]:
        self.ensure_schema()
        mode = str(mode or "off").lower()
        if not session_id or mode not in {"off", "retrieval", "pinned"}:
            return []
        with get_db() as db:
            rows = db.execute(
                """SELECT attachment_id FROM workspace_session_files
                   WHERE session_id=? AND mode=?
                   ORDER BY COALESCE(last_used_at, added_at) DESC LIMIT ?""",
                (session_id, mode, max(1, min(int(limit or 8), 40))),
            ).fetchall()
        return [str(row["attachment_id"]) for row in rows]

    def active_ids(self, session_id: str, limit: int = 4) -> list[str]:
        self.ensure_schema()
        if not session_id:
            return []
        with get_db() as db:
            rows = db.execute(
                """SELECT attachment_id FROM workspace_session_files
                   WHERE session_id=? AND mode!='off'
                   ORDER BY COALESCE(last_used_at, added_at) DESC LIMIT ?""",
                (session_id, max(1, min(int(limit or 4), 8))),
            ).fetchall()
        return [str(row["attachment_id"]) for row in rows]

    def note_used(self, session_id: str, attachment_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(str(value or "") for value in attachment_ids if value))
        if not ids:
            return
        self.ensure_schema()
        now = _now()
        with get_db() as db:
            for attachment_id in ids:
                db.execute(
                    """UPDATE workspace_files
                       SET last_used_at=?, use_count=use_count+1
                       WHERE attachment_id=?""",
                    (now, attachment_id),
                )
                if session_id:
                    db.execute(
                        """UPDATE workspace_session_files SET last_used_at=?
                           WHERE session_id=? AND attachment_id=?""",
                        (now, session_id, attachment_id),
                    )

    @staticmethod
    def _native_delivery_state() -> dict[str, str]:
        try:
            value = json.loads(_NATIVE_DELIVERY_PATH.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def native_was_delivered(self, session_id: str, attachment_id: str) -> bool:
        if not session_id or not attachment_id:
            return False
        key = f"{session_id}:{attachment_id}"
        return key in self._native_delivery_state()

    def note_native_delivered(self, session_id: str, attachment_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(str(value or "") for value in attachment_ids if value))
        if not session_id or not ids:
            return
        state = self._native_delivery_state()
        now = _now()
        for attachment_id in ids:
            state[f"{session_id}:{attachment_id}"] = now
        _NATIVE_DELIVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _NATIVE_DELIVERY_PATH.with_name(f".{_NATIVE_DELIVERY_PATH.name}.tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        temp.replace(_NATIVE_DELIVERY_PATH)

    def update(self, attachment_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_schema()
        fields: list[str] = []
        values: list[Any] = []
        if "pinned" in patch:
            fields.append("pinned=?")
            values.append(int(bool(patch.get("pinned"))))
        if "note" in patch:
            fields.append("note=?")
            values.append(str(patch.get("note") or "")[:500])
        if "tags" in patch:
            raw = patch.get("tags")
            tags = raw if isinstance(raw, list) else str(raw or "").replace("，", ",").split(",")
            tags = [str(tag).strip()[:40] for tag in tags if str(tag).strip()][:20]
            fields.append("tags_json=?")
            values.append(json.dumps(tags, ensure_ascii=False))
        if not fields:
            return None
        values.append(attachment_id)
        with get_db() as db:
            cursor = db.execute(
                f"UPDATE workspace_files SET {', '.join(fields)} WHERE attachment_id=?",
                values,
            )
        if not cursor.rowcount:
            return None
        items = [item for item in self.list(limit=200) if item["id"] == attachment_id]
        return items[0] if items else None

    def delete_record(self, attachment_id: str) -> None:
        self.ensure_schema()
        with get_db() as db:
            db.execute("DELETE FROM workspace_files WHERE attachment_id=?", (attachment_id,))

    def stats(self, session_id: str = "") -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            total = int(db.execute("SELECT COUNT(*) FROM workspace_files").fetchone()[0])
            size = int(db.execute("SELECT COALESCE(SUM(size), 0) FROM workspace_files").fetchone()[0])
            active = 0
            if session_id:
                active = int(db.execute(
                    "SELECT COUNT(*) FROM workspace_session_files WHERE session_id=? AND active=1",
                    (session_id,),
                ).fetchone()[0])
        return {"files": total, "bytes": size, "active": active}

    def mode_stats(self, session_id: str = "") -> dict[str, int]:
        self.ensure_schema()
        result = {"off": 0, "retrieval": 0, "pinned": 0}
        if not session_id:
            return result
        with get_db() as db:
            rows = db.execute(
                """SELECT mode, COUNT(*) AS count
                   FROM workspace_session_files
                   WHERE session_id=? GROUP BY mode""",
                (session_id,),
            ).fetchall()
        for row in rows:
            mode = str(row["mode"] or "off")
            if mode in result:
                result[mode] = int(row["count"] or 0)
        return result

    def health(self) -> dict[str, Any]:
        try:
            stats = self.stats()
            return {"health": "ok", "detail": f"{stats['files']} 份资料已建立本地索引"}
        except Exception as exc:
            return {"health": "error", "detail": "文件工作区组件暂不可用"}


file_workspace = FileWorkspace()
