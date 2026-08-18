"""
数据库模型 - 大西瓜核心 SQLite
三个AI入口（定时任务AI、大西瓜chat、Claude端）读写同一个库
"""
import sqlite3
import os
import json
import math
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH, CACHE_CONFIG

# 确保 data 目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


@contextmanager
def get_db():
    """线程安全的数据库连接"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # 并发读写
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化所有表"""
    with get_db() as db:
        db.executescript("""
        -- ── 会话表 ──
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            title       TEXT DEFAULT '新对话',
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now')),
            provider    TEXT DEFAULT 'anthropic',
            model       TEXT DEFAULT '',
            total_cost  REAL DEFAULT 0.0,
            message_count INTEGER DEFAULT 0
        );

        -- ── 消息表 ──
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,  -- user / assistant / system
            content     TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            -- token 统计
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cache_read      INTEGER DEFAULT 0,
            cache_creation  INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            -- 元数据
            provider    TEXT DEFAULT '',
            model       TEXT DEFAULT '',
            metadata    TEXT DEFAULT '{}',
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_msg_time ON messages(created_at);

        -- ── 消息收藏（只保存引用与用户备注，不复制原文）──
        CREATE TABLE IF NOT EXISTS message_favorites (
            message_id  INTEGER PRIMARY KEY,
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_message_favorites_updated
            ON message_favorites(updated_at DESC);

        -- ── 记忆表（三个AI共享）──
        CREATE TABLE IF NOT EXISTS memories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            content     TEXT NOT NULL,
            source      TEXT DEFAULT 'chat',
            category    TEXT DEFAULT 'general',
            importance  REAL DEFAULT 0.5,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now')),
            embedding   BLOB,
            metadata    TEXT DEFAULT '{}',
            -- Russell 情感坐标（Ombre Brain）
            valence     REAL DEFAULT 0.0,
            arousal     REAL DEFAULT 0.5,
            -- 遗忘曲线（Ombre Brain）
            decay_score REAL DEFAULT 1.0,
            activation_count INTEGER DEFAULT 0,
            last_activated_at TEXT,
            resolved    INTEGER DEFAULT 0,
            archived    INTEGER DEFAULT 0,
            -- 域分类
            domain      TEXT DEFAULT 'other',
            -- 氛围标签（四路检索）
            tags        TEXT DEFAULT '',
            topic_hint  TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_mem_category ON memories(category);
        CREATE INDEX IF NOT EXISTS idx_mem_importance ON memories(importance);
        CREATE INDEX IF NOT EXISTS idx_mem_decay ON memories(decay_score);
        CREATE INDEX IF NOT EXISTS idx_mem_domain ON memories(domain);
        CREATE INDEX IF NOT EXISTS idx_mem_archived ON memories(archived);
        CREATE INDEX IF NOT EXISTS idx_mem_tags ON memories(tags);

        -- ── Token 统计表（按条记录）──
        CREATE TABLE IF NOT EXISTS token_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            message_id  INTEGER,
            provider    TEXT,
            model       TEXT,
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cache_read      INTEGER DEFAULT 0,
            cache_creation  INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            -- 历史价格快照（换API后不失真）
            price_snapshot  TEXT DEFAULT '{}',
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_stats_session ON token_stats(session_id);

        -- ── API 调用审计：只记录模型/令牌/费用/用途，不保存提示词正文 ──
        CREATE TABLE IF NOT EXISTS api_call_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      TEXT,
            provider        TEXT DEFAULT '',
            model           TEXT DEFAULT '',
            purpose         TEXT DEFAULT 'unspecified',
            upstream_requests INTEGER DEFAULT 1,
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            cache_read      INTEGER DEFAULT 0,
            cache_creation  INTEGER DEFAULT 0,
            cost            REAL DEFAULT 0.0,
            cost_source     TEXT DEFAULT '',
            price_snapshot  TEXT DEFAULT '{}',
            created_at      TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_api_calls_created ON api_call_stats(created_at);
        CREATE INDEX IF NOT EXISTS idx_api_calls_session ON api_call_stats(session_id);
        CREATE INDEX IF NOT EXISTS idx_api_calls_purpose ON api_call_stats(purpose);

        -- ── Claude 缓存原文窗口锚点 ──
        -- 只保存消息 ID 和窗口代数，不保存任何聊天正文。旧版每轮都从
        -- “最新 N 条”重新取历史，窗口一满最前面的消息就会每轮变化，
        -- 导致第二个 prompt-cache 前缀持续冷写。这个锚点让窗口起点保持
        -- 不动，只有真正触及条数/字符预算时才成块前移一次。
        CREATE TABLE IF NOT EXISTS cache_context_windows (
            session_id        TEXT PRIMARY KEY,
            anchor_message_id INTEGER NOT NULL DEFAULT 0,
            generation        INTEGER NOT NULL DEFAULT 0,
            rotated_at        TEXT,
            updated_at        TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        -- ── 推送订阅表 ──
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint    TEXT UNIQUE NOT NULL,
            keys_p256dh TEXT NOT NULL,
            keys_auth   TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            active      INTEGER DEFAULT 1
        );

        -- ── 屏幕时间表 ──
        CREATE TABLE IF NOT EXISTS screen_time (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,  -- app_open / app_close
            app_name    TEXT DEFAULT '',
            timestamp   TEXT DEFAULT (datetime('now')),
            duration_seconds INTEGER DEFAULT 0,
            metadata    TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_screen_time ON screen_time(timestamp);

        -- ── 主动关心记录 ──
        CREATE TABLE IF NOT EXISTS proactive_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content     TEXT NOT NULL,
            trigger_reason TEXT DEFAULT '',
            sent_at     TEXT DEFAULT (datetime('now')),
            push_sent   INTEGER DEFAULT 0,
            session_id  TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        -- ── 每日打卡 ──
        CREATE TABLE IF NOT EXISTS daily_checkin (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            mood        TEXT DEFAULT '',
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_checkin_date ON daily_checkin(date);
        """)

        # v7.9.5: older installations already have token_stats, so these fields
        # must be added without rebuilding or discarding the user's database.
        # Keeping this inside init_db also makes isolated tests and restore
        # workflows safe even when the app lifespan migrations have not run.
        stat_columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(token_stats)").fetchall()
        }
        if "reasoning_tokens" not in stat_columns:
            db.execute(
                "ALTER TABLE token_stats ADD COLUMN reasoning_tokens INTEGER DEFAULT 0"
            )
        if "cost_source" not in stat_columns:
            db.execute(
                "ALTER TABLE token_stats ADD COLUMN cost_source TEXT DEFAULT ''"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_stats_created ON token_stats(created_at)"
        )

        # v7.9.5-cache-rebuild: extend the gateway audit with cache diagnostics.
        # These fields contain hashes/IDs/counters only; prompt and response
        # text remain deliberately absent from api_call_stats.
        call_columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(api_call_stats)").fetchall()
        }
        call_additions = {
            "cache_creation_1h": "INTEGER DEFAULT 0",
            "cache_creation_5m": "INTEGER DEFAULT 0",
            "generation_id": "TEXT DEFAULT ''",
            "actual_provider": "TEXT DEFAULT ''",
            "actual_model": "TEXT DEFAULT ''",
            "router_region": "TEXT DEFAULT ''",
            "router_strategy": "TEXT DEFAULT ''",
            "cache_prefix_hash": "TEXT DEFAULT ''",
            "cache_parent_hash": "TEXT DEFAULT ''",
            "cache_shape_hash": "TEXT DEFAULT ''",
            "cache_ttl": "TEXT DEFAULT ''",
            "cache_breakpoint": "TEXT DEFAULT ''",
            "cache_guard_status": "TEXT DEFAULT ''",
            "cache_prefix_chars": "INTEGER DEFAULT 0",
            "cache_prefix_tokens_estimate": "INTEGER DEFAULT 0",
            "cache_min_tokens": "INTEGER DEFAULT 0",
            "cache_strategy": "TEXT DEFAULT ''",
            "cache_control_count": "INTEGER DEFAULT 0",
            "cache_tools_hash": "TEXT DEFAULT ''",
            "cache_segment_hashes": "TEXT DEFAULT ''",
            "cache_fingerprint": "TEXT DEFAULT ''",
            "cache_last_touch_at": "REAL DEFAULT 0",
            "cache_next_warm_at": "REAL DEFAULT 0",
        }
        for column, declaration in call_additions.items():
            if column not in call_columns:
                db.execute(
                    f"ALTER TABLE api_call_stats ADD COLUMN {column} {declaration}"
                )


