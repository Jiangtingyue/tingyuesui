"""
遗忘曲线引擎
改进版艾宾浩斯：情绪强度(arousal)越高衰减越慢，检索次数越多越难忘
未解决的记忆权重更高，已解决的沉底等关键词唤醒
"""
import math
from datetime import datetime, timezone
from models import get_db


# 衰减参数
DECAY_LAMBDA = 0.05        # 衰减速率，越大忘得越快
ARCHIVE_THRESHOLD = 0.3    # 低于这个分数 → 归档
AROUSAL_BASE = 0.4         # arousal 基础权重
AROUSAL_BOOST = 0.6        # arousal 增益系数
RESOLVED_PENALTY = 0.05    # 已解决记忆的权重惩罚（降到5%）


def calc_decay_score(importance: float, activation_count: int,
                     days_since_last: float, arousal: float,
                     resolved: bool) -> float:
    """
    核心衰减公式（来自 Ombre Brain）：
    Score = importance × activation_count^0.3 × e^(-λ × days)
            × (base + arousal × boost)

    - importance: 0-1，记忆重要性
    - activation_count: 被检索/浮现的次数
    - days_since_last: 距上次激活的天数
    - arousal: 唤醒度 0-1，越高越难忘
    - resolved: 是否已解决，已解决的沉底
    """
    # 激活次数加权（常被想起 = 更难忘）
    activation_factor = max(1, activation_count) ** 0.3

    # 时间衰减（指数衰减）
    time_decay = math.exp(-DECAY_LAMBDA * days_since_last)

    # 情绪强度抗衰减
    emotion_factor = AROUSAL_BASE + arousal * AROUSAL_BOOST

    score = importance * activation_factor * time_decay * emotion_factor

    # 已解决 → 权重砍到 5%
    if resolved:
        score *= RESOLVED_PENALTY

    return round(min(score, 10.0), 4)


def run_decay_cycle():
    """
    跑一次全局衰减
    定时任务调用（建议每小时或每天跑一次）
    更新所有记忆的 decay_score，归档低分记忆
    """
    now = datetime.now(timezone.utc)
    updated = 0
    archived = 0
    pinned_skipped = 0

    with get_db() as db:
        rows = db.execute(
            """SELECT id, importance, activation_count, last_activated_at,
                      created_at, updated_at, arousal, resolved, is_pinned
               FROM memories
               WHERE archived = 0"""
        ).fetchall()

        for row in rows:
            # 置顶是使用者的明确决定：既不衰减，也不归档。
            if bool(row["is_pinned"]):
                pinned_skipped += 1
                continue

            # 新写入但尚未被召回的记忆，从创建时间开始计时。旧逻辑把它
            # 直接当作“30 天未激活”，会在第一次定时任务中立刻归档。
            timestamp = (
                row["last_activated_at"]
                or row["created_at"]
                or row["updated_at"]
            )
            try:
                last_dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                days = max(0.0, (now - last_dt.astimezone(timezone.utc)).total_seconds() / 86400)
            except (TypeError, ValueError):
                days = 0.0

            score = calc_decay_score(
                importance=0.5 if row["importance"] is None else row["importance"],
                activation_count=0 if row["activation_count"] is None else row["activation_count"],
                days_since_last=days,
                arousal=0.5 if row["arousal"] is None else row["arousal"],
                resolved=bool(row["resolved"]),
            )

            # 更新分数
            db.execute(
                "UPDATE memories SET decay_score = ? WHERE id = ?",
                (score, row["id"])
            )
            updated += 1

            # 低于阈值 → 归档
            if score < ARCHIVE_THRESHOLD:
                db.execute(
                    "UPDATE memories SET archived = 1 WHERE id = ?",
                    (row["id"],)
                )
                archived += 1

    return {
        "updated": updated,
        "archived": archived,
        "pinned_skipped": pinned_skipped,
    }


def activate_memory(memory_id: int):
    """
    记忆被检索/浮现时调用
    增加激活次数 + 刷新最后激活时间 → 抗衰减
    """
    with get_db() as db:
        db.execute(
            """UPDATE memories SET 
               activation_count = activation_count + 1,
               last_activated_at = datetime('now'),
               updated_at = datetime('now')
            WHERE id = ?""",
            (memory_id,)
        )


def get_surfacing_memories(
    limit: int = 5, *, include_assistant: bool = False,
    session_id: str | None = None,
) -> list:
    """Return high-weight unresolved memories, optionally within one window."""
    with get_db() as db:
        clauses = ["archived = 0", "resolved = 0"]
        params: list = []
        if not include_assistant:
            clauses.append("source NOT LIKE 'chat_assistant%'")
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        params.append(limit)
        rows = db.execute(
            f"""SELECT id, content, source, category, valence, arousal,
                       importance, decay_score, resolved, domain
                FROM memories
                WHERE {' AND '.join(clauses)}
                ORDER BY
                  CASE
                    WHEN source = 'manual' THEN 0
                    WHEN source = 'merge' THEN 1
                    WHEN source LIKE 'chat_user%' THEN 2
                    ELSE 3
                  END,
                  decay_score DESC
                LIMIT ?""",
            params,
        ).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            # 在当前事务中完成激活，避免为每条浮现记忆再开一个连接。
            db.execute(
                """UPDATE memories SET
                     activation_count = activation_count + 1,
                     last_activated_at = datetime('now'),
                     updated_at = datetime('now')
                   WHERE id = ?""",
                (r["id"],),
            )
            results.append(r)

        return results


def resolve_memory(memory_id: int):
    """标记记忆为已解决 → 权重降到5%"""
    with get_db() as db:
        db.execute(
            "UPDATE memories SET resolved = 1, updated_at = datetime('now') WHERE id = ?",
            (memory_id,)
        )
