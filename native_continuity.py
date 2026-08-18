"""Local provider-native turn vault for Claude, GPT and DeepSeek.

Visible text remains the portable source of truth.  Opaque/native content
blocks are stored separately and replayed only to the exact provider + model
that created them, so switching models never leaks or misinterprets state.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

from models import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NativeContinuity:
    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS native_turns (
                    message_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_family TEXT NOT NULL,
                    response_id TEXT DEFAULT '',
                    envelope_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_native_turn_session
                    ON native_turns(session_id, provider, model, message_id);
                """
            )

    @staticmethod
    def _provider_family(provider: str, model: str = "") -> str:
        value = str(provider or "").strip().lower()
        model_value = str(model or "").strip().lower()
        if value == "openrouter_claude" or (
            value.startswith("openrouter") and "claude" in model_value
        ):
            return "anthropic"
        return value

    @classmethod
    def _valid_envelope(cls, provider: str, envelope: Any, model: str = "") -> bool:
        if not isinstance(envelope, dict):
            return False
        family = cls._provider_family(provider, model)
        if family == "openai":
            return envelope.get("api_family") == "responses" and isinstance(envelope.get("output"), list)
        if family == "anthropic":
            return envelope.get("api_family") == "messages" and isinstance(envelope.get("content"), list)
        if family == "deepseek":
            return (
                envelope.get("api_family") == "deepseek_chat_completions"
                and isinstance(envelope.get("messages"), list)
            )
        return False

    def align_visible_text(self, envelope: dict[str, Any] | None, text: str) -> dict[str, Any] | None:
        """Return an untouched copy of a provider-native response envelope.

        The visible archive may omit UI-only sticker markers, but stateless
        reasoning continuity requires every native output item to be replayed
        exactly as returned.  In particular, never edit text next to encrypted
        OpenAI reasoning items or signed Anthropic thinking blocks.

        ``text`` remains in this compatibility signature because older callers
        already invoke this method after preparing the visible archive.
        """
        if not isinstance(envelope, dict):
            return envelope
        return copy.deepcopy(envelope)

    def save_turn(self, *, message_id: int, session_id: str, provider: str,
                  model: str, envelope: dict[str, Any] | None) -> bool:
        if not message_id or not self._valid_envelope(provider, envelope, model):
            return False
        self.ensure_schema()
        payload = copy.deepcopy(envelope)
        api_family = str(payload.get("api_family") or "")
        response_id = str(payload.get("response_id") or "")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        with get_db() as db:
            db.execute(
                """INSERT INTO native_turns
                   (message_id, session_id, provider, model, api_family,
                    response_id, envelope_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     provider=excluded.provider, model=excluded.model,
                     api_family=excluded.api_family, response_id=excluded.response_id,
                     envelope_json=excluded.envelope_json""",
                (message_id, session_id, provider, model, api_family,
                 response_id, raw, _now()),
            )
        return True

    def _map_for(self, message_ids: list[int], provider: str, model: str) -> dict[int, dict[str, Any]]:
        ids = [int(value) for value in message_ids if value]
        if not ids or self._provider_family(provider, model) not in {
            "openai", "anthropic", "deepseek",
        }:
            return {}
        self.ensure_schema()
        marks = ",".join("?" for _ in ids)
        with get_db() as db:
            rows = db.execute(
                f"""SELECT message_id, envelope_json FROM native_turns
                    WHERE provider=? AND model=? AND message_id IN ({marks})""",
                [provider, model, *ids],
            ).fetchall()
        result: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(row["envelope_json"])
                if self._valid_envelope(provider, value, model):
                    result[int(row["message_id"])] = value
            except Exception:
                continue
        return result

    def attach_to_history(self, history: list[dict[str, Any]], provider: str,
                          model: str) -> list[dict[str, Any]]:
        """Return copies decorated with a native envelope for matching turns."""
        envelopes = self._map_for(
            [
                int(item.get("id") or 0)
                for item in history
                if item.get("role") == "assistant"
                and not item.get("_context_truncated")
            ],
            provider, model,
        )
        result: list[dict[str, Any]] = []
        for raw in history:
            item = dict(raw)
            envelope = envelopes.get(int(item.get("id") or 0))
            if envelope:
                item["native_envelope"] = envelope
            result.append(item)
        return result

    def stats(self) -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            rows = db.execute(
                """SELECT provider, COUNT(*) AS count,
                          COALESCE(SUM(LENGTH(envelope_json)), 0) AS bytes
                   FROM native_turns GROUP BY provider"""
            ).fetchall()
        providers = {row["provider"]: {"turns": int(row["count"]),
                                       "bytes": int(row["bytes"])} for row in rows}
        return {"turns": sum(x["turns"] for x in providers.values()),
                "bytes": sum(x["bytes"] for x in providers.values()),
                "providers": providers}

    def health(self) -> dict[str, Any]:
        try:
            stats = self.stats()
            return {"health": "ok", "detail": f"{stats['turns']} 个原生回合已本地保真"}
        except Exception as exc:
            return {"health": "error", "detail": "原生上下文组件暂不可用"}


native_continuity = NativeContinuity()
