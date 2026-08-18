"""
记忆优化模块 v5
1. 矛盾检测 — 新记忆跟旧记忆冲突时自动标记+更新
2. 实体关系图谱 — 记忆之间的显式关联（因果/矛盾/演化）
3. jieba 中文分词 FTS5 — 替换 unicode61，中文搜索质量飞升
"""
import json
import re
import math
import httpx
from collections import Counter
from providers.http_client import make_async_http_client
import numpy as np
from datetime import datetime

from config import (
    ACTIVE_PROVIDER, MEMORY_CONFIG, PROVIDERS, VECTOR_CONFIG, get_active_model,
)
from models import get_db
from relational_honesty import relational_honesty_guard

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  数据库 Schema 补丁
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCHEMA_V5_SQL = """
-- 记忆关系边表（实体图谱）
CREATE TABLE IF NOT EXISTS memory_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    relation    TEXT NOT NULL,
    confidence  REAL DEFAULT 0.8,
    created_at  TEXT DEFAULT (datetime('now')),
    metadata    TEXT DEFAULT '{}',
    FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_edge_source ON memory_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edge_target ON memory_edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edge_relation ON memory_edges(relation);

-- 矛盾记录表
CREATE TABLE IF NOT EXISTS contradictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    old_memory_id INTEGER NOT NULL,
    new_memory_id INTEGER NOT NULL,
    old_content TEXT NOT NULL,
    new_content TEXT NOT NULL,
    resolution  TEXT DEFAULT 'pending',
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (old_memory_id) REFERENCES memories(id),
    FOREIGN KEY (new_memory_id) REFERENCES memories(id)
);
"""


