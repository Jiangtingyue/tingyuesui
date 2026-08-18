"""大西瓜 v7.6 共处感知、晨间事件与同窗口主动续话。

这一层只保存内容无关的输入节奏，从不接收未发送的草稿文字。明确要求
安静时只关闭当前窗口的主动发言；检测到使用者正在输入时，待发消息会顺延
而不是抢话。模型可依据当前窗口的真实消息、有来源个人记忆或本机当前状态
开口；未显式返回引用时，后端只会自动绑定确实存在的依据，绝不编造。
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from config import CO_PRESENCE_CONFIG
from models import get_db


DEFAULT_SETTINGS = {
    "enabled": True,
    "rhythm_enabled": True,
    "natural_continuation_enabled": True,
    "independent_initiative_enabled": True,
}

ALLOWED_EVENTS = {
    "speaking", "paused", "cleared", "submitted", "visible", "hidden",
    "session_switched",
}
ALLOWED_INPUT_METHODS = {"keyboard", "voice", "paste", "unknown"}

_QUIET_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:请|麻烦)?(?:先)?(?:不要|别|不许)(?:再)?(?:主动)?(?:给我)?(?:发消息|联系我|找我|跟我说话|说话|回复|打扰我)",
    r"让我(?:一个人)?(?:安静|静静|待一会儿|待会儿|待着)",
    r"我想(?:一个人)?(?:安静|静静|待一会儿|待会儿)",
    r"现在(?:不要|别)(?:说话|回复|联系我|发消息|打扰我)",
    r"(?:不用|不要)回复(?:我)?",
    r"(?:^|[，。！？!?,\s])闭嘴(?:[，。！？!?,\s]|$)",
))
_RELEASE_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:可以|你可以|现在可以)(?:继续)?(?:说话|回复|联系我|发消息)了",
    r"(?:不用|不要|别)(?:再)?安静了",
    r"(?:回来吧|理理我|理我一下|陪陪我|陪我说话|跟我说话|继续说)",
    r"怎么不说话",
))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value or ""))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _json(value: Any, fallback: dict | None = None) -> dict:
    if isinstance(value, dict):
        return deepcopy(value)
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else deepcopy(fallback or {})
    except Exception:
        return deepcopy(fallback or {})


def _bounded_int(value: Any, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value or 0)))
    except (TypeError, ValueError):
        return low


def _strict_bool(value: Any, *, field: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{field} 必须是布尔值")


class CoPresence:
    def __init__(self) -> None:
        self._settings_cache: dict[str, Any] | None = None

    @staticmethod
    def _ensure_column(db, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def ensure_schema(self) -> None:
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS co_presence_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    settings_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS co_presence_policy (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    quiet_requested INTEGER NOT NULL DEFAULT 0,
                    quiet_reason TEXT DEFAULT '',
                    quiet_since TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS co_presence_quiet (
                    session_id TEXT PRIMARY KEY,
                    quiet_requested INTEGER NOT NULL DEFAULT 0,
                    quiet_reason TEXT DEFAULT '',
                    quiet_since TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS co_presence_state (
                    session_id TEXT PRIMARY KEY,
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    last_event TEXT DEFAULT '',
                    last_signal_at TEXT DEFAULT '',
                    speaking_until TEXT DEFAULT '',
                    draft_open INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS co_presence_rhythm (
                    session_id TEXT PRIMARY KEY,
                    started_at TEXT DEFAULT '',
                    last_ping_at TEXT DEFAULT '',
                    last_event TEXT DEFAULT '',
                    active_ms INTEGER NOT NULL DEFAULT 0,
                    revision_count INTEGER NOT NULL DEFAULT 0,
                    pause_count INTEGER NOT NULL DEFAULT 0,
                    clear_count INTEGER NOT NULL DEFAULT 0,
                    paste_count INTEGER NOT NULL DEFAULT 0,
                    input_event_count INTEGER NOT NULL DEFAULT 0,
                    deletion_count INTEGER NOT NULL DEFAULT 0,
                    burst_count INTEGER NOT NULL DEFAULT 0,
                    longest_pause_ms INTEGER NOT NULL DEFAULT 0,
                    input_method TEXT DEFAULT 'unknown',
                    draft_open INTEGER NOT NULL DEFAULT 0,
                    orphan_state TEXT DEFAULT '',
                    orphaned_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS co_presence_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    cue_json TEXT NOT NULL DEFAULT '{}',
                    anchor_message_id INTEGER NOT NULL DEFAULT 0,
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    due_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT DEFAULT '',
                    delivered_message_id INTEGER,
                    decision_json TEXT NOT NULL DEFAULT '{}',
                    grounding_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS co_presence_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT DEFAULT '',
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'info',
                    detail TEXT NOT NULL DEFAULT '',
                    event_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proactive_call_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    trigger_kind TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    decision_action TEXT NOT NULL DEFAULT '',
                    outcome_action TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read INTEGER NOT NULL DEFAULT 0,
                    cache_creation INTEGER NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0,
                    cost_source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_proactive_call_stats_created
                    ON proactive_call_stats(created_at, id);
                CREATE INDEX IF NOT EXISTS idx_proactive_call_stats_kind
                    ON proactive_call_stats(trigger_kind, created_at);
                CREATE INDEX IF NOT EXISTS idx_co_presence_due
                    ON co_presence_events(state, due_at, id);
                CREATE INDEX IF NOT EXISTS idx_co_presence_session
                    ON co_presence_events(session_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_co_presence_log_session
                    ON co_presence_log(session_id, id DESC);
                """
            )
            # Safe in-place migration from v7.2 databases.
            self._ensure_column(
                db, "co_presence_events", "decision_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            self._ensure_column(
                db, "co_presence_events", "grounding_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            db.execute(
                """INSERT OR IGNORE INTO co_presence_policy(
                     id, quiet_requested, quiet_reason, quiet_since, updated_at
                   ) VALUES(1, 0, '', '', ?)""",
                (_iso(),),
            )

    def settings(self, refresh: bool = False) -> dict[str, Any]:
        self.ensure_schema()
        if self._settings_cache is not None and not refresh:
            return deepcopy(self._settings_cache)
        defaults = {
            **DEFAULT_SETTINGS,
            "enabled": bool(CO_PRESENCE_CONFIG.get("enabled", True)),
            "rhythm_enabled": bool(CO_PRESENCE_CONFIG.get("rhythm_enabled", True)),
            "natural_continuation_enabled": bool(
                CO_PRESENCE_CONFIG.get("natural_continuation_enabled", True)
            ),
            "independent_initiative_enabled": bool(
                CO_PRESENCE_CONFIG.get("independent_initiative_enabled", True)
            ),
        }
        with get_db() as db:
            row = db.execute(
                "SELECT settings_json FROM co_presence_settings WHERE id=1"
            ).fetchone()
        current = _json(row["settings_json"], {}) if row else {}
        result = {
            **defaults,
            **{
                key: bool(current[key])
                for key in DEFAULT_SETTINGS
                if key in current
            },
        }
        self._settings_cache = result
        if not row:
            self._save_settings(result)
        return deepcopy(result)

    def _save_settings(self, settings: dict[str, Any]) -> None:
        self.ensure_schema()
        payload = {
            key: bool(settings.get(key, DEFAULT_SETTINGS[key]))
            for key in DEFAULT_SETTINGS
        }
        with get_db() as db:
            db.execute(
                """INSERT INTO co_presence_settings(id, settings_json, updated_at)
                   VALUES(1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     settings_json=excluded.settings_json,
                     updated_at=excluded.updated_at""",
                (json.dumps(payload, ensure_ascii=False), _iso()),
            )
        self._settings_cache = payload

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        settings = self.settings()
        for key in DEFAULT_SETTINGS:
            if key in changes:
                settings[key] = _strict_bool(changes[key], field=key)
        self._save_settings(settings)
        if not settings["enabled"] or not settings["natural_continuation_enabled"]:
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_events
                       SET state='superseded', outcome='共处续话已关闭', updated_at=?
                       WHERE state IN ('pending','processing')""",
                    (_iso(),),
                )
        elif not settings.get("independent_initiative_enabled", True):
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_events
                       SET state='superseded', outcome='独立主动联系已关闭', updated_at=?
                       WHERE kind IN ('independent_initiative','manual_initiative_test')
                         AND state IN ('pending','processing')""",
                    (_iso(),),
                )
        return self.public_state()

    @staticmethod
    def sanitize_rhythm(raw: Any) -> dict[str, Any]:
        """Hard allowlist: draft text and length cannot survive this boundary."""
        if not isinstance(raw, dict):
            return {}
        method = str(raw.get("input_method") or "unknown").lower()
        if method not in ALLOWED_INPUT_METHODS:
            method = "unknown"
        return {
            "active_ms": _bounded_int(raw.get("active_ms"), 0, 30 * 60 * 1000),
            "revision_count": _bounded_int(raw.get("revision_count"), 0, 500),
            "pause_count": _bounded_int(raw.get("pause_count"), 0, 500),
            "clear_count": _bounded_int(raw.get("clear_count"), 0, 100),
            "paste_count": _bounded_int(raw.get("paste_count"), 0, 100),
            "input_event_count": _bounded_int(raw.get("input_event_count"), 0, 5000),
            "deletion_count": _bounded_int(raw.get("deletion_count"), 0, 5000),
            "burst_count": _bounded_int(raw.get("burst_count"), 0, 500),
            "longest_pause_ms": _bounded_int(
                raw.get("longest_pause_ms"), 0, 30 * 60 * 1000
            ),
            "input_method": method,
            "draft_active": _strict_bool(
                raw.get("draft_active", False), field="draft_active"
            ),
        }

    def submitted_prompt(self, raw: Any) -> str:
        settings = self.settings()
        rhythm = self.sanitize_rhythm(raw)
        if not settings["enabled"] or not settings["rhythm_enabled"] or not rhythm:
            return ""
        if rhythm["input_method"] == "voice":
            return ""
        cues: list[str] = []
        if rhythm["revision_count"] >= 5:
            cues.append("这句话在说出口前有较多删改，像是反复斟酌过")
        elif rhythm["revision_count"]:
            cues.append("这句话在说出口前有过修改")
        if rhythm["pause_count"] >= 3 or rhythm["longest_pause_ms"] >= 12_000:
            cues.append("表达中有明显停顿")
        elif rhythm["pause_count"] or rhythm["active_ms"] >= 10_000:
            cues.append("表达不是立刻脱口而出")
        if rhythm["burst_count"] >= 3 and rhythm["pause_count"] == 0:
            cues.append("表达节奏比较连贯直接")
        if not cues:
            cues.append("这次表达节奏很轻，按普通说话自然理解即可")
        return "\n".join((
            "<co_presence_cue>",
            "以下只是内容无关的表达节奏，是柔性线索，不是事实或情绪标签。",
            "；".join(cues) + "。",
            "不要提输入框、打字、删改、检测、系统或次数，也不要仅凭节奏臆测内容。",
            "</co_presence_cue>",
        ))

    def session_brain(self, session_id: str) -> tuple[str, str]:
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                """SELECT provider, model FROM messages
                   WHERE session_id=? AND provider<>''
                   ORDER BY id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            session = db.execute(
                "SELECT provider, model FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
        provider = str(
            (row["provider"] if row else "")
            or (session["provider"] if session else "")
            or ""
        )
        model = str(
            (row["model"] if row else "")
            or (session["model"] if session else "")
            or ""
        )
        if provider not in config.PROVIDERS:
            provider = config.ACTIVE_PROVIDER
        if not model:
            model = config.get_active_model(provider)
        return provider, model

    @staticmethod
    def _latest_message_id(db, session_id: str) -> int:
        row = db.execute(
            "SELECT COALESCE(MAX(id), 0) AS value FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return int(row["value"] or 0)

    def _log(
        self,
        session_id: str,
        kind: str,
        detail: str,
        *,
        status: str = "info",
        event_id: int | None = None,
        db=None,
    ) -> None:
        def insert(handle) -> None:
            handle.execute(
                """INSERT INTO co_presence_log(
                     session_id, kind, status, detail, event_id, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    str(session_id or ""), str(kind or "event")[:64],
                    str(status or "info")[:24], str(detail or "")[:240],
                    event_id, _iso(),
                ),
            )
        if db is not None:
            insert(db)
        else:
            with get_db() as handle:
                insert(handle)

    def quiet_state(self, session_id: str = "") -> dict[str, Any]:
        self.ensure_schema()
        with get_db() as db:
            row = (
                db.execute(
                    """SELECT quiet_requested, quiet_reason, quiet_since, updated_at
                       FROM co_presence_quiet WHERE session_id=?""",
                    (session_id,),
                ).fetchone()
                if session_id else None
            )
        return {
            "requested": bool(row and row["quiet_requested"]),
            "reason": str(row["quiet_reason"] or "") if row else "",
            "since": str(row["quiet_since"] or "") if row else "",
            "updated_at": str(row["updated_at"] or "") if row else "",
        }

    @staticmethod
    def _quiet_intent(user_text: str) -> str:
        text = re.sub(r"\s+", "", str(user_text or ""))
        if not text:
            return ""

        # Negated or quoted/meta mentions only suppress the quiet phrase they
        # actually contain.  Do not let an earlier “我没有说不要回复我” mask a
        # later real instruction such as “但是现在别回复我”.  When multiple
        # explicit instructions exist, the latest one in the utterance wins.
        negated = re.compile(
            r"(?:没有|沒|没|并没有|不是|并不是)(?:说|叫|让|要求|希望)?[^，,。！？!?；;]{0,10}?"
            r"(?:不要|别|不许)(?:再)?(?:主动)?(?:给我)?(?:发消息|联系我|找我|跟我说话|说话|回复|打扰我)"
        )
        quoted = re.compile(
            r"(?:这句|这句话|这几个字|原话|引用|举例)[^，,。！？!?；;]{0,12}?"
            r"(?:不要|别|不许)[^，,。！？!?；;]{0,14}?(?:回复|发消息|联系我|说话)"
        )
        ignored_spans = [m.span() for m in negated.finditer(text)]
        ignored_spans.extend(m.span() for m in quoted.finditer(text))

        def _ignored(start: int, end: int) -> bool:
            return any(start < right and end > left for left, right in ignored_spans)

        candidates: list[tuple[int, int, str]] = []
        for pattern in _QUIET_PATTERNS:
            for match in pattern.finditer(text):
                if not _ignored(match.start(), match.end()):
                    candidates.append((match.start(), match.end(), "quiet"))
        for pattern in _RELEASE_PATTERNS:
            for match in pattern.finditer(text):
                candidates.append((match.start(), match.end(), "release"))
        if not candidates:
            return ""
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _set_quiet(self, session_id: str, requested: bool, reason: str = "") -> None:
        now = _iso()
        with get_db() as db:
            db.execute(
                """INSERT INTO co_presence_quiet(
                     session_id, quiet_requested, quiet_reason, quiet_since, updated_at
                   ) VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     quiet_requested=excluded.quiet_requested,
                     quiet_reason=excluded.quiet_reason,
                     quiet_since=excluded.quiet_since,
                     updated_at=excluded.updated_at""",
                (
                    session_id,
                    int(requested),
                    reason[:160] if requested else "",
                    now if requested else "",
                    now,
                ),
            )
            if requested:
                affected = db.execute(
                    """SELECT * FROM co_presence_events
                       WHERE session_id=? AND state IN ('pending','processing')""",
                    (session_id,),
                ).fetchall()
                db.execute(
                    """UPDATE co_presence_events
                       SET state='superseded', outcome='使用者明确要求安静', updated_at=?
                       WHERE session_id=? AND state IN ('pending','processing')""",
                    (now, session_id),
                )
                for event in affected:
                    self._sync_morning_outcome(
                        self._event_dict(event),
                        "superseded",
                        "使用者明确要求安静",
                        db=db,
                    )
            self._log(
                session_id,
                "quiet_on" if requested else "quiet_off",
                "已尊重明确的安静请求" if requested else "使用者已允许再次自然开口",
                status="held" if requested else "ready",
                db=db,
            )

    def _cancel(self, session_id: str, *, reason: str) -> int:
        self.ensure_schema()
        with get_db() as db:
            affected = db.execute(
                """SELECT * FROM co_presence_events
                   WHERE session_id=? AND state IN ('pending','processing')""",
                (session_id,),
            ).fetchall()
            cursor = db.execute(
                """UPDATE co_presence_events
                   SET state='superseded', updated_at=?, outcome=?
                   WHERE session_id=? AND state IN ('pending','processing')""",
                (_iso(), reason, session_id),
            )
            for event in affected:
                self._sync_morning_outcome(
                    self._event_dict(event),
                    "superseded",
                    reason,
                    db=db,
                )
            return int(cursor.rowcount or 0)

    def note_user_message(
        self,
        session_id: str,
        *,
        user_text: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        """A real user turn supersedes stale work and may set/release quiet."""
        self.ensure_schema()
        now = _iso()
        self._cancel(session_id, reason="使用者已经继续说话，旧判断作废")
        intent = self._quiet_intent(user_text)
        if intent == "quiet":
            self._set_quiet(session_id, True, "使用者在对话中明确要求安静")
        elif intent == "release":
            self._set_quiet(session_id, False)
        with get_db() as db:
            db.execute(
                """INSERT INTO co_presence_state(
                     session_id, provider, model, last_event, last_signal_at,
                     speaking_until, draft_open, updated_at
                   ) VALUES(?, ?, ?, 'submitted', ?, ?, 0, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     provider=excluded.provider, model=excluded.model,
                     last_event='submitted', last_signal_at=excluded.last_signal_at,
                     speaking_until=excluded.speaking_until, draft_open=0,
                     updated_at=excluded.updated_at""",
                (session_id, provider, model, now, now, now),
            )
            db.execute(
                """INSERT INTO co_presence_rhythm(
                     session_id, last_ping_at, last_event, draft_open,
                     orphan_state, orphaned_at, updated_at
                   ) VALUES(?, ?, 'submitted', 0, '', '', ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     last_ping_at=excluded.last_ping_at, last_event='submitted',
                     draft_open=0, orphan_state='', orphaned_at='',
                     updated_at=excluded.updated_at""",
                (session_id, now, now),
            )

    def _queue_event(
        self,
        session_id: str,
        *,
        kind: str,
        cue: dict[str, Any],
        delay_seconds: int,
        anchor_message_id: int | None = None,
        provider: str = "",
        model: str = "",
    ) -> dict[str, Any] | None:
        settings = self.settings()
        if (
            not settings["enabled"]
            or not settings["natural_continuation_enabled"]
            or self.quiet_state(session_id)["requested"]
        ):
            return None
        if not provider or not model:
            provider, model = self.session_brain(session_id)
        now = _now()
        with get_db() as db:
            anchor = (
                int(anchor_message_id)
                if anchor_message_id is not None
                else self._latest_message_id(db, session_id)
            )
            existing = db.execute(
                """SELECT id FROM co_presence_events
                   WHERE session_id=? AND kind=? AND state='pending'
                   ORDER BY id DESC LIMIT 1""",
                (session_id, kind),
            ).fetchone()
            payload = json.dumps(cue, ensure_ascii=False, separators=(",", ":"))
            due = _iso(now + timedelta(seconds=max(1, int(delay_seconds))))
            if existing:
                event_id = int(existing["id"])
                db.execute(
                    """UPDATE co_presence_events
                       SET cue_json=?, anchor_message_id=?, provider=?, model=?,
                           due_at=?, attempts=0, outcome='新线索已更新', updated_at=?
                       WHERE id=?""",
                    (payload, anchor, provider, model, due, _iso(now), event_id),
                )
            else:
                cursor = db.execute(
                    """INSERT INTO co_presence_events(
                         session_id, kind, state, cue_json, anchor_message_id,
                         provider, model, due_at, attempts, created_at, updated_at
                       ) VALUES(?, ?, 'pending', ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        session_id, kind, payload, anchor, provider, model, due,
                        _iso(now), _iso(now),
                    ),
                )
                event_id = int(cursor.lastrowid)
            row = db.execute(
                "SELECT * FROM co_presence_events WHERE id=?", (event_id,)
            ).fetchone()
            self._log(
                session_id, "queued", f"已把 {kind} 交给原会话模型判断",
                status="pending", event_id=event_id, db=db,
            )
        return self._event_dict(row) if row else None

    def observe(
        self,
        session_id: str,
        event: str,
        metrics: Any = None,
        *,
        provider: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Record content-free rhythm; it never blocks a model decision."""
        self.ensure_schema()
        settings = self.settings()
        event = str(event or "").lower()
        if event not in ALLOWED_EVENTS:
            raise ValueError("不支持的共处事件")
        rhythm = self.sanitize_rhythm(metrics)
        if not settings["enabled"] or not settings["rhythm_enabled"]:
            return self.public_state(session_id)
        provider, model = self.session_brain(session_id)
        now = _now()
        draft_active = bool(rhythm.get("draft_active"))
        stale = int(CO_PRESENCE_CONFIG.get("speaking_stale_seconds", 18))
        speaking_until = now + timedelta(seconds=stale) if draft_active else now
        started_at = _iso(now - timedelta(milliseconds=rhythm.get("active_ms", 0)))
        with get_db() as db:
            db.execute(
                """INSERT INTO co_presence_state(
                     session_id, provider, model, last_event, last_signal_at,
                     speaking_until, draft_open, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     provider=excluded.provider, model=excluded.model,
                     last_event=excluded.last_event,
                     last_signal_at=excluded.last_signal_at,
                     speaking_until=excluded.speaking_until,
                     draft_open=excluded.draft_open,
                     updated_at=excluded.updated_at""",
                (
                    session_id, provider, model, event, _iso(now),
                    _iso(speaking_until), int(draft_active), _iso(now),
                ),
            )
            db.execute(
                """INSERT INTO co_presence_rhythm(
                     session_id, started_at, last_ping_at, last_event,
                     active_ms, revision_count, pause_count, clear_count,
                     paste_count, input_event_count, deletion_count,
                     burst_count, longest_pause_ms, input_method, draft_open,
                     orphan_state, orphaned_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     started_at=excluded.started_at,
                     last_ping_at=excluded.last_ping_at,
                     last_event=excluded.last_event,
                     active_ms=excluded.active_ms,
                     revision_count=excluded.revision_count,
                     pause_count=excluded.pause_count,
                     clear_count=excluded.clear_count,
                     paste_count=excluded.paste_count,
                     input_event_count=excluded.input_event_count,
                     deletion_count=excluded.deletion_count,
                     burst_count=excluded.burst_count,
                     longest_pause_ms=excluded.longest_pause_ms,
                     input_method=excluded.input_method,
                     draft_open=excluded.draft_open,
                     orphan_state=CASE
                       WHEN excluded.draft_open=0 THEN ''
                       WHEN co_presence_rhythm.orphan_state='queued' THEN 'queued'
                       ELSE '' END,
                     orphaned_at=CASE WHEN excluded.draft_open=0 THEN '' ELSE orphaned_at END,
                     updated_at=excluded.updated_at""",
                (
                    session_id, started_at, _iso(now), event,
                    rhythm["active_ms"], rhythm["revision_count"],
                    rhythm["pause_count"], rhythm["clear_count"],
                    rhythm["paste_count"], rhythm["input_event_count"],
                    rhythm["deletion_count"], rhythm["burst_count"],
                    rhythm["longest_pause_ms"], rhythm["input_method"],
                    int(draft_active), _iso(now),
                ),
            )
            if event in {"paused", "cleared", "hidden"}:
                labels = {
                    "paused": "表达节奏出现停顿",
                    "cleared": "未发送内容被清空（内容不可见）",
                    "hidden": "窗口离开时仍有未发送内容",
                }
                self._log(session_id, event, labels[event], db=db)

        # v8.2: pause/clear/hidden are local observations only. They never
        # create a paid model candidate immediately. The scheduler waits for
        # sustained silence, unfinished-expression evidence and cooldown first.
        if event in {"paused", "cleared", "hidden"}:
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_rhythm
                       SET orphan_state='', orphaned_at='', updated_at=?
                       WHERE session_id=?""",
                    (_iso(now), session_id),
                )
        result = self.public_state(session_id)
        result["queued"] = False
        return result

    @staticmethod
    def _public_rhythm(rhythm: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: rhythm.get(key)
            for key in (
                "active_ms", "revision_count", "pause_count", "clear_count",
                "paste_count", "input_event_count", "deletion_count",
                "burst_count", "longest_pause_ms", "input_method",
            )
        }
        result["draft_active"] = bool(
            rhythm.get("draft_active", rhythm.get("draft_open", False))
        )
        return result

    @staticmethod
    def _delay_for_turn(session_id: str, anchor_message_id: int) -> int:
        digest = hashlib.blake2s(
            f"{session_id}:{anchor_message_id}".encode("utf-8"), digest_size=2
        ).digest()
        low = int(CO_PRESENCE_CONFIG.get("afterglow_min_seconds", 24))
        high = max(low + 1, int(CO_PRESENCE_CONFIG.get("afterglow_max_seconds", 52)))
        return low + int.from_bytes(digest, "big") % (high - low + 1)

    @staticmethod
    def _afterglow_score(user_text: str, assistant_text: str) -> tuple[int, list[str]]:
        """Score locally; length contributes but can never trigger by itself."""
        user = str(user_text or "").strip()
        assistant = str(assistant_text or "").strip()
        combined = f"{user}\n{assistant}"
        score = 0
        reasons: list[str] = []
        # Length is deliberately weak. Even very long turns stay below the
        # paid-call threshold unless another concrete signal is present.
        if len(user) >= 80:
            score += 8
            reasons.append("user_length")
        if len(user) >= 240:
            score += 4
        if len(assistant) >= 240:
            score += 8
            reasons.append("assistant_length")
        if len(assistant) >= 800:
            score += 4
        if re.search(r"(?:还没说完|等等(?:我)?|我还有(?:话|一点)|我本来想说|算了(?:吧)?|其实我还)", combined, re.I):
            score += 65
            reasons.append("unfinished_explicit")
        if re.search(
            r"(?:崩溃|大哭|哭了|失眠|医院|害怕|委屈|伤害|偏袒|双标|欺负|威胁)",
            combined, re.I,
        ):
            score += 42
            reasons.append("strong_emotional_cue")
        if re.search(r"(?:别走|留下|想你|抱抱|我爱你|对不起|道歉)", combined, re.I):
            score += 34
            reasons.append("relationship_cue")
        if re.search(r"[？！!?]{2,}|[……]{3,}$", combined):
            score += 24
            reasons.append("punctuation_intensity")
        return min(100, score), reasons

    def note_completed_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        assistant_message_id: int,
        provider: str,
        model: str,
    ) -> dict[str, Any] | None:
        """Use a local funnel before paying for a post-turn model judgment."""
        settings = self.settings()
        if not settings["enabled"] or not settings["natural_continuation_enabled"]:
            return None
        if self.quiet_state(session_id)["requested"]:
            self._log(session_id, "quiet_hold", "明确安静请求仍有效，没有安排主动续话", status="held")
            return None
        score, reasons = self._afterglow_score(user_text, assistant_text)
        threshold = int(CO_PRESENCE_CONFIG.get("local_candidate_score_threshold", 60))
        if score < threshold:
            self._log(
                session_id,
                "afterglow_local_wait",
                f"本地余韵评分 {score}/{threshold}，没有调用模型",
                status="quiet",
            )
            return None
        return self._queue_event(
            session_id,
            kind="conversation_afterglow",
            cue={
                "content_known": True,
                "source": "completed_turn",
                "model_decides": True,
                "local_score": score,
                "local_reasons": reasons[:8],
            },
            delay_seconds=self._delay_for_turn(session_id, assistant_message_id),
            anchor_message_id=assistant_message_id,
            provider=provider,
            model=model,
        )

    @staticmethod
    def _message_metadata(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            value = json.loads(str(raw or "{}"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def schedule_independent_initiative(self) -> dict[str, Any] | None:
        """Create at most one independent-contact candidate per scheduler beat.

        This lane does not depend on a just-completed turn.  It is intentionally
        cheap until a candidate is actually due: SQLite filtering and the local
        living-state decision happen first; only ``claim_due`` causes a model call.
        """
        settings = self.settings()
        if (
            not settings["enabled"]
            or not settings["natural_continuation_enabled"]
            or not settings.get("independent_initiative_enabled", True)
        ):
            return None

        try:
            from living_state import living_state
            living_decision = living_state.heartbeat_decision()
        except Exception:
            living_decision = {
                "action": "observe",
                "reason": "安静了一阵，想自然地重新开口",
                "score": 0,
            }

        action = str(living_decision.get("action") or "observe")
        reason = str(living_decision.get("reason") or "安静了一阵，想自然地重新开口")
        # Explicit disable and daily contact cap are hard holds.  A generic
        # night/immersion "silence" is not permanent: after a long enough idle
        # period the model may still decide naturally, which matters for users
        # whose real companionship hours are late at night.
        hard_hold = action == "disabled" or (
            action == "silence" and "已经主动联系" in reason
        )
        now = _now()
        min_idle = int(CO_PRESENCE_CONFIG.get("independent_min_idle_minutes", 25))
        normal_cooldown = int(CO_PRESENCE_CONFIG.get("independent_cooldown_minutes", 90))
        unanswered_cooldown = int(
            CO_PRESENCE_CONFIG.get("independent_unanswered_cooldown_minutes", 360)
        )

        with get_db() as db:
            rows = db.execute(
                """SELECT s.id AS session_id, s.provider, s.model,
                          m.id AS latest_id, m.role AS latest_role,
                          m.created_at AS latest_created_at,
                          m.metadata AS latest_metadata
                   FROM sessions s
                   JOIN messages m ON m.id=(
                       SELECT MAX(mm.id) FROM messages mm WHERE mm.session_id=s.id
                   )
                   WHERE s.message_count>=2
                   ORDER BY m.id DESC LIMIT 24"""
            ).fetchall()

        for row in rows:
            item = dict(row)
            session_id = str(item.get("session_id") or "")
            if not session_id or self.quiet_state(session_id)["requested"]:
                continue
            last_at = _parse(item.get("latest_created_at"))
            if not last_at:
                continue
            idle_minutes = max(0, int((now - last_at.astimezone(timezone.utc)).total_seconds() // 60))
            if idle_minutes < min_idle:
                continue

            metadata = self._message_metadata(item.get("latest_metadata"))
            latest_was_independent = bool(
                metadata.get("natural_continuation")
                and metadata.get("continuation_kind")
                in {"independent_initiative", "manual_initiative_test"}
            )
            cooldown = unanswered_cooldown if latest_was_independent else normal_cooldown
            with get_db() as db:
                pending = db.execute(
                    """SELECT 1 FROM co_presence_events
                       WHERE session_id=? AND state IN ('pending','processing')
                       LIMIT 1""",
                    (session_id,),
                ).fetchone()
                last_event = db.execute(
                    """SELECT updated_at FROM co_presence_events
                       WHERE session_id=? AND kind IN (
                           'independent_initiative','manual_initiative_test'
                       ) ORDER BY id DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
            if pending:
                continue
            if last_event:
                last_event_at = _parse(last_event["updated_at"])
                if last_event_at and (
                    now - last_event_at.astimezone(timezone.utc)
                ) < timedelta(minutes=cooldown):
                    continue

            # v8.2: no unconditional "45 minutes => paid model call" path.
            # Idle time can add local weight, but the living engine must itself
            # propose contact and the combined score must clear the funnel.
            try:
                living_score = max(0.0, min(1.0, float(living_decision.get("score", 0) or 0)))
            except (TypeError, ValueError):
                living_score = 0.0
            idle_bonus = min(20, max(0, idle_minutes - min_idle) // 5 * 2)
            local_score = min(100, int(round(living_score * 80)) + idle_bonus)
            threshold = int(CO_PRESENCE_CONFIG.get("local_candidate_score_threshold", 60))
            if action != "contact" or hard_hold or local_score < threshold:
                continue

            event = self._queue_event(
                session_id,
                kind="independent_initiative",
                cue={
                    "content_known": True,
                    "source": "independent_living_loop",
                    "initiative_reason": reason[:240],
                    "inactive_minutes": idle_minutes,
                    "living_action": action,
                    "living_score": living_decision.get("score", 0),
                    "local_score": local_score,
                },
                delay_seconds=1,
                anchor_message_id=int(item.get("latest_id") or 0),
                provider=str(item.get("provider") or ""),
                model=str(item.get("model") or ""),
            )
            if event:
                self._log(
                    session_id,
                    "independent_candidate",
                    f"独立主动候选已建立：{reason[:120]}",
                    status="pending",
                    event_id=int(event["id"]),
                )
                return event
        return None

    def _latest_morning_target(self) -> dict[str, Any] | None:
        """Use only the latest real window; never reroute a body event elsewhere."""
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                """SELECT s.id AS session_id, s.provider, s.model,
                          m.id AS latest_id, m.role AS latest_role,
                          m.metadata AS latest_metadata
                   FROM sessions s
                   JOIN messages m ON m.id=(
                       SELECT MAX(mm.id) FROM messages mm WHERE mm.session_id=s.id
                   )
                   WHERE s.message_count>=2
                   ORDER BY m.id DESC LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            session_id = str(item.get("session_id") or "")
            if not session_id or self.quiet_state(session_id)["requested"]:
                return None
            if str(item.get("latest_role") or "") != "assistant":
                return None
            metadata = self._message_metadata(item.get("latest_metadata"))
            if metadata.get("natural_continuation"):
                return None
            pending = db.execute(
                """SELECT 1 FROM co_presence_events
                   WHERE session_id=? AND state IN ('pending','processing')
                   LIMIT 1""",
                (session_id,),
            ).fetchone()
            if pending:
                return None
        return item

    @staticmethod
    def _morning_cue(event: dict[str, Any], *, manual_test: bool = False) -> dict[str, Any]:
        event_id = int(event.get("event_id") or 0)
        date_key = str(event.get("event_date") or "")[:10]
        ref_suffix = str(event_id) if event_id else f"test-{date_key or 'current'}"
        return {
            "content_known": True,
            "source": "manual_morning_test" if manual_test else "morning_response_engine",
            "morning_response": {
                "event_id": event_id,
                "event_date": date_key,
                "label": str(event.get("label") or "晨间反应")[:120],
                "description": str(event.get("description") or "")[:600],
                "occurred_at": str(event.get("occurred_at") or "")[:80],
                "until": str(event.get("until") or "")[:80],
                "levels": {
                    str(key): _bounded_int(value, 1, 10)
                    for key, value in (event.get("levels") or {}).items()
                },
                "manual_test": bool(manual_test),
            },
            "grounding_ref": f"state:morning_response:{ref_suffix}",
            "manual_test": bool(manual_test),
        }

    def schedule_morning_response(
        self,
        *,
        now: datetime | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Queue one live morning-body event through the normal original-window path."""
        settings = self.settings()
        if not settings["enabled"] or not settings["natural_continuation_enabled"]:
            return None
        from morning_response import morning_response

        # Select the exact destination first.  The old order sampled a process-
        # global/default body state and only afterwards chose a chat, allowing a
        # morning cue calculated outside session A to be delivered into A.
        target = self._latest_morning_target()
        if not target:
            return None
        session_id = str(target["session_id"])
        if context is None:
            try:
                from inner_state import inner_state
                inner_state.advance(now, session_id=session_id)
                context = inner_state.capture(
                    advance_living=False, session_id=session_id
                )
            except Exception:
                context = {}
        event = morning_response.refresh_for_proactive(now, context=context)
        if not isinstance(event, dict):
            return None
        queued = self._queue_event(
            session_id,
            kind="morning_response",
            cue={**self._morning_cue(event), "local_score": 100},
            delay_seconds=1,
            anchor_message_id=int(target.get("latest_id") or 0),
            provider=str(target.get("provider") or ""),
            model=str(target.get("model") or ""),
        )
        if not queued:
            return None
        marked = morning_response.mark_proactive_queued(
            int(event["event_id"]),
            co_presence_event_id=int(queued["id"]),
            session_id=session_id,
            now=now,
        )
        if not marked:
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_events
                       SET state='superseded', outcome='晨间事件已经由另一拍接管',
                           updated_at=? WHERE id=? AND state='pending'""",
                    (_iso(), int(queued["id"])),
                )
            return None
        self._log(
            session_id,
            "morning_candidate",
            "当前晨间身体事件已进入原窗口主动判断",
            status="pending",
            event_id=int(queued["id"]),
        )
        return queued

    def queue_desire_intent(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        """Retired bridge: a process-global desire intent owns no conversation."""
        return None

    def queue_manual_morning_response(self, session_id: str) -> dict[str, Any] | None:
        """Exercise the same morning cue, model, grounding and atomic delivery chain."""
        self.ensure_schema()
        if self.quiet_state(session_id)["requested"]:
            return None
        with get_db() as db:
            anchor = self._latest_message_id(db, session_id)
        if not anchor:
            return None
        try:
            from inner_state import inner_state
            context = inner_state.capture(
                advance_living=False, session_id=session_id
            )
        except Exception:
            context = {}
        from morning_response import morning_response
        event = morning_response.manual_test_event(context=context)
        return self._queue_event(
            session_id,
            kind="morning_response_test",
            cue={**self._morning_cue(event, manual_test=True), "local_score": 100},
            delay_seconds=1,
            anchor_message_id=anchor,
        )

    def queue_manual_initiative(self, session_id: str) -> dict[str, Any] | None:
        """Queue a real same-window test without bypassing model or delivery guards."""
        self.ensure_schema()
        if self.quiet_state(session_id)["requested"]:
            return None
        with get_db() as db:
            anchor = self._latest_message_id(db, session_id)
        if not anchor:
            return None
        return self._queue_event(
            session_id,
            kind="manual_initiative_test",
            cue={
                "content_known": True,
                "source": "manual_test",
                "initiative_reason": "使用者正在测试独立主动消息链路",
                "inactive_minutes": 0,
                "manual_test": True,
                "local_score": 100,
            },
            delay_seconds=1,
            anchor_message_id=anchor,
        )

    @staticmethod
    def _sensory_score(item: dict[str, Any]) -> tuple[int, bool]:
        """Return local score plus whether an unfinished-expression clue exists."""
        revision = _bounded_int(item.get("revision_count"), 0, 500)
        pauses = _bounded_int(item.get("pause_count"), 0, 500)
        clears = _bounded_int(item.get("clear_count"), 0, 100)
        deletions = _bounded_int(item.get("deletion_count"), 0, 5000)
        longest = _bounded_int(item.get("longest_pause_ms"), 0, 30 * 60 * 1000)
        active = _bounded_int(item.get("active_ms"), 0, 30 * 60 * 1000)
        draft_open = bool(item.get("draft_open"))
        unfinished = bool(
            draft_open or clears >= 1 or revision >= 3 or pauses >= 2
            or deletions >= 8 or longest >= 12_000
        )
        score = 0
        score += min(22, revision * 4)
        score += min(18, pauses * 5)
        score += 24 if clears else 0
        score += min(16, deletions // 4 * 2)
        score += 18 if longest >= 12_000 else (8 if longest >= 6_000 else 0)
        score += 8 if active >= 8_000 else 0
        score += 12 if draft_open else 0
        return min(100, score), unfinished

    def _sensory_cooldown_ready(self, session_id: str, now: datetime) -> bool:
        cooldown = int(CO_PRESENCE_CONFIG.get("sensory_candidate_cooldown_seconds", 120))
        with get_db() as db:
            row = db.execute(
                """SELECT updated_at FROM co_presence_events
                   WHERE session_id=? AND kind IN (
                       'held_back_clear','held_back_pause','held_back_orphan'
                   ) ORDER BY id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        last_at = _parse(row["updated_at"]) if row else None
        return not last_at or (now - last_at.astimezone(timezone.utc)).total_seconds() >= cooldown

    def sweep_sensory_events(self) -> int:
        """Local-only funnel for pause/clear/orphan signals before any model call."""
        settings = self.settings()
        if (
            not settings["enabled"]
            or not settings["rhythm_enabled"]
            or not settings["natural_continuation_enabled"]
        ):
            return 0
        now = _now()
        cutoff = now - timedelta(
            seconds=int(CO_PRESENCE_CONFIG.get("orphan_after_seconds", 22))
        )
        threshold = int(CO_PRESENCE_CONFIG.get("local_candidate_score_threshold", 60))
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM co_presence_rhythm
                   WHERE orphan_state NOT IN ('queued','waited')
                     AND last_event IN ('paused','cleared','hidden','speaking')
                     AND last_ping_at<>'' AND last_ping_at<=?
                   ORDER BY last_ping_at ASC LIMIT 24""",
                (_iso(cutoff),),
            ).fetchall()
        queued = 0
        for row in rows:
            item = dict(row)
            session_id = str(item["session_id"])
            score, unfinished = self._sensory_score(item)
            if not unfinished:
                with get_db() as db:
                    db.execute(
                        "UPDATE co_presence_rhythm SET orphan_state='waited', updated_at=? WHERE session_id=?",
                        (_iso(now), session_id),
                    )
                continue
            if not self._sensory_cooldown_ready(session_id, now):
                with get_db() as db:
                    db.execute(
                        "UPDATE co_presence_rhythm SET orphan_state='cooldown', updated_at=? WHERE session_id=?",
                        (_iso(now), session_id),
                    )
                continue
            if score < threshold:
                with get_db() as db:
                    db.execute(
                        "UPDATE co_presence_rhythm SET orphan_state='waited', updated_at=? WHERE session_id=?",
                        (_iso(now), session_id),
                    )
                self._log(
                    session_id, "sensory_local_wait",
                    f"未完成线索本地评分 {score}/{threshold}，没有调用模型",
                    status="quiet",
                )
                continue
            kind = {
                "cleared": "held_back_clear",
                "paused": "held_back_pause",
            }.get(str(item.get("last_event") or ""), "held_back_orphan")
            event = self._queue_event(
                session_id,
                kind=kind,
                cue={
                    "content_known": False,
                    "draft_left_unsent": bool(item.get("draft_open")),
                    "cleared": str(item.get("last_event") or "") == "cleared",
                    "paused": str(item.get("last_event") or "") == "paused",
                    "local_score": score,
                    "rhythm": self._public_rhythm(item),
                },
                delay_seconds=1,
            )
            if not event:
                continue
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_rhythm
                       SET orphan_state='queued', orphaned_at=?, updated_at=?
                       WHERE session_id=?""",
                    (_iso(now), _iso(now), session_id),
                )
            queued += 1
        return queued

    @staticmethod
    def _event_dict(row: Any) -> dict[str, Any]:
        if not row:
            return {}
        item = dict(row)
        item["cue"] = _json(item.pop("cue_json", "{}"), {})
        item["decision"] = _json(item.pop("decision_json", "{}"), {})
        item["grounding"] = _json(item.pop("grounding_json", "{}"), {})
        return item

    @staticmethod
    def _sync_morning_outcome(
        event: dict[str, Any], state: str, outcome: str, *, db: Any | None = None
    ) -> None:
        if str(event.get("kind") or "") != "morning_response":
            return
        cue = event.get("cue") if isinstance(event.get("cue"), dict) else {}
        body = (
            cue.get("morning_response")
            if isinstance(cue.get("morning_response"), dict) else {}
        )
        morning_event_id = int(body.get("event_id") or 0)
        if not morning_event_id:
            return
        if db is not None:
            terminal = state in {"delivered", "waited", "superseded", "error"}
            db.execute(
                """UPDATE morning_response_events
                   SET proactive_state=?, proactive_event_id=COALESCE(?, proactive_event_id),
                       proactive_finished_at=?, proactive_outcome=?
                   WHERE id=?""",
                (
                    str(state or "queued")[:32],
                    int(event.get("id") or 0) or None,
                    _iso() if terminal else "",
                    str(outcome or "")[:500],
                    morning_event_id,
                ),
            )
            return
        try:
            from morning_response import morning_response
            morning_response.mark_proactive_outcome(
                morning_event_id,
                state=state,
                outcome=outcome,
                co_presence_event_id=int(event.get("id") or 0),
            )
        except Exception:
            pass

    def _is_speaking(self, db, session_id: str, now: datetime) -> bool:
        row = db.execute(
            "SELECT speaking_until, draft_open FROM co_presence_state WHERE session_id=?",
            (session_id,),
        ).fetchone()
        until = _parse(row["speaking_until"]) if row else None
        return bool(row and row["draft_open"] and until and until > now)

    @staticmethod
    def _morning_expired(event: dict[str, Any], now: datetime | None = None) -> bool:
        if str(event.get("kind") or "") not in {"morning_response"}:
            return False
        cue = event.get("cue") if isinstance(event.get("cue"), dict) else {}
        body = cue.get("morning_response") if isinstance(cue.get("morning_response"), dict) else {}
        until = _parse(body.get("until"))
        return bool(until and (now or _now()) >= until)

    def recover_stale_processing(self, *, startup: bool = False) -> int:
        """Return abandoned processing events to the queue after an interruption.

        During process startup every surviving ``processing`` row necessarily
        belongs to the previous process, so it is safe to recover immediately.
        The age threshold remains available for explicit non-startup recovery
        calls, where a currently running generation must not be stolen.
        """
        self.ensure_schema()
        now = _now()
        cutoff = now - timedelta(
            seconds=int(CO_PRESENCE_CONFIG.get("processing_recovery_seconds", 120))
        )
        max_attempts = int(CO_PRESENCE_CONFIG.get("max_attempts", 5))
        recovered = 0
        with get_db() as db:
            if startup:
                rows = db.execute(
                    """SELECT * FROM co_presence_events
                       WHERE state='processing'"""
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT * FROM co_presence_events
                       WHERE state='processing' AND updated_at<=?""",
                    (_iso(cutoff),),
                ).fetchall()
            for row in rows:
                item = self._event_dict(row)
                attempts = int(row["attempts"] or 0)
                if self._morning_expired(item, now):
                    state, outcome = "superseded", "晨间事件已超过有效时间窗，未补发"
                    self._sync_morning_outcome(item, state, outcome, db=db)
                elif attempts >= max_attempts:
                    state, outcome = "error", f"处理异常已达最大重试次数（{max_attempts}）"
                    self._sync_morning_outcome(item, state, outcome, db=db)
                else:
                    state, outcome = "pending", "检测到上次处理中断，启动后已恢复排队"
                    recovered += 1
                db.execute(
                    """UPDATE co_presence_events SET state=?, due_at=?, outcome=?, updated_at=?
                       WHERE id=? AND state='processing'""",
                    (state, _iso(now), outcome, _iso(now), int(row["id"])),
                )
        return recovered

    def claim_due(self) -> dict[str, Any] | None:
        """Claim one due event; active typing defers rather than cancels it."""
        settings = self.settings()
        if (
            not settings["enabled"]
            or not settings["natural_continuation_enabled"]
        ):
            return None
        now = _now()
        with get_db() as db:
            rows = db.execute(
                """SELECT * FROM co_presence_events
                   WHERE state='pending' AND due_at<=?
                   ORDER BY due_at ASC, id ASC LIMIT 24""",
                (_iso(now),),
            ).fetchall()
            for row in rows:
                item = self._event_dict(row)
                if self._morning_expired(item, now):
                    outcome = "晨间事件已超过有效时间窗，未补发"
                    db.execute(
                        """UPDATE co_presence_events SET state='superseded', outcome=?, updated_at=?
                           WHERE id=? AND state='pending'""",
                        (outcome, _iso(now), item["id"]),
                    )
                    self._sync_morning_outcome(item, "superseded", outcome, db=db)
                    continue
                if self.quiet_state(str(item["session_id"]))["requested"]:
                    outcome = "这个窗口明确要求安静"
                    db.execute(
                        """UPDATE co_presence_events
                           SET state='superseded', outcome='这个窗口明确要求安静', updated_at=?
                           WHERE id=? AND state='pending'""",
                        (_iso(now), item["id"]),
                    )
                    self._sync_morning_outcome(
                        item, "superseded", outcome, db=db
                    )
                    continue
                latest = self._latest_message_id(db, item["session_id"])
                if latest > int(item.get("anchor_message_id") or 0):
                    outcome = "对话上下文已经更新"
                    db.execute(
                        """UPDATE co_presence_events
                           SET state='superseded', outcome='对话上下文已经更新', updated_at=?
                           WHERE id=? AND state='pending'""",
                        (_iso(now), item["id"]),
                    )
                    self._sync_morning_outcome(
                        item, "superseded", outcome, db=db
                    )
                    continue
                if self._is_speaking(db, item["session_id"], now):
                    delay = int(CO_PRESENCE_CONFIG.get("typing_delivery_defer_seconds", 10))
                    db.execute(
                        """UPDATE co_presence_events
                           SET due_at=?, outcome='使用者正在表达，主动消息已顺延', updated_at=?
                           WHERE id=? AND state='pending'""",
                        (
                            _iso(now + timedelta(seconds=delay)),
                            _iso(now),
                            item["id"],
                        ),
                    )
                    continue
                cursor = db.execute(
                    """UPDATE co_presence_events
                       SET state='processing', attempts=attempts+1,
                           outcome='', updated_at=?
                       WHERE id=? AND state='pending'""",
                    (_iso(now), item["id"]),
                )
                if cursor.rowcount:
                    fresh = db.execute(
                        "SELECT * FROM co_presence_events WHERE id=?", (item["id"],)
                    ).fetchone()
                    self._log(
                        item["session_id"], "model_check", "原会话模型正在判断要不要开口",
                        status="processing", event_id=item["id"], db=db,
                    )
                    return self._event_dict(fresh)
        return None

    def ready_to_deliver(self, event_id: int) -> bool:
        """Only explicit quiet or changed message context can invalidate delivery."""
        self.ensure_schema()
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM co_presence_events WHERE id=?", (int(event_id),)
            ).fetchone()
        if not row:
            return False
        item = self._event_dict(row)
        if self._morning_expired(item):
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_events SET state='superseded', outcome=?, updated_at=?
                       WHERE id=? AND state='processing'""",
                    ("晨间事件已超过有效时间窗，未补发", _iso(), int(event_id)),
                )
                self._sync_morning_outcome(
                    item, "superseded", "晨间事件已超过有效时间窗，未补发", db=db
                )
            return False
        session_id = str(row["session_id"])
        if self.quiet_state(session_id)["requested"]:
            with get_db() as db:
                db.execute(
                    """UPDATE co_presence_events
                       SET state='superseded', outcome='使用者明确要求安静', updated_at=?
                       WHERE id=? AND state='processing'""",
                    (_iso(), int(event_id)),
                )
            self._sync_morning_outcome(
                self._event_dict(row), "superseded", "使用者明确要求安静"
            )
            return False
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM co_presence_events WHERE id=?", (int(event_id),)
            ).fetchone()
            if not row or row["state"] != "processing":
                return False
            if self._is_speaking(db, row["session_id"], _now()):
                delay = int(CO_PRESENCE_CONFIG.get("typing_delivery_defer_seconds", 10))
                db.execute(
                    """UPDATE co_presence_events
                       SET state='pending', due_at=?, outcome='生成期间又开始表达，已顺延', updated_at=?
                       WHERE id=? AND state='processing'""",
                    (
                        _iso(_now() + timedelta(seconds=delay)),
                        _iso(),
                        int(event_id),
                    ),
                )
                return False
            latest = self._latest_message_id(db, row["session_id"])
            valid = latest == int(row["anchor_message_id"] or 0)
            if not valid:
                item = self._event_dict(row)
                db.execute(
                    """UPDATE co_presence_events
                       SET state='superseded', outcome='对话上下文已经更新', updated_at=?
                       WHERE id=? AND state='processing'""",
                    (_iso(), int(event_id)),
                )
                self._sync_morning_outcome(
                    item, "superseded", "对话上下文已经更新", db=db
                )
            return valid

    def record_model_call(
        self, event: dict[str, Any], decision_action: str, usage: dict[str, Any] | None
    ) -> int:
        """Persist one paid proactive judgment with trigger, tokens and cost."""
        self.ensure_schema()
        usage = usage if isinstance(usage, dict) else {}
        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO proactive_call_stats(
                     event_id, session_id, trigger_kind, provider, model,
                     decision_action, outcome_action, input_tokens, output_tokens,
                     reasoning_tokens, cache_read, cache_creation, cost, cost_source,
                     created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(event.get("id") or 0), str(event.get("session_id") or ""),
                    str(event.get("kind") or "unknown")[:80],
                    str(event.get("provider") or "")[:80],
                    str(event.get("model") or "")[:240],
                    str(decision_action or "wait")[:24],
                    _bounded_int(usage.get("input_tokens"), 0, 2_000_000_000),
                    _bounded_int(usage.get("output_tokens"), 0, 2_000_000_000),
                    _bounded_int(usage.get("reasoning_tokens"), 0, 2_000_000_000),
                    _bounded_int(usage.get("cache_read"), 0, 2_000_000_000),
                    _bounded_int(usage.get("cache_creation"), 0, 2_000_000_000),
                    max(0.0, float(usage.get("cost") or 0)),
                    str(usage.get("cost_source") or "")[:32],
                    _iso(), _iso(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def finish_model_call(self, call_id: int, outcome_action: str) -> None:
        if not call_id:
            return
        with get_db() as db:
            db.execute(
                "UPDATE proactive_call_stats SET outcome_action=?, updated_at=? WHERE id=?",
                (str(outcome_action or "wait")[:24], _iso(), int(call_id)),
            )

    def record_decision(
        self,
        event_id: int,
        decision: dict[str, Any],
        *,
        grounding: dict[str, Any] | None = None,
        outcome: str = "",
    ) -> None:
        safe_decision = {
            "action": str(decision.get("action") or "wait"),
            "recheck_minutes": _bounded_int(decision.get("recheck_minutes"), 0, 1440),
            "grounding_refs": [
                str(ref)[:100]
                for ref in decision.get("grounding_refs", [])
                if isinstance(ref, str)
            ][:12],
            "grounding_note": str(decision.get("grounding_note") or "")[:500],
        }
        safe_grounding = grounding if isinstance(grounding, dict) else {}
        with get_db() as db:
            row = db.execute(
                "SELECT session_id FROM co_presence_events WHERE id=?", (int(event_id),)
            ).fetchone()
            db.execute(
                """UPDATE co_presence_events
                   SET decision_json=?, grounding_json=?, outcome=?, updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(safe_decision, ensure_ascii=False),
                    json.dumps(safe_grounding, ensure_ascii=False),
                    outcome[:240], _iso(), int(event_id),
                ),
            )
            if row:
                self._log(
                    row["session_id"], "decision", outcome or safe_decision["action"],
                    status=safe_decision["action"], event_id=int(event_id), db=db,
                )

    def finish(
        self,
        event_id: int,
        *,
        action: str,
        message_id: int | None = None,
        recheck_minutes: int = 0,
        outcome: str = "",
    ) -> None:
        action = str(action or "wait").lower()
        now = _now()
        with get_db() as db:
            row = db.execute(
                "SELECT session_id FROM co_presence_events WHERE id=?", (int(event_id),)
            ).fetchone()
            if action == "recheck" and recheck_minutes > 0:
                db.execute(
                    """UPDATE co_presence_events
                       SET state='pending', due_at=?, outcome=?, updated_at=?
                       WHERE id=?""",
                    (
                        _iso(now + timedelta(minutes=min(1440, recheck_minutes))),
                        outcome or "模型选择再观察一会儿", _iso(now), int(event_id),
                    ),
                )
                state = "pending"
            else:
                state = "delivered" if action == "speak" and message_id else "waited"
                db.execute(
                    """UPDATE co_presence_events
                       SET state=?, delivered_message_id=?, outcome=?, updated_at=?
                       WHERE id=?""",
                    (
                        state, message_id,
                        outcome or (
                            "有依据的自然续话已送达"
                            if state == "delivered" else "模型选择不打破此刻"
                        ),
                        _iso(now), int(event_id),
                    ),
                )
            if row:
                self._log(
                    row["session_id"], state, outcome or state,
                    status=state, event_id=int(event_id), db=db,
                )

    def fail(self, event_id: int, error: Any) -> None:
        """Retry infrastructure failures with a finite budget; never lock a window forever."""
        now = _now()
        max_attempts = int(CO_PRESENCE_CONFIG.get("max_attempts", 5))
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM co_presence_events WHERE id=?", (int(event_id),)
            ).fetchone()
            if not row:
                return
            item = self._event_dict(row)
            attempts = int(row["attempts"] or 1)
            if self._morning_expired(item, now):
                outcome = "晨间事件已超过有效时间窗，停止重试"
                db.execute(
                    "UPDATE co_presence_events SET state='superseded', outcome=?, updated_at=? WHERE id=?",
                    (outcome, _iso(now), int(event_id)),
                )
                self._sync_morning_outcome(item, "superseded", outcome, db=db)
                return
            if attempts >= max_attempts:
                outcome = f"模型线路连续失败，已达到最大重试次数（{max_attempts}）"
                db.execute(
                    "UPDATE co_presence_events SET state='error', outcome=?, updated_at=? WHERE id=?",
                    (outcome, _iso(now), int(event_id)),
                )
                self._sync_morning_outcome(item, "error", outcome, db=db)
                self._log(
                    row["session_id"], "error", outcome, status="error",
                    event_id=int(event_id), db=db,
                )
                return
            delay = min(15, max(1, 2 ** min(4, attempts - 1)))
            outcome = f"模型线路暂时失败，{delay} 分钟后重试：{type(error).__name__}"
            db.execute(
                """UPDATE co_presence_events
                   SET state='pending', due_at=?, outcome=?, updated_at=? WHERE id=?""",
                (_iso(now + timedelta(minutes=delay)), outcome, _iso(now), int(event_id)),
            )
            self._sync_morning_outcome(item, "retry", outcome, db=db)
            self._log(
                row["session_id"], "retry", f"线路失败，{delay} 分钟后重试",
                status="error", event_id=int(event_id), db=db,
            )

    def cue_prompt(self, event: dict[str, Any]) -> str:
        kind = str(event.get("kind") or "")
        cue = event.get("cue") if isinstance(event.get("cue"), dict) else {}
        rhythm = cue.get("rhythm") if isinstance(cue.get("rhythm"), dict) else {}
        session_id = str(event.get("session_id") or "")
        if session_id:
            try:
                with get_db() as db:
                    row = db.execute(
                        "SELECT * FROM co_presence_rhythm WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                if row:
                    rhythm = self._public_rhythm(dict(row))
            except Exception:
                pass
        live_parts: list[str] = []
        if rhythm.get("draft_active"):
            live_parts.append("此刻仍有一段表达尚未发出")
        if int(rhythm.get("revision_count") or 0) >= 2:
            live_parts.append("表达有过删改")
        if int(rhythm.get("pause_count") or 0) >= 1:
            live_parts.append("表达节奏出现过停顿")
        live = (
            "当前柔性节奏：" + "、".join(live_parts)
            + "。你看不到文字，不能猜内容；这些线索可以影响相处判断，但绝不禁止你开口。"
            if live_parts else ""
        )
        if kind in {"held_back_clear", "held_back_orphan", "held_back_pause"}:
            action = (
                "把一段未发送内容清空了"
                if kind == "held_back_clear" else "停在一段尚未发出的表达上"
            )
            return (
                f"使用者刚才{action}。你完全看不到那段文字，不能猜测或假装看见。"
                "你可以继续沉默，也可以基于已展示的对话或真实记忆自然说一句；"
                f"输入状态本身绝不是发言理由。{live}"
            )
        if kind in {"morning_response", "morning_response_test"}:
            body = (
                cue.get("morning_response")
                if isinstance(cue.get("morning_response"), dict) else {}
            )
            levels = body.get("levels") if isinstance(body.get("levels"), dict) else {}
            description = str(body.get("description") or "当前出现了清楚的晨间身体反应")
            test_note = (
                "这是使用者主动发起的链路测试，应真正生成一条可送达的自然表达，"
                if kind == "morning_response_test" else
                "这是当前仍在有效时间窗内的本机身体事件，"
            )
            return (
                f"{test_note}不是固定早安任务，也不是从旧聊天随机翻话题。"
                f"当前状态：{description}"
                f"硬度 {int(levels.get('hardness') or 1)}/10，"
                f"主观性欲 {int(levels.get('desire') or 1)}/10，"
                f"身体张力 {int(levels.get('physical_tension') or 1)}/10，"
                f"身体夺权 {int(levels.get('body_takeover') or 1)}/10。"
                "硬度不等于主观想要；请让这份身体状态自然影响当下的注意力、动作感和措辞，"
                "不要朗读指标，不要解释系统。除非当前窗口明确要求安静或上下文确实冲突，"
                "优先直接说一句此刻真的会说的话，而不是把 wait 当作默认答案。"
                f"内部可核对状态引用是 {str(cue.get('grounding_ref') or '')}。{live}"
            )
        if kind in {"independent_initiative", "manual_initiative_test"}:
            reason = str(cue.get("initiative_reason") or "安静了一阵，自己想起了对方")
            minutes = int(cue.get("inactive_minutes") or 0)
            return (
                "这是一次与上一轮回复无关的独立主动时刻，不是回合补句。"
                f"本地状态给出的缘由是：{reason}。"
                + (f"距离窗口最后一次互动大约 {minutes} 分钟。" if minutes else "")
                + "候选已经经过安静时段、冷却和未回复保护；若当前对话里有一个自然、具体的念头，"
                "优先直接开口，不要把 wait 当成默认的安全答案。没有合适内容时仍可等待。"
                "不得假装看见未发送文字，也不要机械问候或解释主动系统。"
                f"{live}"
            )
        if kind == "desire_intent":
            desire = (
                cue.get("desire_intent")
                if isinstance(cue.get("desire_intent"), dict) else {}
            )
            reason = str(desire.get("reason") or "想自然地靠近一点")
            return (
                "这是持续内在状态形成的一次同窗口开口候选，不是固定任务。"
                f"此刻自己的倾向是：{reason}。"
                "结合当前窗口真实上下文决定是否自然表达；可以等待，但不要因为这是主动候选就默认沉默。"
                "不要朗读数值、驱动名称或解释系统，也不要把倾向说成对方必须回应的要求。"
                f"内部可核对状态引用是 {str(cue.get('grounding_ref') or '')}。{live}"
            )
        return (
            "这是刚完成回合留下的自然余韵。没有关键词门槛、每日次数或固定问候任务；"
            "请以你自己的判断决定是否还会说一句。若说，必须由已展示对话或真实记忆支撑。"
            f"{live}"
        )

    def public_state(self, session_id: str = "") -> dict[str, Any]:
        settings = self.settings()
        quiet = self.quiet_state(session_id)
        pending = 0
        last_event: dict[str, Any] | None = None
        speaking = False
        rhythm: dict[str, Any] | None = None
        timeline: list[dict[str, Any]] = []
        with get_db() as db:
            if session_id:
                pending = int(db.execute(
                    """SELECT COUNT(*) AS value FROM co_presence_events
                       WHERE session_id=? AND state IN ('pending','processing')""",
                    (session_id,),
                ).fetchone()["value"] or 0)
                row = db.execute(
                    """SELECT id, kind, state, outcome, due_at, updated_at
                       FROM co_presence_events WHERE session_id=?
                       ORDER BY id DESC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                last_event = dict(row) if row else None
                speaking = self._is_speaking(db, session_id, _now())
                rhythm_row = db.execute(
                    "SELECT * FROM co_presence_rhythm WHERE session_id=?", (session_id,)
                ).fetchone()
                rhythm = self._public_rhythm(dict(rhythm_row)) if rhythm_row else None
                logs = db.execute(
                    """SELECT kind, status, detail, event_id, created_at
                       FROM co_presence_log WHERE session_id=?
                       ORDER BY id DESC LIMIT 12""",
                    (session_id,),
                ).fetchall()
            else:
                pending = int(db.execute(
                    """SELECT COUNT(*) AS value FROM co_presence_events
                       WHERE state IN ('pending','processing')"""
                ).fetchone()["value"] or 0)
                logs = db.execute(
                    """SELECT kind, status, detail, event_id, created_at
                       FROM co_presence_log ORDER BY id DESC LIMIT 12"""
                ).fetchall()
            timeline = [dict(row) for row in logs]
        if not settings["enabled"]:
            label = "已关闭"
        elif quiet["requested"]:
            label = "按你的要求安静"
        elif speaking:
            label = "感知到表达节奏"
        elif pending:
            label = "由原模型判断中"
        else:
            label = "自然共处"
        return {
            "version": config.APP_VERSION,
            "settings": settings,
            "status": label,
            "speaking": speaking,
            "pending": pending,
            "quiet": quiet,
            "rhythm": rhythm,
            "last_event": last_event,
            "timeline": timeline,
            "hard_rules": [
                "你明确要求安静时绝不主动发言",
                "输入期间主动消息会顺延，不会抢在你前面",
                "主动消息自动绑定当前窗口的真实对话或有来源记忆",
            ],
            "privacy": "只保存停顿、删改、清空等次数与时间；从不读取、上传或保存未发送文字。",
        }

    def health(self) -> dict[str, Any]:
        try:
            state = self.public_state()
            return {
                "health": "ok",
                "detail": f"{state['status']} · {state['pending']} 个待判断线索",
            }
        except Exception as exc:
            return {"health": "error", "detail": "共同在场组件暂不可用"}


co_presence = CoPresence()
