"""Local, provider-separated storage for exposed reasoning text.

Only provider fields that are explicitly returned as displayable reasoning
are stored here.  The content is never appended to normal chat history and is
therefore never replayed when the user switches from DeepSeek to Claude/GPT.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from models import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ThinkingVault:
    MAX_CHARS = 2_000_000

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS reasoning_traces (
                    message_id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    char_count INTEGER NOT NULL DEFAULT 0,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_reasoning_session
                    ON reasoning_traces(session_id, message_id DESC);
                """
            )

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # A deliberately conservative display estimate for mixed Chinese and
        # English.  Actual billed reasoning tokens still come from the API.
        return max(0, math.ceil(len(text or "") / 2.4))

    def save(
        self,
        *,
        message_id: int,
        session_id: str,
        provider: str,
        model: str,
        content: str,
    ) -> bool:
        self.ensure_schema()
        text = str(content or "")[: self.MAX_CHARS]
        if not text.strip():
            return False
        with get_db() as db:
            db.execute(
                """INSERT INTO reasoning_traces
                   (message_id, session_id, provider, model, content,
                    char_count, token_estimate, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     provider=excluded.provider, model=excluded.model,
                     content=excluded.content, char_count=excluded.char_count,
                     token_estimate=excluded.token_estimate,
                     created_at=excluded.created_at""",
                (
                    int(message_id), session_id, provider, model, text,
                    len(text), self.estimate_tokens(text), _now(),
                ),
            )
        return True

    def get(self, message_id: int) -> dict[str, Any] | None:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM reasoning_traces WHERE message_id=?",
                (int(message_id),),
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, int]:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                """SELECT COUNT(*) AS traces,
                          COALESCE(SUM(char_count), 0) AS chars
                   FROM reasoning_traces"""
            ).fetchone()
        return {"traces": int(row["traces"] or 0), "chars": int(row["chars"] or 0)}

    def health(self) -> dict[str, str]:
        try:
            stats = self.stats()
            return {"health": "ok", "detail": f"{stats['traces']} 条可见思考已分层保存"}
        except Exception as exc:
            return {"health": "error", "detail": "思考保险库组件暂不可用"}


thinking_vault = ThinkingVault()