def init_v5_schema():
    """初始化 v5 表结构"""
    with get_db() as db:
        db.executescript(SCHEMA_V5_SQL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. 矛盾检测
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 矛盾关键词对（简单但有效的本地检测）
CONTRADICTION_PATTERNS = [
    # (旧记忆含A, 新记忆含B) → 可能矛盾
    ("喜欢", "不喜欢"),
    ("不喜欢", "喜欢"),
    ("讨厌", "喜欢"),
    ("喜欢", "讨厌"),
    ("是", "不是"),
    ("不是", "是"),
    ("有", "没有"),
    ("没有", "有"),
    ("在", "不在"),
    ("不在", "在"),
    ("会", "不会"),
    ("不会", "会"),
    ("想", "不想"),
    ("不想", "想"),
    ("好", "不好"),
    ("能", "不能"),
]


class ContradictionDetector:
    """
    矛盾检测器
    新记忆写入时，跟语义相近的旧记忆做比对
    发现矛盾 → 标记旧记忆为 superseded，建立"演化自"关系
    """

    def __init__(self):
        self._client = make_async_http_client(timeout=20)

    async def check_and_resolve(self, new_content: str,
                                 new_memory_id: int,
                                 similar_memories: list[dict]) -> list[dict]:
        """
        检查新记忆是否跟相似的旧记忆矛盾
        返回矛盾列表
        """
        if not similar_memories:
            return []

        contradictions = []

        # 先用本地规则快速筛
        candidates = []
        for old_mem in similar_memories:
            if old_mem["id"] == new_memory_id:
                continue
            if self._local_contradiction_check(old_mem["content"], new_content):
                candidates.append(old_mem)

        if not candidates:
            return []

        # 有候选的话用 API 精确判断
        for old_mem in candidates[:5]:  # 最多检查5条
            is_contradiction = await self._api_check(
                old_mem["content"], new_content
            )
            if is_contradiction:
                contradiction = self._record_contradiction(
                    old_mem, new_memory_id, new_content
                )
                contradictions.append(contradiction)

        return contradictions

    def _local_contradiction_check(self, old: str, new: str) -> bool:
        """本地关键词对快速检查"""
        old_lower = old.lower()
        new_lower = new.lower()

        for pattern_old, pattern_new in CONTRADICTION_PATTERNS:
            if pattern_old in old_lower and pattern_new in new_lower:
                # 还需要确认是在说同一件事（有共同词）
                old_words = set(old_lower)
                new_words = set(new_lower)
                overlap = len(old_words & new_words)
                if overlap > len(old_words) * 0.3:
                    return True
        return False

    async def _api_check(self, old_content: str, new_content: str) -> bool:
        """Use the cheap auxiliary route for optional contradiction confirmation."""
        if not MEMORY_CONFIG.get("use_remote_enrichment", False):
            return False
        from api_cost import auxiliary_chat

        prompt = f"""判断以下两条记忆是否矛盾（信息冲突）。
只回答 true 或 false，不要其他内容。

旧记忆：{old_content[:500]}
新记忆：{new_content[:500]}

如果两者描述的是同一件事但结论/事实相反，回答 true。
如果只是补充信息或话题不同，回答 false。"""
        try:
            result = await auxiliary_chat(
                messages=[{"role": "user", "content": prompt}],
                purpose="memory_contradiction",
                max_output_tokens=64,
            )
            if not result:
                return False
            text = str(result.get("content") or "").strip().lower()
            return text.startswith("true")
        except Exception:
            return False

    def _record_contradiction(self, old_mem: dict, new_id: int,
                               new_content: str) -> dict:
        """记录矛盾 + 建立"演化自"关系 + 标记旧记忆"""
        old_id = old_mem["id"]

        with get_db() as db:
            # 写矛盾记录
            db.execute(
                """INSERT INTO contradictions
                (old_memory_id, new_memory_id, old_content, new_content, resolution)
                VALUES (?, ?, ?, ?, 'auto_superseded')""",
                (old_id, new_id, old_mem["content"][:500], new_content[:500])
            )

            # 建立"演化自"关系
            db.execute(
                """INSERT OR IGNORE INTO memory_edges
                (source_id, target_id, relation, confidence)
                VALUES (?, ?, 'supersedes', 0.9)""",
                (new_id, old_id)
            )

            # 降低旧记忆的重要性（不删除，留着做历史参考）
            db.execute(
                """UPDATE memories SET
                importance = importance * 0.3,
                resolved = 1,
                updated_at = datetime('now')
                WHERE id = ?""",
                (old_id,)
            )

        return {
            "old_id": old_id,
            "new_id": new_id,
            "old_content": old_mem["content"][:200],
            "new_content": new_content[:200],
            "resolution": "auto_superseded",
        }

    async def close(self):
        await self._client.aclose()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. 实体关系图谱
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 支持的关系类型
RELATION_TYPES = [
    "supersedes",    # A 取代了 B（矛盾检测自动建）
    "evolves_from",  # A 从 B 演化来（观点变化）
    "causes",        # A 导致了 B
    "contradicts",   # A 跟 B 矛盾（但都没被取代）
    "related",       # 一般性关联
    "part_of",       # A 是 B 的一部分
]


class MemoryGraph:
    """
    实体关系图谱
    记忆之间的显式关联，搜索时关联记忆一起出现
    """

    def add_edge(self, source_id: int, target_id: int,
                 relation: str, confidence: float = 0.8) -> bool:
        """添加一条关系边"""
        if relation not in RELATION_TYPES:
            return False
        if source_id == target_id:
            return False

        with get_db() as db:
            try:
                db.execute(
                    """INSERT OR IGNORE INTO memory_edges
                    (source_id, target_id, relation, confidence)
                    VALUES (?, ?, ?, ?)""",
                    (source_id, target_id, relation, confidence)
                )
                return True
            except Exception:
                return False

    def get_connected(self, memory_id: int, depth: int = 1) -> list[dict]:
        """
        获取与某条记忆关联的所有记忆（BFS遍历）
        depth=1 只看直接关联，depth=2 看二跳
        """
        visited = set()
        result = []
        queue = [(memory_id, 0)]

        while queue:
            current_id, current_depth = queue.pop(0)
            if current_id in visited or current_depth > depth:
                continue
            visited.add(current_id)

            with get_db() as db:
                # 出边
                outgoing = db.execute(
                    """SELECT e.target_id as connected_id, e.relation,
                              e.confidence, m.content, m.tags, m.domain
                       FROM memory_edges e
                       JOIN memories m ON e.target_id = m.id
                       WHERE e.source_id = ? AND m.archived = 0""",
                    (current_id,)
                ).fetchall()
                # 入边
                incoming = db.execute(
                    """SELECT e.source_id as connected_id, e.relation,
                              e.confidence, m.content, m.tags, m.domain
                       FROM memory_edges e
                       JOIN memories m ON e.source_id = m.id
                       WHERE e.target_id = ? AND m.archived = 0""",
                    (current_id,)
                ).fetchall()

            for row in list(outgoing) + list(incoming):
                conn_id = row["connected_id"]
                if conn_id not in visited:
                    result.append({
                        "id": conn_id,
                        "content": row["content"],
                        "relation": row["relation"],
                        "confidence": row["confidence"],
                        "tags": row.get("tags", ""),
                        "domain": row.get("domain", ""),
                    })
                    if current_depth + 1 <= depth:
                        queue.append((conn_id, current_depth + 1))

        return result

    def get_graph_for_memory(self, memory_id: int) -> dict:
        """获取某条记忆的完整图谱信息（用于前端展示）"""
        with get_db() as db:
            edges = db.execute(
                """SELECT source_id, target_id, relation, confidence
                   FROM memory_edges
                   WHERE source_id = ? OR target_id = ?""",
                (memory_id, memory_id)
            ).fetchall()

        connected = self.get_connected(memory_id, depth=1)

        return {
            "center_id": memory_id,
            "edges": [dict(e) for e in edges],
            "connected_memories": connected,
        }

    def auto_link_similar(self, memory_id: int,
                           similar_results: list[dict],
                           threshold: float = 0.75):
        """自动给高相似度的记忆建"related"关系"""
        for mem in similar_results:
            if mem.get("score", 0) >= threshold and mem["id"] != memory_id:
                self.add_edge(memory_id, mem["id"], "related",
                              confidence=mem["score"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. jieba 中文分词 FTS5
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def jieba_tokenize(text: str) -> str:
    """用 jieba 分词，返回空格分隔的词语（给 FTS5 用）"""
    try:
        import jieba
        words = jieba.cut_for_search(text)
        # 过滤掉单字和标点
        tokens = [w.strip() for w in words if len(w.strip()) > 1]
        return " ".join(tokens)
    except ImportError:
        # jieba 没装就直接返回原文
        return text


def init_jieba_fts5():
    """Create the jieba FTS index and rebuild only when its schema changes."""
    index_version = "jieba-fts-v2"
    with get_db() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS runtime_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts_jieba'"
        ).fetchone()
        old_sql = (row["sql"] or "") if row else ""
        broken_external_content = bool(
            row and "content=memories" in old_sql.replace(" ", "")
        )
        if broken_external_content:
            db.execute("DROP TABLE memories_fts_jieba")
            row = None

        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts_jieba
            USING fts5(content_jieba, tags, domain)
        """)
        version_row = db.execute(
            "SELECT value FROM runtime_metadata WHERE key='jieba_fts_version'"
        ).fetchone()
        if row and version_row and version_row["value"] == index_version:
            return

        db.execute("DELETE FROM memories_fts_jieba")
        rows = db.execute(
            "SELECT id, content, tags, domain FROM memories WHERE archived = 0"
        ).fetchall()
        for item in rows:
            db.execute(
                """INSERT INTO memories_fts_jieba
                   (rowid, content_jieba, tags, domain)
                   VALUES (?, ?, ?, ?)""",
                (
                    item["id"], jieba_tokenize(item["content"]),
                    item["tags"] or "", item["domain"] or "",
                ),
            )
        db.execute(
            "INSERT INTO runtime_metadata(key, value) VALUES('jieba_fts_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (index_version,),
        )


def jieba_search(
    query: str, limit: int = 10, *, session_id: str | None = None
) -> list[dict]:
    """用 jieba 分词后搜索 FTS5；聊天召回可限定在单一窗口。"""
    tokenized_query = jieba_tokenize(query)
    tokens = []
    for token in tokenized_query.split():
        token = token.strip().replace('"', '""')
        if len(token) > 1 and token not in tokens:
            tokens.append(token)
    if not tokens:
        return []
    # Default FTS syntax joins words with AND, which makes a natural Chinese
    # sentence fail unless every filler word exists in the memory.  OR keeps
    # exact token matching while allowing the important noun to surface.
    match_query = " OR ".join(f'"{token}"' for token in tokens[:32])

    try:
        with get_db() as db:
            clauses = ["memories_fts_jieba MATCH ?", "m.archived = 0"]
            params: list = [match_query]
            if session_id is not None:
                clauses.append("m.session_id = ?")
                params.append(session_id)
            params.append(limit)
            rows = db.execute(
                f"""SELECT memories_fts_jieba.rowid AS id,
                          bm25(memories_fts_jieba) AS rank
                   FROM memories_fts_jieba
                   JOIN memories m ON m.id = memories_fts_jieba.rowid
                   WHERE {' AND '.join(clauses)}
                   ORDER BY rank
                   LIMIT ?""",
                params,
            ).fetchall()

            if not rows:
                return []

            return [{
                "id": r["id"],
                # FTS5 BM25 values are only meaningful relative to the same
                # result set; reciprocal rank is stable and monotonic.
                "fts_score": round(1.0 / (1.0 + index * 0.18), 4),
                "source": "jieba_fts",
            } for index, r in enumerate(rows)]

    except Exception as e:
        print(f"[JiebaFTS] 搜索失败: {e}")
        return []


def index_single_memory_jieba(memory_id: int, content: str,
                                tags: str = "", domain: str = ""):
    """单条记忆写入 jieba FTS 索引（新消息时调用）"""
    tokenized = jieba_tokenize(content)
    try:
        with get_db() as db:
            db.execute(
                """INSERT OR REPLACE INTO memories_fts_jieba
                (rowid, content_jieba, tags, domain)
                VALUES (?, ?, ?, ?)""",
                (memory_id, tokenized, tags, domain)
            )
    except Exception as e:
        print(f"[JiebaFTS] 索引失败: {e}")


def remove_memory_jieba(memory_id: int) -> None:
    """Remove one row from the active-memory FTS index."""
    try:
        with get_db() as db:
            db.execute(
                "DELETE FROM memories_fts_jieba WHERE rowid = ?",
                (int(memory_id),),
            )
    except Exception as e:
        print(f"[JiebaFTS] 移除旧索引失败: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  初始化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 全局单例
contradiction_detector = ContradictionDetector()
memory_graph = MemoryGraph()

# 启动时初始化
init_v5_schema()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  v5.2: 合并 merge + 相似聚类（移植自记忆模块V2说明书）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def merge_memories(memory_ids: list[int], gateway) -> dict:
    """
    多条糅成一条：重复归一、独特全保留、保温度精简。
    采用后新记忆入库，原 N 条归档（不删，留安全网；任一原条置顶则新条继承）。
    返回 {preview, sources} —— 落库由调用方确认后执行（非破坏性，先预览）。
    """
    clean_ids = list(dict.fromkeys(
        int(item) for item in (memory_ids or [])
        if str(item).strip().lstrip("-").isdigit() and int(item) > 0
    ))
    if len(clean_ids) < 2:
        return {"error": "至少选两条"}

    from models import get_db
    with get_db() as db:
        rows = db.execute(
            f"""SELECT id, content, is_pinned FROM memories
                WHERE id IN ({','.join('?'*len(clean_ids))})
                  AND archived = 0""",
            clean_ids).fetchall()
    if len(rows) < 2:
        return {"error": "至少选两条"}
    originals = [dict(r) for r in rows]
    prompt = ("把下面几条记忆融合成一条：重复的归一，各自独特的细节全保留，"
              "自然糅合，保留情感温度地精简。只输出合并后的记忆正文。\n\n"
              + "\n---\n".join(r["content"] for r in originals))
    from api_cost import auxiliary_chat
    try:
        result = await auxiliary_chat(
            messages=[{"role": "user", "content": prompt}],
            purpose="memory_merge",
            system_prompt=(
                "你只做机械文本归并，不扮演聊天角色。保留所有独特事实，去重后只输出正文。"
            ),
            max_output_tokens=1200,
        )
    except Exception:
        result = None
    if not result:
        return {
            "error": "DeepSeek Flash 辅助通道当前不可用；原记忆未改动。",
            "sources": originals,
            "inherit_pin": any(r["is_pinned"] for r in originals),
        }
    result = result if isinstance(result, dict) else {}
    preview = str(result.get("content") or "").strip()
    common = {
        "sources": originals,
        "inherit_pin": any(r["is_pinned"] for r in originals),
        "usage": result.get("usage") or {},
        "auxiliary_route": result.get("_auxiliary_route") or {},
    }
    if not preview:
        return {
            **common,
            "error": "DeepSeek Flash 没有返回可用正文；原记忆未改动。",
        }
    return {**common, "preview": preview}


async def commit_merge(merged_content: str, source_ids: list[int],
                       inherit_pin: bool = False) -> int:
    """确认合并并原子重建 embedding、FTS 和原文来源链。"""
    content = str(merged_content or "").strip()
    clean_ids = list(dict.fromkeys(
        int(item) for item in (source_ids or [])
        if str(item).strip().lstrip("-").isdigit() and int(item) > 0
    ))
    if not content:
        raise ValueError("合并后的记忆不能为空")
    if len(clean_ids) < 2:
        raise ValueError("至少需要两条有效来源记忆")

    from embedding_service import embedding_service
    embedding = await embedding_service.encode(content, allow_load=True)
    embedding_bytes = (
        embedding.astype(np.float32).tobytes()
        if embedding is not None
        else None
    )

    from models import get_db
    with get_db() as db:
        placeholders = ",".join("?" * len(clean_ids))
        rows = db.execute(
            f"""SELECT id, tags, domain, importance, valence, arousal,
                       is_pinned
                FROM memories
                WHERE id IN ({placeholders}) AND archived = 0""",
            clean_ids,
        ).fetchall()
        if len(rows) < 2:
            raise ValueError("来源记忆不存在、已归档或已被合并，请刷新后重试")

        valid_ids = [row["id"] for row in rows]
        valid_placeholders = ",".join("?" * len(valid_ids))
        tags: list[str] = []
        for row in rows:
            for tag in str(row["tags"] or "").split(","):
                tag = tag.strip()
                if tag and tag not in tags:
                    tags.append(tag)
        domains = [
            str(row["domain"] or "").strip()
            for row in rows
            if str(row["domain"] or "").strip() not in {"", "other"}
        ]
        domain = Counter(domains).most_common(1)[0][0] if domains else "other"
        importance = min(
            1.0,
            max(float(row["importance"] or 0.5) for row in rows) + 0.05,
        )
        valence = sum(float(row["valence"] or 0.0) for row in rows) / len(rows)
        arousal = max(float(row["arousal"] or 0.5) for row in rows)
        pinned = bool(inherit_pin) or any(bool(row["is_pinned"]) for row in rows)
        metadata = json.dumps(
            {"merged_from": valid_ids, "provenance": "user_confirmed_merge"},
            ensure_ascii=False,
        )

        cur = db.execute(
            """INSERT INTO memories
               (content, source, category, importance, embedding, metadata,
                valence, arousal, domain, decay_score, tags,
                resolved, archived, is_pinned)
               VALUES (?, 'merge', ?, ?, ?, ?, ?, ?, ?, 1.0, ?,
                       0, 0, ?)""",
            (
                content, domain, importance, embedding_bytes, metadata,
                valence, arousal, domain, ",".join(tags),
                1 if pinned else 0,
            ),
        )
        new_id = cur.lastrowid

        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        if "memory_source_links" in tables:
            db.execute(
                f"""INSERT OR IGNORE INTO memory_source_links
                    (memory_id, session_id, message_id, exact_quote,
                     verified, created_at)
                    SELECT ?, session_id, message_id, exact_quote,
                           verified, datetime('now')
                    FROM memory_source_links
                    WHERE memory_id IN ({valid_placeholders})""",
                [new_id, *valid_ids],
            )
        if "memory_edges" in tables:
            db.executemany(
                """INSERT OR IGNORE INTO memory_edges
                   (source_id, target_id, relation, confidence, metadata)
                   VALUES (?, ?, 'evolves_from', 1.0, ?)""",
                [
                    (
                        new_id,
                        source_id,
                        json.dumps({"merge": True}, ensure_ascii=False),
                    )
                    for source_id in valid_ids
                ],
            )
        db.execute(
            f"""UPDATE memories SET archived = 1
                WHERE id IN ({valid_placeholders})""",
            valid_ids,
        )
        if "memories_fts_jieba" in tables:
            db.execute(
                f"""DELETE FROM memories_fts_jieba
                    WHERE rowid IN ({valid_placeholders})""",
                valid_ids,
            )

    index_single_memory_jieba(new_id, content, ",".join(tags), domain)
    return new_id


def similar_clusters(threshold: float = 0.78, limit: int = 300) -> list:
    """
    余弦单链接聚类：发现重复（類似順）。
    返回 [{"members": [{id, content}...]}]，只含 ≥2 条的组。
    惰性维护：无 embedding 的条目跳过。
    """
    import numpy as np
    from models import get_db
    with get_db() as db:
        rows = db.execute(
            """SELECT id, content, embedding FROM memories
               WHERE archived = 0 AND embedding IS NOT NULL
               ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    if len(rows) < 2:
        return []
    ids = [r["id"] for r in rows]
    texts = {r["id"]: r["content"] for r in rows}
    embs = [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
    n = len(ids)
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    norms = [e / (np.linalg.norm(e) + 1e-8) for e in embs]
    for i in range(n):
        for j in range(i + 1, n):
            if float(np.dot(norms[i], norms[j])) >= threshold:
                parent[find(i)] = find(j)          # 单链接：连上即同组
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [
        {"members": [{"id": ids[i], "content": texts[ids[i]][:160]}
                     for i in idx]}
        for idx in groups.values() if len(idx) >= 2
    ]
