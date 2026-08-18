"""Versioned database migrations for cross-cutting application infrastructure."""
from __future__ import annotations

from collections.abc import Iterable

from models import get_db


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "relational honesty audits and recoverable chat requests",
        """
        CREATE TABLE IF NOT EXISTS chat_requests (
            client_request_id   TEXT PRIMARY KEY,
            session_id          TEXT NOT NULL,
            provider            TEXT DEFAULT '',
            model               TEXT DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'processing',
            trace_id            TEXT DEFAULT '',
            user_message_id     INTEGER,
            assistant_message_id INTEGER,
            cancel_requested    INTEGER NOT NULL DEFAULT 0,
            error_code          TEXT DEFAULT '',
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at        TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (user_message_id) REFERENCES messages(id),
            FOREIGN KEY (assistant_message_id) REFERENCES messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_requests_session
            ON chat_requests(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chat_requests_status
            ON chat_requests(status, updated_at);

        CREATE TABLE IF NOT EXISTS relational_honesty_audits (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            client_request_id   TEXT DEFAULT '',
            session_id          TEXT DEFAULT '',
            assistant_message_id INTEGER,
            draft_sha256        TEXT NOT NULL,
            categories          TEXT NOT NULL DEFAULT '[]',
            action              TEXT NOT NULL,
            passed              INTEGER NOT NULL DEFAULT 0,
            rule_version        TEXT NOT NULL,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (assistant_message_id) REFERENCES messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_honesty_audits_created
            ON relational_honesty_audits(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_honesty_audits_request
            ON relational_honesty_audits(client_request_id);
        """,
    ),
    (
        2,
        "persistent relational honesty settings",
        """
        CREATE TABLE IF NOT EXISTS relational_honesty_settings (
            id            INTEGER PRIMARY KEY CHECK(id = 1),
            settings_json TEXT NOT NULL,
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
)


def run_schema_migrations(
    migrations: Iterable[tuple[int, str, str]] = MIGRATIONS,
) -> list[int]:
    """Apply each migration exactly once and return newly applied versions."""
    applied_now: list[int] = []
    with get_db() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        applied = {
            int(row["version"])
            for row in db.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, name, sql in sorted(migrations, key=lambda item: item[0]):
            if int(version) in applied:
                continue
            db.executescript(sql)
            db.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (int(version), str(name)),
            )
            applied_now.append(int(version))
    return applied_now


def migration_status() -> dict:
    with get_db() as db:
        exists = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='schema_migrations'"""
        ).fetchone()
        if not exists:
            return {"latest": 0, "applied": []}
        rows = db.execute(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
    items = [dict(row) for row in rows]
    return {"latest": max((int(item["version"]) for item in items), default=0), "applied": items}