# ── CRUD 工具函数 ──

def create_session(session_id: str, provider: str = "", model: str = "") -> dict:
    with get_db() as db:
        db.execute(
            "INSERT INTO sessions (id, provider, model) VALUES (?, ?, ?)",
            (session_id, provider, model)
        )
        return {"id": session_id, "provider": provider, "model": model}


def save_message(session_id: str, role: str, content: str,
                 tokens: dict = None, provider: str = "", model: str = "",
                 metadata: dict | None = None,
                 expected_latest_id: int | None = None,
                 required_co_presence_event_id: int | None = None) -> int:
    """保存消息并更新统计。

    ``expected_latest_id`` 与 ``required_co_presence_event_id`` 只供 v7.3
    的同窗口自然续话使用。两项检查和消息写入位于同一个
    ``BEGIN IMMEDIATE`` 事务里：若使用者刚好先继续说话，续话会返回
    ``0`` 并保持安静，不会在新消息之后抢着插入。
    """
    tokens = tokens or {}
    input_t = tokens.get("input_tokens", 0)
    output_t = tokens.get("output_tokens", 0)
    cache_r = tokens.get("cache_read", 0)
    cache_c = tokens.get("cache_creation", 0)
    reasoning_t = tokens.get("reasoning_tokens", 0)
    cost = tokens.get("cost", 0.0)
    cost_source = str(tokens.get("cost_source", "") or "")
    price_snapshot = tokens.get("price_snapshot", {})
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)

    with get_db() as db:
        if (
            expected_latest_id is not None
            or required_co_presence_event_id is not None
        ):
            db.execute("BEGIN IMMEDIATE")
        if expected_latest_id is not None:
            latest = db.execute(
                """SELECT COALESCE(MAX(id), 0) AS value
                   FROM messages WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            if int(latest["value"] or 0) != max(0, int(expected_latest_id)):
                return 0
        if required_co_presence_event_id is not None:
            table_exists = db.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='co_presence_events'"""
            ).fetchone()
            event = (
                db.execute(
                    """SELECT session_id, state FROM co_presence_events
                       WHERE id=?""",
                    (int(required_co_presence_event_id),),
                ).fetchone()
                if table_exists else None
            )
            if (
                not event
                or str(event["session_id"]) != str(session_id)
                or str(event["state"]) != "processing"
            ):
                return 0
        cursor = db.execute(
            """INSERT INTO messages 
            (session_id, role, content, input_tokens, output_tokens, 
             cache_read, cache_creation, cost, provider, model, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, role, content, input_t, output_t,
             cache_r, cache_c, cost, provider, model, metadata_json)
        )
        msg_id = cursor.lastrowid

        # 写入统计表（带价格快照）
        db.execute(
            """INSERT INTO token_stats
            (session_id, message_id, provider, model, 
             input_tokens, output_tokens, reasoning_tokens,
             cache_read, cache_creation, cost, cost_source, price_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, msg_id, provider, model,
             input_t, output_t, reasoning_t, cache_r, cache_c,
             cost, cost_source, json.dumps(price_snapshot))
        )

        # 更新 session 汇总
        db.execute(
            """UPDATE sessions SET 
            updated_at = datetime('now'),
            total_cost = total_cost + ?,
            message_count = message_count + 1,
            provider = CASE WHEN ? <> '' THEN ? ELSE provider END,
            model = CASE WHEN ? <> '' THEN ? ELSE model END
            WHERE id = ?""",
            (cost, provider, provider, model, model, session_id)
        )
        return msg_id


