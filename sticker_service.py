"""v5.7 本地表情包库与主动发送骨架。"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from attachment_service import _validate_image
from config import _env_int
from runtime_paths import STICKER_DATA_DIR, STICKER_DIR
from typing import Any


MANIFEST_PATH = STICKER_DATA_DIR / "manifest.json"
STICKER_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_STICKER_BYTES = _env_int(
    "STICKER_MAX_BYTES", 8 * 1024 * 1024,
    min_value=1024, max_value=64 * 1024 * 1024,
)
MARKER_RE = re.compile(r"\[\[sticker:([a-zA-Z0-9_-]{1,80})\]\]")
SERIOUS_MARKERS = (
    "我很认真", "认真说", "去世", "死亡", "自杀", "伤害自己", "报警", "急诊",
    "确诊", "住院", "崩溃", "遗书", "危险", "严重", "法律风险",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StickerService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session_turns: dict[str, int] = {}
        self._last_sent_turn: dict[str, int] = {}
        if not MANIFEST_PATH.exists():
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                data = json.loads(MANIFEST_PATH.read_text("utf-8"))
                return data if isinstance(data, list) else []
            except Exception:
                return []

    def _write(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            MANIFEST_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        items = self._read()
        result = []
        for item in items:
            if enabled_only and not item.get("enabled", True):
                continue
            file_path = STICKER_DIR / item.get("file", "")
            if not file_path.is_file():
                continue
            clean = dict(item)
            clean["url"] = f"/api/stickers/{item['id']}/content"
            result.append(clean)
        return result

    def get(self, sticker_id: str) -> dict[str, Any] | None:
        for item in self.list():
            if item.get("id") == sticker_id:
                return item
        return None

    def file_path(self, sticker_id: str) -> Path | None:
        item = self.get(sticker_id)
        if not item:
            return None
        path = STICKER_DIR / str(item.get("file") or "")
        try:
            path.resolve().relative_to(STICKER_DIR.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    def import_bytes(
        self,
        filename: str,
        raw: bytes,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        display_name = Path(filename or "sticker").name[:160]
        suffix = Path(display_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise ValueError("表情包只支持 PNG、JPG、GIF、WebP")
        if not raw:
            raise ValueError("表情包文件为空")
        if len(raw) > MAX_STICKER_BYTES:
            raise ValueError(f"表情包超过 {MAX_STICKER_BYTES // 1024 // 1024} MB 上限")
        _validate_image(raw, suffix)
        sticker_id = f"stk_{uuid.uuid4().hex[:16]}"
        stored_name = f"{sticker_id}{suffix}"
        target = STICKER_DIR / stored_name
        temp = STICKER_DIR / f".{stored_name}.{uuid.uuid4().hex}.tmp"
        temp.write_bytes(raw)
        entry = {
            "id": sticker_id,
            "file": stored_name,
            "name": (name or Path(display_name).stem or "表情包")[:80],
            "tags": [str(tag).strip()[:30] for tag in (tags or []) if str(tag).strip()][:12],
            "description": str(description or "")[:240],
            "enabled": True,
            "created_at": _now(),
        }
        items = self._read()
        items.append(entry)
        try:
            temp.replace(target)
            self._write(items)
        except Exception:
            temp.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        entry["url"] = f"/api/stickers/{sticker_id}/content"
        return entry

    async def import_upload(self, upload: Any, **kwargs: Any) -> dict[str, Any]:
        raw = await upload.read()
        return self.import_bytes(upload.filename or "sticker.png", raw, **kwargs)

    def update(self, sticker_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        items = self._read()
        updated = None
        for item in items:
            if item.get("id") != sticker_id:
                continue
            if "name" in patch:
                item["name"] = str(patch["name"] or "表情包")[:80]
            if "description" in patch:
                item["description"] = str(patch["description"] or "")[:240]
            if "tags" in patch and isinstance(patch["tags"], list):
                item["tags"] = [str(x).strip()[:30] for x in patch["tags"] if str(x).strip()][:12]
            if "enabled" in patch:
                item["enabled"] = bool(patch["enabled"])
            updated = dict(item)
            break
        if updated is None:
            return None
        self._write(items)
        updated["url"] = f"/api/stickers/{sticker_id}/content"
        return updated

    def delete(self, sticker_id: str) -> bool:
        items = self._read()
        kept: list[dict[str, Any]] = []
        removed = None
        for item in items:
            if item.get("id") == sticker_id:
                removed = item
            else:
                kept.append(item)
        if not removed:
            return False
        self._write(kept)
        try:
            (STICKER_DIR / removed.get("file", "")).unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def catalog_prompt(self, mode: str) -> str:
        mode = mode if mode in {"restrained", "active"} else "off"
        items = self.list(enabled_only=True)[:60]
        if mode == "off" or not items:
            return ""
        lines = []
        for item in items:
            tags = "、".join(item.get("tags") or [])
            desc = item.get("description") or tags or ""
            lines.append(f"- {item['id']}：{item.get('name', '表情包')}；{desc}")
        cadence = "非常克制，通常至少间隔四轮" if mode == "restrained" else "自然但不要刷屏，通常至少间隔两轮"
        return (
            "【本地表情包库】\n"
            f"你可以偶尔发送一张表情包，频率要求：{cadence}。一次最多一张。"
            "需要发送时，只在回答末尾追加 [[sticker:表情包ID]]。"
            "认真、悲伤、危险或用户说‘我很认真’时不要发搞笑表情包。不要解释该标记。\n"
            + "\n".join(lines)
        )

    def note_user_turn(self, session_id: str) -> int:
        turn = self._session_turns.get(session_id, 0) + 1
        self._session_turns[session_id] = turn
        return turn

    def can_auto_send(self, session_id: str, mode: str, user_message: str) -> bool:
        if mode not in {"restrained", "active"}:
            return False
        if any(marker in (user_message or "") for marker in SERIOUS_MARKERS):
            return False
        current = self._session_turns.get(session_id, 0)
        last = self._last_sent_turn.get(session_id, -999)
        cooldown = 4 if mode == "restrained" else 2
        return current - last >= cooldown

    def extract_valid_markers(
        self,
        text: str,
        *,
        session_id: str,
        mode: str,
        user_message: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        requested = MARKER_RE.findall(text or "")
        cleaned = MARKER_RE.sub("", text or "").rstrip()
        if not requested or not self.can_auto_send(session_id, mode, user_message):
            return cleaned, []
        item = self.get(requested[0])
        if not item or not item.get("enabled", True):
            return cleaned, []
        self._last_sent_turn[session_id] = self._session_turns.get(session_id, 0)
        return cleaned, [item]

    @staticmethod
    def message_metadata(item: dict[str, Any], *, display_text: str = "") -> dict[str, Any]:
        return {
            "message_type": "mixed" if display_text else "sticker",
            "display_text": display_text,
            "sticker": {
                "id": item.get("id"),
                "name": item.get("name"),
                "url": item.get("url"),
                "tags": item.get("tags", []),
            },
        }


sticker_service = StickerService()