def get_session_messages(session_id: str, limit: int = 100) -> list:
    """Return the latest ``limit`` messages in chronological order.

    The old query returned the *oldest* 100 messages, so after a longer chat the
    model stopped seeing anything new. Ordering by the integer id also avoids
    ties when several messages share the same SQLite second-level timestamp.
    """
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM (
                   SELECT * FROM messages
                   WHERE session_id = ?
                   ORDER BY id DESC
                   LIMIT ?
               ) recent
               ORDER BY id ASC""",
            (session_id, limit)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_metadata = item.get("metadata")
            if isinstance(raw_metadata, str):
                try:
                    item["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
                except Exception:
                    item["metadata"] = {}
            elif not isinstance(raw_metadata, dict):
                item["metadata"] = {}
            result.append(item)
        return result


def _context_clip_marker(original_chars: int) -> str:
    return (
        f"\n\n[中间内容因单轮上下文安全上限省略；原消息共 {original_chars:,} 字，"
        "完整原文仍保存在本机聊天记录中。超长材料请改用文件导入。]\n\n"
    )


def _clip_context_message_parts(
    head_source: str,
    tail_source: str,
    *,
    original_chars: int,
    max_chars: int,
) -> tuple[str, bool]:
    """Compose a bounded head/tail view without requiring the full source."""
    cap = max(1, int(max_chars))
    if int(original_chars) <= cap:
        return str(head_source or "")[:cap], False
    marker = _context_clip_marker(int(original_chars))
    if cap <= len(marker) + 2:
        return marker[:cap], True
    remaining = cap - len(marker)
    head = (remaining + 1) // 2
    tail = remaining - head
    clipped = (
        str(head_source or "")[:head]
        + marker
        + (str(tail_source or "")[-tail:] if tail else "")
    )
    return clipped[:cap], True


def _clip_context_message_text(content: str, max_chars: int) -> tuple[str, bool]:
    """Keep both ends of an oversized historical message under a hard cap."""
    text = str(content or "")
    return _clip_context_message_parts(
        text, text, original_chars=len(text), max_chars=max_chars
    )


def select_recent_context_messages(
    messages: list[dict],
    *,
    max_chars: int,
    single_message_max_chars: int,
    max_tokens: int | None = None,
    single_message_max_tokens: int | None = None,
) -> dict:
    """Select newest-first under a strict total character budget.

    ``messages`` must be chronological.  Selection walks backwards and stops
    as soon as the next older message would cross the budget.  Oversized
    individual imported messages retain their beginning and end; the database
    value is never changed.
    """
    budget = max(1, int(max_chars))
    per_message = max(1, min(int(single_message_max_chars), budget))
    token_budget = max(1, int(max_tokens if max_tokens is not None else 10**9))
    per_message_tokens = max(
        1,
        min(
            int(single_message_max_tokens if single_message_max_tokens is not None else token_budget),
            token_budget,
        ),
    )
    chosen_reversed: list[dict] = []
    used = 0
    used_tokens = 0
    truncated = 0
    selected_source_chars = 0

    for raw in reversed(messages):
        original = str(raw.get("content") or "")
        source_tokens = estimate_context_tokens_from_lengths(
            len(original), len(original.encode("utf-8", "ignore"))
        )
        message_cap = per_message
        if source_tokens > per_message_tokens:
            message_cap = min(
                message_cap,
                max(1, math.floor(len(original) * per_message_tokens / source_tokens)),
            )
        content, was_truncated = _clip_context_message_text(original, message_cap)
        chars = len(content)
        tokens = estimate_context_tokens_from_lengths(
            chars, len(content.encode("utf-8", "ignore"))
        )
        if chosen_reversed and (
            used + chars > budget or used_tokens + tokens > token_budget
        ):
            break
        # The newest item is always retained.  per_message <= budget keeps it
        # within the same hard ceiling even for an enormous imported message.
        item = dict(raw)
        item["content"] = content
        if was_truncated:
            item["_context_truncated"] = True
            item["_context_original_chars"] = len(original)
            truncated += 1
        chosen_reversed.append(item)
        used += chars
        used_tokens += tokens
        selected_source_chars += len(original)

    chosen = list(reversed(chosen_reversed))
    return {
        "items": chosen,
        "stats": {
            "candidate_messages": len(messages),
            "selected_messages": len(chosen),
            "dropped_messages": max(0, len(messages) - len(chosen)),
            "truncated_messages": truncated,
            "selected_chars": used,
            "selected_tokens_estimate": used_tokens,
            "selected_source_chars": selected_source_chars,
            "candidate_source_chars": sum(
                len(str(item.get("content") or "")) for item in messages
            ),
            "max_chars": budget,
            "max_tokens": token_budget,
            "single_message_max_chars": per_message,
            "single_message_max_tokens": per_message_tokens,
            "oldest_selected_id": int(chosen[0].get("id") or 0) if chosen else 0,
            "newest_selected_id": int(chosen[-1].get("id") or 0) if chosen else 0,
        },
    }



def _ensure_cache_context_window_table(db) -> None:
    """Create the non-content cache window state for restored/old databases."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS cache_context_windows (
               session_id        TEXT PRIMARY KEY,
               anchor_message_id INTEGER NOT NULL DEFAULT 0,
               generation        INTEGER NOT NULL DEFAULT 0,
               rotated_at        TEXT,
               updated_at        TEXT DEFAULT (datetime('now')),
               FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
           )"""
    )


def estimate_context_tokens_from_lengths(chars: int, byte_length: int | None = None) -> int:
    """Conservatively estimate provider tokens without sending a count request.

    A plain ``chars / 4`` rule badly under-counts Chinese/Japanese text, while
    treating every UTF-8 byte as a token wastes most of the available window
    for English and source code.  SQLite can provide character and byte lengths
    without materialising an imported multi-megabyte message.  The byte delta
    gives us a safe approximation of the non-ASCII share: those characters are
    budgeted at 1.15 tokens each, ASCII at four characters per token, plus a
    small per-message framing allowance.

    This is intentionally an estimate, not a billing claim.  The hard character
    ceiling remains in force as a second independent guard.
    """
    chars = max(0, int(chars or 0))
    if chars <= 0:
        return 0
    byte_length = max(chars, int(byte_length if byte_length is not None else chars))
    non_ascii = min(chars, max(0, math.ceil((byte_length - chars) / 2)))
    ascii_chars = max(0, chars - non_ascii)
    return max(1, math.ceil(non_ascii * 1.15 + ascii_chars / 4.0) + 4)


def calculate_claude_cache_metrics(
    *,
    uncached_input: int,
    cache_read: int,
    cache_creation: int,
    prompt_total: int | None = None,
) -> dict[str, float | None]:
    """Return cache ratios without conflating reads, writes and requests.

    Claude Console's read ratio deliberately excludes cache-creation tokens:
    ``read / (read + uncached_input)``.  That is the useful 99.x headline, but
    it can hide a large write bill, so the stricter all-input reuse and
    read/write amortisation are returned beside it.
    """
    uncached = max(0, int(uncached_input or 0))
    read = max(0, int(cache_read or 0))
    creation = max(0, int(cache_creation or 0))
    total = max(
        0,
        int(prompt_total if prompt_total is not None else uncached + read + creation),
    )
    read_side_total = read + uncached
    return {
        "cache_read_ratio": round(read / read_side_total * 100, 2)
        if read_side_total else 0.0,
        "cache_total_reuse_rate": round(read / total * 100, 2)
        if total else 0.0,
        "cache_prefix_reuse_rate": round(read / (read + creation) * 100, 2)
        if read + creation else 0.0,
        "cache_read_write_ratio": round(read / creation, 2)
        if creation else None,
    }


def _context_row_size(
    row: dict,
    *,
    per_message_chars: int,
    per_message_tokens: int,
) -> tuple[int, int]:
    source_chars = max(0, int(row.get("content_chars") or 0))
    source_bytes = max(source_chars, int(row.get("content_bytes") or source_chars))
    source_tokens = estimate_context_tokens_from_lengths(source_chars, source_bytes)
    tokens = min(source_tokens, per_message_tokens)
    char_cap = min(source_chars, per_message_chars)
    if source_tokens > per_message_tokens and source_tokens > 0:
        # Preserve both ends later, but shrink the amount loaded so a single
        # CJK/emoji-heavy message cannot bypass the token safety ceiling.
        char_cap = min(
            char_cap,
            max(1, math.floor(source_chars * per_message_tokens / source_tokens)),
        )
    clipped_bytes = max(char_cap, math.ceil(source_bytes * char_cap / max(1, source_chars)))
    clipped_tokens = min(
        tokens,
        estimate_context_tokens_from_lengths(char_cap, clipped_bytes),
    )
    return char_cap, clipped_tokens


def _recent_context_id_plan(
    db,
    session_id: str,
    *,
    row_limit: int,
    budget: int,
    per_message: int,
    token_budget: int,
    per_message_tokens: int,
    target_ratio: float,
) -> dict:
    """Build a bounded newest window, optionally leaving rotation headroom."""
    target_count = max(1, min(row_limit, int(row_limit * target_ratio)))
    target_budget = max(per_message, min(budget, int(budget * target_ratio)))
    target_token_budget = max(
        per_message_tokens,
        min(token_budget, int(token_budget * target_ratio)),
    )
    candidates = [dict(row) for row in db.execute(
        """SELECT id, LENGTH(content) AS content_chars,
                  LENGTH(CAST(content AS BLOB)) AS content_bytes
           FROM messages
           WHERE session_id=? AND role IN ('user','assistant')
           ORDER BY id DESC LIMIT ?""",
        (session_id, row_limit),
    ).fetchall()]
    selected_desc: list[dict] = []
    used_chars = 0
    used_tokens = 0
    char_caps: dict[int, int] = {}
    for row in candidates:
        chars, tokens = _context_row_size(
            row,
            per_message_chars=per_message,
            per_message_tokens=per_message_tokens,
        )
        if selected_desc and (
            len(selected_desc) >= target_count
            or used_chars + chars > target_budget
            or used_tokens + tokens > target_token_budget
        ):
            break
        selected_desc.append(row)
        used_chars += chars
        used_tokens += tokens
        char_caps[int(row["id"])] = chars
    if not selected_desc and candidates:
        row = candidates[0]
        chars, tokens = _context_row_size(
            row,
            per_message_chars=per_message,
            per_message_tokens=per_message_tokens,
        )
        selected_desc.append(row)
        used_chars = chars
        used_tokens = tokens
        char_caps[int(row["id"])] = chars
    selected = list(reversed(selected_desc))
    return {
        "ids": [int(row["id"]) for row in selected],
        "used_chars": used_chars,
        "used_tokens": used_tokens,
        "message_char_caps": char_caps,
        "anchor_message_id": int(selected[0]["id"]) if selected else 0,
    }


def _session_context_window_plan_with_db(
    db,
    session_id: str,
    *,
    limit: int = 500,
    max_chars: int = 600000,
    single_message_max_chars: int = 240000,
    max_tokens: int = 158000,
    single_message_max_tokens: int = 60000,
    stable: bool | None = None,
) -> dict:
    """Internal implementation that can reuse an existing SQLite connection."""
    row_limit = max(1, min(int(limit), 500))
    budget = max(1, int(max_chars))
    per_message = max(1, min(int(single_message_max_chars), budget))
    token_budget = max(1, int(max_tokens))
    per_message_tokens = max(
        1, min(int(single_message_max_tokens), token_budget)
    )
    if stable is None:
        stable = bool(CACHE_CONFIG.get("stable_history_window", True))
    try:
        ratio = float(CACHE_CONFIG.get("stable_history_target_ratio", 0.94) or 0.94)
    except (TypeError, ValueError):
        ratio = 0.94
    ratio = max(0.40, min(ratio, 0.96))

    if not stable:
        plan = _recent_context_id_plan(
            db, session_id, row_limit=row_limit, budget=budget,
            per_message=per_message, token_budget=token_budget,
            per_message_tokens=per_message_tokens, target_ratio=1.0,
        )
        return {
            **plan, "generation": 0, "rotated": False, "stable": False,
            "max_chars": budget, "single_message_max_chars": per_message,
            "max_tokens": token_budget,
            "single_message_max_tokens": per_message_tokens,
        }

    _ensure_cache_context_window_table(db)
    state = db.execute(
        "SELECT anchor_message_id, generation FROM cache_context_windows WHERE session_id=?",
        (session_id,),
    ).fetchone()
    anchor = int(state["anchor_message_id"] or 0) if state else 0
    generation = int(state["generation"] or 0) if state else 0
    rotated = False

    def make_fresh_plan(*, is_rotation: bool) -> dict:
        nonlocal anchor, generation, rotated
        fresh = _recent_context_id_plan(
            db, session_id, row_limit=row_limit, budget=budget,
            per_message=per_message, token_budget=token_budget,
            per_message_tokens=per_message_tokens,
            target_ratio=(ratio if is_rotation else 1.0),
        )
        anchor = int(fresh.get("anchor_message_id") or 0)
        if is_rotation:
            generation += 1
            rotated = True
        # Empty/non-existent sessions have no valid message anchor. Do not write
        # an anchor row in that case: cache_context_windows has a FK to sessions,
        # and persisting an artificial anchor=0 would turn an innocent empty read
        # into a foreign-key error.
        if anchor > 0:
            db.execute(
                """INSERT INTO cache_context_windows(
                       session_id, anchor_message_id, generation, rotated_at, updated_at
                   ) VALUES (?, ?, ?, CASE WHEN ? THEN datetime('now') ELSE NULL END, datetime('now'))
                   ON CONFLICT(session_id) DO UPDATE SET
                       anchor_message_id=excluded.anchor_message_id,
                       generation=excluded.generation,
                       rotated_at=CASE WHEN ? THEN datetime('now') ELSE cache_context_windows.rotated_at END,
                       updated_at=datetime('now')""",
                (session_id, anchor, generation, int(is_rotation), int(is_rotation)),
            )
        return fresh

    if anchor <= 0:
        fresh = make_fresh_plan(is_rotation=False)
        selected_ids = list(fresh.get("ids") or [])
        used_chars = int(fresh.get("used_chars") or 0)
        used_tokens = int(fresh.get("used_tokens") or 0)
        message_char_caps = dict(fresh.get("message_char_caps") or {})
    else:
        rows = [dict(row) for row in db.execute(
            """SELECT id, LENGTH(content) AS content_chars,
                      LENGTH(CAST(content AS BLOB)) AS content_bytes
               FROM messages
               WHERE session_id=? AND role IN ('user','assistant') AND id>=?
               ORDER BY id ASC LIMIT ?""",
            (session_id, anchor, row_limit + 1),
        ).fetchall()]
        anchor_missing = bool(rows and int(rows[0]["id"]) != anchor)
        overflow_count = len(rows) > row_limit
        clipped_sizes = [
            _context_row_size(
                row,
                per_message_chars=per_message,
                per_message_tokens=per_message_tokens,
            )
            for row in rows
        ]
        overflow_chars = sum(item[0] for item in clipped_sizes[:row_limit]) > budget
        overflow_tokens = sum(item[1] for item in clipped_sizes[:row_limit]) > token_budget
        no_rows_but_messages = not rows and bool(db.execute(
            """SELECT 1 FROM messages
               WHERE session_id=? AND role IN ('user','assistant') LIMIT 1""",
            (session_id,),
        ).fetchone())
        if (
            anchor_missing or overflow_count or overflow_chars
            or overflow_tokens or no_rows_but_messages
        ):
            fresh = make_fresh_plan(is_rotation=True)
            selected_ids = list(fresh.get("ids") or [])
            used_chars = int(fresh.get("used_chars") or 0)
            used_tokens = int(fresh.get("used_tokens") or 0)
            message_char_caps = dict(fresh.get("message_char_caps") or {})
        else:
            selected_ids = [int(row["id"]) for row in rows]
            used_chars = sum(item[0] for item in clipped_sizes)
            used_tokens = sum(item[1] for item in clipped_sizes)
            message_char_caps = {
                int(row["id"]): clipped_sizes[index][0]
                for index, row in enumerate(rows)
            }
            db.execute(
                "UPDATE cache_context_windows SET updated_at=datetime('now') WHERE session_id=?",
                (session_id,),
            )

    return {
        "ids": selected_ids,
        "used_chars": used_chars,
        "used_tokens": used_tokens,
        "message_char_caps": message_char_caps,
        "anchor_message_id": anchor,
        "generation": generation,
        "rotated": rotated,
        "stable": True,
        "max_chars": budget,
        "single_message_max_chars": per_message,
        "max_tokens": token_budget,
        "single_message_max_tokens": per_message_tokens,
    }


def get_session_context_window_plan(
    session_id: str,
    *,
    limit: int = 500,
    max_chars: int = 600000,
    single_message_max_chars: int = 240000,
    max_tokens: int = 158000,
    single_message_max_tokens: int = 60000,
    stable: bool | None = None,
    db=None,
) -> dict:
    """Choose provider-facing raw IDs with a cache-stable oldest message.

    The old loader always selected the newest N messages. Once N was full,
    every new turn evicted the oldest message and changed the prompt-cache
    prefix. This planner stores only an anchor message ID and rotates it in a
    deliberate jump when the hard row/character budget is genuinely exceeded.
    """
    if db is not None:
        return _session_context_window_plan_with_db(
            db, session_id, limit=limit, max_chars=max_chars,
            single_message_max_chars=single_message_max_chars,
            max_tokens=max_tokens,
            single_message_max_tokens=single_message_max_tokens,
            stable=stable,
        )
    with get_db() as owned_db:
        return _session_context_window_plan_with_db(
            owned_db, session_id, limit=limit, max_chars=max_chars,
            single_message_max_chars=single_message_max_chars,
            max_tokens=max_tokens,
            single_message_max_tokens=single_message_max_tokens,
            stable=stable,
        )

def get_session_context_messages(
    session_id: str,
    *,
    limit: int = 500,
    max_chars: int = 600000,
    single_message_max_chars: int = 240000,
    max_tokens: int = 158000,
    single_message_max_tokens: int = 60000,
) -> dict:
    """Load the provider-facing window without materialising oversized sources."""
    row_limit = max(1, min(int(limit), 500))
    budget = max(1, int(max_chars))
    per_message = max(1, min(int(single_message_max_chars), budget))
    token_budget = max(1, int(max_tokens))
    per_message_tokens = max(1, min(int(single_message_max_tokens), token_budget))
    plan = get_session_context_window_plan(
        session_id,
        limit=row_limit,
        max_chars=budget,
        single_message_max_chars=per_message,
        max_tokens=token_budget,
        single_message_max_tokens=per_message_tokens,
    )
    selected_ids = [int(value) for value in plan.get("ids") or []]
    selected_source_chars = 0
    message_char_caps = {
        int(key): max(1, int(value))
        for key, value in (plan.get("message_char_caps") or {}).items()
    }
    with get_db() as db:
        loaded: list[dict] = []
        if selected_ids:
            marks = ",".join("?" for _ in selected_ids)
            piece_chars = max(1, (per_message + 1) // 2)
            rows = db.execute(
                f"""SELECT id, session_id, role,
                            CASE WHEN LENGTH(content)>? THEN SUBSTR(content, 1, ?)
                                 ELSE content END AS content_head,
                            CASE WHEN LENGTH(content)>? THEN SUBSTR(content, -?)
                                 ELSE '' END AS content_tail,
                            LENGTH(content) AS context_original_chars,
                            created_at, input_tokens, output_tokens,
                            cache_read, cache_creation, cost, provider, model, metadata
                     FROM messages
                     WHERE session_id=? AND id IN ({marks})
                     ORDER BY id ASC""",
                [
                    per_message, piece_chars, per_message, piece_chars,
                    session_id, *selected_ids,
                ],
            ).fetchall()
            loaded = [dict(row) for row in rows]
            selected_source_chars = sum(
                max(0, int(row.get("context_original_chars") or 0))
                for row in loaded
            )
        total_visible = int(db.execute(
            """SELECT COUNT(*) FROM messages
               WHERE session_id=? AND role IN ('user','assistant')""",
            (session_id,),
        ).fetchone()[0])
        candidate = db.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(content_chars), 0) AS chars
               FROM (
                   SELECT LENGTH(content) AS content_chars FROM messages
                   WHERE session_id=? AND role IN ('user','assistant')
                   ORDER BY id DESC LIMIT ?
               )""",
            (session_id, row_limit),
        ).fetchone()
        candidate_messages = int(candidate["n"] or 0)
        candidate_source_chars = int(candidate["chars"] or 0)

    chosen: list[dict] = []
    truncated = 0
    actual_used = 0
    for item in loaded:
        original_chars = max(0, int(item.pop("context_original_chars") or 0))
        head = str(item.pop("content_head") or "")
        tail = str(item.pop("content_tail") or "")
        message_cap = min(
            per_message,
            message_char_caps.get(int(item.get("id") or 0), per_message),
        )
        content, was_truncated = _clip_context_message_parts(
            head, tail, original_chars=original_chars, max_chars=message_cap,
        )
        item["content"] = content
        raw_metadata = item.get("metadata")
        if isinstance(raw_metadata, str):
            try:
                item["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
            except Exception:
                item["metadata"] = {}
        elif not isinstance(raw_metadata, dict):
            item["metadata"] = {}
        if was_truncated:
            item["_context_truncated"] = True
            item["_context_original_chars"] = original_chars
            truncated += 1
        chosen.append(item)
        actual_used += len(content)

    return {
        "items": chosen,
        "stats": {
            "candidate_messages": candidate_messages,
            "selected_messages": len(chosen),
            "dropped_messages": max(0, candidate_messages - len(chosen)),
            "truncated_messages": truncated,
            "selected_chars": actual_used,
            "selected_tokens_estimate": int(plan.get("used_tokens") or 0),
            "selected_source_chars": selected_source_chars,
            "candidate_source_chars": candidate_source_chars,
            "max_chars": budget,
            "max_tokens": token_budget,
            "single_message_max_chars": per_message,
            "single_message_max_tokens": per_message_tokens,
            "oldest_selected_id": int(chosen[0].get("id") or 0) if chosen else 0,
            "newest_selected_id": int(chosen[-1].get("id") or 0) if chosen else 0,
            "cache_window_anchor_id": int(plan.get("anchor_message_id") or 0),
            "cache_window_generation": int(plan.get("generation") or 0),
            "cache_window_rotated": bool(plan.get("rotated")),
            "cache_window_stable": bool(plan.get("stable")),
        },
    }


def get_session_message_page(
    session_id: str,
    *,
    limit: int = 80,
    before_id: int | None = None,
    oldest: bool = False,
    position: int | None = None,
) -> dict:
    """Return one display-order page with truthful global history positions.

    Positions are one-based within the complete session.  ``oldest`` jumps
    directly to the first page, while ``position`` centres a page around a
    requested global position.  This lets the browser reach a 50,000-message
    import without issuing hundreds of sequential requests and lets it render
    a real whole-conversation progress indicator.
    """
    limit = max(1, min(int(limit), 500))
    requested_position = int(position) if position is not None else None
    with get_db() as db:
        # Keep the count, selected rows and their ranks on one read snapshot.
        db.execute("BEGIN")
        total_count = int(db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0])

        if total_count <= 0:
            return {
                "items": [],
                "oldest_id": 0,
                "newest_id": 0,
                "has_more": False,
                "has_newer": False,
                "total_count": 0,
                "start_position": 0,
                "end_position": 0,
                "requested_position": 0,
            }

        max_start_offset = max(0, total_count - limit)
        if oldest:
            start_offset = 0
            requested_position = 1
        elif requested_position is not None:
            requested_position = max(1, min(requested_position, total_count))
            start_offset = max(0, requested_position - 1 - (limit // 2))
            start_offset = min(start_offset, max_start_offset)
        elif before_id is not None and int(before_id) > 0:
            eligible_count = int(db.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND id<?",
                (session_id, int(before_id)),
            ).fetchone()[0])
            start_offset = max(0, eligible_count - limit)
        else:
            start_offset = max_start_offset

        rows = db.execute(
            """SELECT m.*,
                      EXISTS(
                        SELECT 1 FROM message_favorites f
                        WHERE f.message_id=m.id
                      ) AS is_favorite
               FROM messages m
               WHERE m.session_id=?
               ORDER BY m.id ASC LIMIT ? OFFSET ?""",
            (session_id, limit, start_offset),
        ).fetchall()
        items = []
        for index, row in enumerate(rows):
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except Exception:
                item["metadata"] = {}
            item["history_position"] = start_offset + index + 1
            items.append(item)

        start_position = int(items[0]["history_position"]) if items else 0
        end_position = int(items[-1]["history_position"]) if items else 0
        return {
            "items": items,
            "oldest_id": int(items[0]["id"]) if items else 0,
            "newest_id": int(items[-1]["id"]) if items else 0,
            "has_more": start_position > 1,
            "has_newer": bool(end_position and end_position < total_count),
            "total_count": total_count,
            "start_position": start_position,
            "end_position": end_position,
            "requested_position": requested_position or end_position,
        }


def get_session_messages_after(
    session_id: str, *, after_id: int = 0, limit: int = 100,
) -> list[dict]:
    """Return newly persisted messages for a live window in stable ID order."""
    limit = max(1, min(int(limit), 500))
    with get_db() as db:
        rows = db.execute(
            """SELECT m.*,
                      EXISTS(
                        SELECT 1 FROM message_favorites f
                        WHERE f.message_id=m.id
                      ) AS is_favorite
               FROM messages m
               WHERE m.session_id=? AND m.id>?
               ORDER BY m.id ASC LIMIT ?""",
            (session_id, max(0, int(after_id)), limit),
        ).fetchall()
        start_position = 0
        if rows:
            start_position = int(db.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND id<?",
                (session_id, int(rows[0]["id"])),
            ).fetchone()[0]) + 1
    result: list[dict] = []
    for index, row in enumerate(rows):
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except Exception:
            item["metadata"] = {}
        item["history_position"] = start_position + index
        result.append(item)
    return result


def get_sessions(limit: int = 50, offset: int = 0, query: str = "") -> list:
    with get_db() as db:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        text = str(query or "").strip()
        if text:
            rows = db.execute(
                """SELECT * FROM sessions WHERE title LIKE ?
                   ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?""",
                (f"%{text}%", limit, offset),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM sessions
                   ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def get_session(session_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def rename_session(session_id: str, new_title: str):
    with get_db() as db:
        db.execute(
            "UPDATE sessions SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (new_title, session_id)
        )


def branch_session_before_message(message_id: int) -> dict:
    """Create an independent branch containing everything before one user turn.

    The source window remains untouched.  The edited user text is sent through
    the normal chat endpoint afterwards, so it receives ordinary memory,
    attachment and provider handling instead of becoming a special message.
    """
    with get_db() as db:
        target = db.execute(
            """SELECT m.*, s.title AS session_title, s.provider AS session_provider,
                      s.model AS session_model
               FROM messages m JOIN sessions s ON s.id=m.session_id
               WHERE m.id=?""",
            (int(message_id),),
        ).fetchone()
        if not target:
            raise ValueError("没有找到要编辑的消息")
        if target["role"] != "user":
            raise ValueError("只能从用户消息处建立新分支")
        source_session_id = str(target["session_id"])
        new_session_id = str(uuid.uuid4())
        source_title = str(target["session_title"] or "新对话")
        branch_title = source_title if source_title.endswith("· 分支") else f"{source_title} · 分支"
        prefix = db.execute(
            """SELECT * FROM messages
               WHERE session_id=? AND id<? ORDER BY id ASC""",
            (source_session_id, int(message_id)),
        ).fetchall()
        created_at = prefix[0]["created_at"] if prefix else datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO sessions
               (id, title, created_at, updated_at, provider, model, total_cost, message_count)
               VALUES (?, ?, ?, datetime('now'), ?, ?, 0, ?)""",
            (
                new_session_id, branch_title[:120], created_at,
                target["session_provider"] or target["provider"] or "",
                target["session_model"] or target["model"] or "",
                len(prefix),
            ),
        )
        for source in prefix:
            try:
                metadata = json.loads(source["metadata"] or "{}")
            except Exception:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            for key in ("thinking_available", "thinking_chars", "thinking_provider", "thinking_label"):
                metadata.pop(key, None)
            metadata.update({
                "branch_copy": True,
                "branch_copy_of": int(source["id"]),
                "branch_source_session_id": source_session_id,
            })
            metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
            db.execute(
                """INSERT INTO messages
                   (session_id, role, content, created_at, provider, model, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_session_id, source["role"], source["content"],
                    source["created_at"], source["provider"], source["model"],
                    metadata_json,
                ),
            )
        try:
            target_metadata = json.loads(target["metadata"] or "{}")
        except Exception:
            target_metadata = {}
        return {
            "session_id": new_session_id,
            "source_session_id": source_session_id,
            "source_message_id": int(message_id),
            "title": branch_title[:120],
            "copied_messages": len(prefix),
            "original_content": str(target["content"] or ""),
            "original_metadata": target_metadata if isinstance(target_metadata, dict) else {},
        }


def delete_session(session_id: str) -> bool:
    """Delete one conversation and its per-session evidence/telemetry.

    Derived long-term memories are deliberately retained: a conversation can
    be removed from the chat UI without silently erasing the companion's memory
    graph. Memory management remains an explicit, separate action.
    """
    with get_db() as db:
        exists = db.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not exists:
            return False
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # Tables added by v5.8/v5.9 are available before HTTP routes through
        # the application lifespan, but this function also tolerates an older
        # database or a direct maintenance-script call before those migrations.
        # Raw conversation text follows the user's delete action.  The tiny,
        # exact quote already attached to a retained long-term memory remains
        # as a provenance tombstone, so that memory never becomes an
        # untraceable model-written claim after its source conversation is gone.
        for table in (
            "memory_recall_log", "raw_archive",
            "style_audits", "interaction_settlements", "token_stats",
            "proactive_messages", "reasoning_traces",
            "context_chapter_sources", "context_chapters", "context_compaction_state",
            "co_presence_events", "co_presence_state", "co_presence_rhythm",
            "co_presence_log", "persona_context_gate_state",
            "relational_honesty_audits", "chat_requests",
        ):
            if table in tables:
                db.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return True


def save_memory(content: str, source: str = "chat",
                 category: str = "general", importance: float = 0.5,
                 embedding: bytes = None) -> int:
    with get_db() as db:
        cursor = db.execute(
            """INSERT INTO memories (content, source, category, importance, embedding)
            VALUES (?, ?, ?, ?, ?)""",
            (content, source, category, importance, embedding)
        )
        return cursor.lastrowid


def search_memories(keyword: str = "", limit: int = 10) -> list:
    with get_db() as db:
        if keyword:
            rows = db.execute(
                """SELECT * FROM memories WHERE content LIKE ? 
                ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (f"%{keyword}%", limit)
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM memories 
                ORDER BY importance DESC, updated_at DESC LIMIT ?""",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def record_api_call(
    *,
    session_id: str | None,
    provider: str,
    model: str,
    purpose: str,
    usage: dict | None,
) -> int:
    """Persist one gateway operation without storing prompt/response text."""
    usage = usage if isinstance(usage, dict) else {}
    try:
        upstream_requests = max(1, int(usage.get("upstream_requests") or 1))
    except (TypeError, ValueError):
        upstream_requests = 1

    def _int(name: str) -> int:
        try:
            return max(0, int(usage.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    def _text(name: str, limit: int = 160) -> str:
        return str(usage.get(name) or "")[:limit]

    try:
        cost = max(0.0, float(usage.get("cost") or 0.0))
    except (TypeError, ValueError):
        cost = 0.0
    price_snapshot = usage.get("price_snapshot")
    if not isinstance(price_snapshot, dict):
        price_snapshot = {}
    with get_db() as db:
        cur = db.execute(
            """INSERT INTO api_call_stats(
                 session_id, provider, model, purpose, upstream_requests,
                 input_tokens, output_tokens, reasoning_tokens, cache_read,
                 cache_creation, cost, cost_source, price_snapshot,
                 cache_creation_1h, cache_creation_5m, generation_id,
                 actual_provider, actual_model, router_region, router_strategy,
                 cache_prefix_hash, cache_parent_hash, cache_shape_hash, cache_ttl, cache_breakpoint,
                 cache_guard_status, cache_prefix_chars,
                 cache_prefix_tokens_estimate, cache_min_tokens, cache_strategy,
                 cache_control_count, cache_tools_hash, cache_segment_hashes,
                 cache_fingerprint, cache_last_touch_at, cache_next_warm_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(session_id or "") or None,
                str(provider or ""),
                str(model or ""),
                str(purpose or "unspecified")[:80],
                upstream_requests,
                _int("input_tokens"), _int("output_tokens"),
                _int("reasoning_tokens"), _int("cache_read"),
                _int("cache_creation"), cost,
                str(usage.get("cost_source") or ""),
                json.dumps(price_snapshot, ensure_ascii=False),
                _int("cache_creation_1h"), _int("cache_creation_5m"),
                _text("generation_id"), _text("actual_provider"),
                _text("actual_model"), _text("router_region"),
                _text("router_strategy"), _text("cache_prefix_hash", 80),
                _text("cache_parent_hash", 80), _text("cache_shape_hash", 80), _text("cache_ttl", 16),
                _text("cache_breakpoint", 80),
                _text("cache_guard_status", 80) or _text("history_cache_guard", 80),
                _int("cache_prefix_chars"), _int("cache_prefix_tokens_estimate"),
                _int("cache_min_tokens"), _text("cache_strategy", 120),
                _int("cache_control_count"), _text("cache_tools_hash", 80),
                _text("cache_segment_hashes", 500), _text("cache_fingerprint", 80),
                float(usage.get("cache_last_touch_at") or 0.0),
                float(usage.get("cache_next_warm_at") or 0.0),
            ),
        )
        return int(cur.lastrowid or 0)



def enrich_cache_diagnostic_rows(rows: list[dict]) -> list[dict]:
    """Classify recent Claude cache audit rows without reading prompt text.

    ``rows`` must be newest-first (the same order returned by the console SQL).
    The result preserves that order.  Provider/model comparisons remember the
    last non-empty routing metadata because OpenRouter cache replays may omit
    router metadata even though the cache hit is valid.
    """
    raw = [dict(row) for row in rows]
    previous_by_session: dict[str, dict] = {}
    last_route_by_session: dict[str, dict] = {}
    enriched_oldest_first: list[dict] = []

    for item in reversed(raw):
        for key in (
            "cache_read", "cache_creation", "cache_creation_1h",
            "cache_creation_5m", "cache_prefix_chars", "cache_min_tokens",
            "cache_prefix_tokens_estimate",
        ):
            try:
                item[key] = int(item.get(key) or 0)
            except (TypeError, ValueError):
                item[key] = 0
        item["cache_event"] = (
            "read" if item["cache_read"] > 0
            else "write" if item["cache_creation"] > 0
            else "none"
        )
        # Main chat and tool-loop requests can have different tool/system
        # shapes even inside one visible chat. Compare continuity only within
        # the same transport purpose lane.
        raw_session_key = str(item.get("session_id") or "")
        session_key = (
            "|".join((
                raw_session_key,
                str(item.get("purpose") or "unspecified"),
            ))
            if raw_session_key else ""
        )
        previous = previous_by_session.get(session_key) if session_key else None
        last_route = last_route_by_session.get(session_key) if session_key else None
        parent = str(item.get("cache_parent_hash") or "")
        previous_prefix = str((previous or {}).get("cache_prefix_hash") or "")
        continuity = bool(parent and previous_prefix and parent == previous_prefix)
        item["cache_parent_continuity"] = continuity

        current_model = str(item.get("actual_model") or "")
        current_provider = str(item.get("actual_provider") or "")
        last_model = str((last_route or {}).get("actual_model") or "")
        last_provider = str((last_route or {}).get("actual_provider") or "")

        model_changed = bool(current_model and last_model and current_model != last_model)
        provider_changed = bool(current_provider and last_provider and current_provider != last_provider)
        item["cache_model_changed"] = model_changed
        item["cache_provider_changed"] = provider_changed
        item["cache_route_changes"] = [
            name for name, changed in (("model_changed", model_changed), ("provider_changed", provider_changed))
            if changed
        ]

        if item["cache_event"] == "read":
            diagnosis = "cache_hit"
        elif model_changed:
            diagnosis = "model_changed"
        elif provider_changed:
            diagnosis = "provider_changed"
        elif previous and str(item.get("cache_shape_hash") or "") != str(previous.get("cache_shape_hash") or ""):
            diagnosis = "request_shape_changed"
        elif parent and previous_prefix and not continuity:
            diagnosis = "history_prefix_broke"
        elif item["cache_event"] == "write":
            diagnosis = "cold_write_or_expired"
        else:
            diagnosis = "no_cache_tokens"
        item["cache_diagnosis"] = diagnosis

        if session_key:
            previous_by_session[session_key] = item
            if current_model or current_provider:
                merged_route = dict(last_route or {})
                if current_model:
                    merged_route["actual_model"] = current_model
                if current_provider:
                    merged_route["actual_provider"] = current_provider
                last_route_by_session[session_key] = merged_route
        enriched_oldest_first.append(item)

    return list(reversed(enriched_oldest_first))

def get_token_stats(session_id: str = None) -> dict:
    """获取 token 统计（按 session 或全局）"""
    with get_db() as db:
        if session_id:
            row = db.execute(
                """SELECT 
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(reasoning_tokens) as total_reasoning,
                    SUM(cache_read) as total_cache_read,
                    SUM(cache_creation) as total_cache_creation,
                    SUM(cost) as total_cost,
                    COUNT(*) as total_messages
                FROM token_stats WHERE session_id = ?""",
                (session_id,)
            ).fetchone()
        else:
            row = db.execute(
                """SELECT 
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(reasoning_tokens) as total_reasoning,
                    SUM(cache_read) as total_cache_read,
                    SUM(cache_creation) as total_cache_creation,
                    SUM(cost) as total_cost,
                    COUNT(*) as total_messages
                FROM token_stats"""
            ).fetchone()
        return dict(row) if row else {}


def get_detailed_stats(session_id: str) -> list:
    """获取 session 内每条消息的详细费用（含历史价格快照）"""
    with get_db() as db:
        rows = db.execute(
            """SELECT ts.*, m.role, m.content 
            FROM token_stats ts
            LEFT JOIN messages m ON ts.message_id = m.id
            WHERE ts.session_id = ?
            ORDER BY ts.created_at ASC""",
            (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_screen_event(event_type: str, app_name: str = "",
                      duration: int = 0) -> int:
    with get_db() as db:
        cursor = db.execute(
            """INSERT INTO screen_time (event_type, app_name, duration_seconds)
            VALUES (?, ?, ?)""",
            (event_type, app_name, duration)
        )
        return cursor.lastrowid


def get_recent_screen_time(hours: int = 24) -> list:
    with get_db() as db:
        rows = db.execute(
            """SELECT * FROM screen_time 
            WHERE timestamp > datetime('now', ?)
            ORDER BY timestamp DESC""",
            (f"-{hours} hours",)
        ).fetchall()
        return [dict(r) for r in rows]


# 恢复包只在下一次进程启动、任何 SQLite schema 初始化之前生效。
# 这样不会在聊天请求或定时任务仍持有连接时替换数据库文件。
try:
    from local_restore_runtime import apply_pending_restore

    _restore_boot_result = apply_pending_restore(
        Path(DB_PATH).resolve().parent,
        Path(DB_PATH).name,
    )
    if _restore_boot_result and not _restore_boot_result.get("failed"):
        print("[Models] 本机数据恢复已在启动前完成")
    elif _restore_boot_result and _restore_boot_result.get("failed"):
        print("[Models] 本机数据恢复失败，已保留失败包与回滚包")
except Exception as _restore_boot_error:
    print(f"[Models] 启动前恢复检查跳过: {type(_restore_boot_error).__name__}")

# 启动时初始化
init_db()


def _ensure_memory_session_scope():
    """Add session ownership to older databases without importing any old files.

    Existing unscoped memories intentionally stay NULL: chat recall never treats
    them as belonging to a newly opened window. New chat memories are written
    with their session_id and can only be recalled from that same window.
    """
    with get_db() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(memories)").fetchall()]
        if "session_id" not in cols:
            db.execute("ALTER TABLE memories ADD COLUMN session_id TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(session_id)")


try:
    _ensure_memory_session_scope()
except Exception as _e:
    print(f"[Models] 会话记忆隔离字段初始化跳过: {_e}")


# ━━━ v5.2 迁移：pin 字段（置顶不衰减不归档）━━━
def _migrate_v52():
    with get_db() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(memories)").fetchall()]
        if "is_pinned" not in cols:
            db.execute("ALTER TABLE memories ADD COLUMN is_pinned INTEGER DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS idx_mem_pinned ON memories(is_pinned)")
try:
    _migrate_v52()
except Exception as _e:
    print(f"[Models] v5.2迁移跳过: {_e}")
