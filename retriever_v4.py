"""
四路并行检索 + Rerank
最终版检索引擎 — 融合全部项目精华

四条检索路径：
1. Vector Search   — 语义相似度 top 10
2. Keyword Search  — FTS5 关键词命中
3. Tag Matching    — 氛围标签交集
4. Random Inject   — 可选的联想彩蛋；默认关闭且不能单独构成召回

然后 Rerank：DeepSeek 从候选池里挑最多 4 条真正相关的
"""
import json
import random
import re
import httpx
from providers.http_client import make_async_http_client
import numpy as np
from datetime import datetime

from config import PROVIDERS, VECTOR_CONFIG, MEMORY_CONFIG
from models import get_db
from embedding_service import embedding_service
from decay_engine import activate_memory

def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def _bytes_to_vec(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def _memory_source_label(source: str) -> str:
    """Human-readable provenance used inside the model context."""
    source = (source or "").strip().lower()
    if source.startswith("chat_user"):
        return "用户原话派生记忆"
    if source.startswith("chat_assistant"):
        return "助手曾经的回复（不等同于用户事实）"
    if source == "manual":
        return "用户手动保存"
    if source == "merge":
        return "用户确认合并"
    if source.startswith("import"):
        return "用户导入资料"
    return "本机长期记忆"


# ── 向量矩阵缓存（补丁：全表逐条扫 → 缓存矩阵 + 单次矩阵乘法）──
_emb_cache = {"fp": None, "ids": None, "mat": None}


def _vector_cache_fingerprint(db) -> tuple:
    row = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS mx "
        "FROM memories WHERE embedding IS NOT NULL AND archived = 0"
    ).fetchone()
    return (row["n"], row["mx"])


def _refresh_vector_cache():
    """活跃记忆变了（新增/归档）才重建矩阵；靠一次 COUNT/MAX 判断。"""
    with get_db() as db:
        fp = _vector_cache_fingerprint(db)
        if fp == _emb_cache["fp"] and _emb_cache["mat"] is not None:
            return
        rows = db.execute(
            "SELECT id, embedding FROM memories "
            "WHERE embedding IS NOT NULL AND archived = 0 ORDER BY id"
        ).fetchall()
    if not rows:
        _emb_cache.update(fp=fp, ids=np.array([], dtype=np.int64),
                          mat=np.zeros((0, 0), dtype=np.float32))
        return
    ids = np.fromiter((r["id"] for r in rows), dtype=np.int64, count=len(rows))
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1e-8
    _emb_cache.update(fp=fp, ids=ids, mat=(mat / norms).astype(np.float32))


# ── 本地 cross-encoder 重排（补丁：每轮一次 LLM 调用 → 本机模型）──
RERANK_MODEL_NAME = "BAAI/bge-reranker-base"  # 轻、中文够用；更强可换 BAAI/bge-reranker-v2-m3
_reranker = None


def _load_reranker():
    global _reranker
    if _reranker is not None:
        return
    from sentence_transformers import CrossEncoder
    _reranker = CrossEncoder(RERANK_MODEL_NAME)  # 首次自动下载缓存


def _rerank_scores(query: str, passages: list[str]) -> list[float]:
    _load_reranker()
    scores = _reranker.predict([[query, p] for p in passages])
    return [float(s) for s in scores]


class RetrieverV4:
    """四路检索 + Rerank"""

    def __init__(self):
        self._client = make_async_http_client(timeout=30)

    async def retrieve(self, query: str,
                       recent_messages: list[str] = None,
                       current_tags: list[str] = None,
                       max_results: int = 4,
                       session_id: str | None = None,
                       exclude_message_ids: set[int] | None = None) -> list[dict]:
        """
        完整检索流程：
        四路并行 → 合并去重 → Rerank → 返回最多 4 条
        """
        # ── 四路并行检索 ──
        vec_results = await self._vector_search(query, limit=10, session_id=session_id)
        kw_results = self._keyword_search(query, limit=10, session_id=session_id)
        tag_results = self._tag_search(current_tags or [], limit=8, session_id=session_id)
        signal_results = vec_results or kw_results or tag_results
        random_results = (
            self._random_inject(count=2, session_id=session_id)
            if MEMORY_CONFIG.get("enable_serendipity", False) and signal_results
            else []
        )

        # ── 合并去重 ──
        candidates = self._merge_deduplicate(
            vec_results, kw_results, tag_results, random_results
        )
        candidates = self._exclude_recent_source_memories(candidates, exclude_message_ids)

        if not candidates:
            return []

        # ── Rerank ──
        context = "\n".join(recent_messages[-6:]) if recent_messages else query
        final = await self._rerank(candidates, context, max_results)

        # 激活被选中的记忆
        for r in final:
            if r.get("id") and r["id"] > 0:
                activate_memory(r["id"])

        return final

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  路径 1: Vector Search
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _vector_search(
        self, query: str, limit: int = 10, *, session_id: str | None = None
    ) -> list[dict]:
        """语义向量搜索。

        关键稳定性规则：聊天请求路径绝不负责下载/加载模型。模型尚未由后台
        写入队列准备好时，本轮直接使用 FTS + 标签检索。
        """
        q = await embedding_service.encode(query, allow_load=False)
        if q is None:
            return []
        q = q.astype(np.float32)

        if session_id is not None:
            # A global embedding cache can rank another window above the current
            # one before SQL filtering. Build the candidate matrix from this
            # session only so cross-window memories cannot influence top-k.
            with get_db() as db:
                scoped_rows = db.execute(
                    """SELECT id, embedding FROM memories
                       WHERE session_id=? AND archived=0 AND embedding IS NOT NULL
                       ORDER BY id DESC LIMIT 4000""",
                    (session_id,),
                ).fetchall()
            ids_list = []
            vectors = []
            for row in scoped_rows:
                try:
                    vec = np.frombuffer(row["embedding"], dtype=np.float32)
                    if vec.shape == q.shape:
                        ids_list.append(int(row["id"]))
                        vectors.append(vec / (np.linalg.norm(vec) + 1e-8))
                except Exception:
                    continue
            if not vectors:
                return []
            mat = np.vstack(vectors)
            ids = np.asarray(ids_list, dtype=np.int64)
        else:
            _refresh_vector_cache()
            mat, ids = _emb_cache["mat"], _emb_cache["ids"]
            if mat is None or mat.shape[0] == 0:
                return []

        qn = q / (np.linalg.norm(q) + 1e-8)
        sims = mat @ qn

        k = min(limit, sims.shape[0])
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        idx = [i for i in idx if sims[i] > 0.45]
        if not idx:
            return []

        hit_ids = [int(ids[i]) for i in idx]
        score_by_id = {int(ids[i]): float(sims[i]) for i in idx}

        with get_db() as db:
            ph = ",".join("?" * len(hit_ids))
            scoped_clause = " AND session_id = ?" if session_id is not None else ""
            params = [*hit_ids, *([session_id] if session_id is not None else [])]
            rows = db.execute(
                f"""SELECT id, content, source AS memory_source, tags,
                           importance, decay_score, valence, arousal
                    FROM memories
                    WHERE id IN ({ph}) AND archived = 0{scoped_clause}""",
                params,
            ).fetchall()
        row_map = {r["id"]: dict(r) for r in rows}

        results = []
        for mid in hit_ids:
            r = row_map.get(mid)
            if not r:
                continue
            results.append({
                "id": mid,
                "content": r["content"],
                "tags": r.get("tags", ""),
                "score": round(score_by_id[mid], 4),
                "source": "vector",
                "retrieval_source": "vector",
                "memory_source": r.get("memory_source", ""),
                "importance": r.get("importance", 0.5),
                "decay_score": r.get("decay_score", 0.5),
                "valence": r.get("valence", 0),
                "arousal": r.get("arousal", 0.5),
            })
        return results

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  路径 2: Keyword Search
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    def _keyword_search(
        self, query: str, limit: int = 10, *, session_id: str | None = None
    ) -> list[dict]:
        """FTS5 关键词搜索 — jieba 分词版，中文搜索质量飞升"""
        try:
            # 优先用 jieba 分词版 FTS
            from memory_optimizer import jieba_search
            jieba_results = jieba_search(query, limit, session_id=session_id)
            if jieba_results:
                # 补全记忆内容
                ids = [r["id"] for r in jieba_results]
                with get_db() as db:
                    placeholders = ",".join("?" * len(ids))
                    scoped_clause = " AND session_id = ?" if session_id is not None else ""
                    params = [*ids, *([session_id] if session_id is not None else [])]
                    rows = db.execute(
                        f"""SELECT id, content, source AS memory_source, tags,
                                   importance, decay_score, valence, arousal
                            FROM memories
                            WHERE id IN ({placeholders}) AND archived = 0{scoped_clause}""",
                        params
                    ).fetchall()
                row_map = {r["id"]: dict(r) for r in rows}

                return [{
                    "id": jr["id"],
                    "content": row_map.get(jr["id"], {}).get("content", ""),
                    "tags": row_map.get(jr["id"], {}).get("tags", ""),
                    "score": max(jr["fts_score"], 0.5),
                    "source": "keyword_jieba",
                    "retrieval_source": "keyword_jieba",
                    "memory_source": row_map.get(jr["id"], {}).get("memory_source", ""),
                    "importance": row_map.get(jr["id"], {}).get("importance", 0.5),
                    "decay_score": row_map.get(jr["id"], {}).get("decay_score", 0.5),
                    "valence": row_map.get(jr["id"], {}).get("valence", 0),
                    "arousal": row_map.get(jr["id"], {}).get("arousal", 0.5),
                } for jr in jieba_results if jr["id"] in row_map]
        except Exception:
            pass

        # jieba 不可用时降级到原版 unicode61 FTS
        try:
            with get_db() as db:
                clauses = ["memories_fts MATCH ?", "m.archived = 0"]
                params: list = [query]
                if session_id is not None:
                    clauses.append("m.session_id = ?")
                    params.append(session_id)
                params.append(limit)
                rows = db.execute(
                    f"""SELECT m.id, m.content, m.source AS memory_source,
                              m.tags, m.importance, m.decay_score,
                              m.valence, m.arousal
                       FROM memories_fts fts
                       JOIN memories m ON fts.rowid = m.id
                       WHERE {' AND '.join(clauses)}
                       ORDER BY fts.rank
                       LIMIT ?""",
                    params
                ).fetchall()

                return [{
                    "id": r["id"],
                    "content": r["content"],
                    "tags": r.get("tags", ""),
                    "score": 0.7,
                    "source": "keyword",
                    "retrieval_source": "keyword",
                    "memory_source": r.get("memory_source", ""),
                    "importance": r.get("importance", 0.5),
                    "decay_score": r.get("decay_score", 0.5),
                    "valence": r.get("valence", 0),
                    "arousal": r.get("arousal", 0.5),
                } for r in (dict(x) for x in rows)]
        except Exception:
            return []

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  路径 3: Tag Matching
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    def _tag_search(
        self, current_tags: list[str], limit: int = 8, *,
        session_id: str | None = None,
    ) -> list[dict]:
        """氛围标签匹配 — 你在撒娇就找以前撒娇的记忆"""
        if not current_tags:
            return []

        with get_db() as db:
            # 参数化查询，防SQL注入
            placeholders = " OR ".join(
                "tags LIKE ?" for _ in current_tags
            )
            params = [f"%{tag}%" for tag in current_tags]
            scoped_clause = " AND session_id = ?" if session_id is not None else ""
            if session_id is not None:
                params.append(session_id)
            params.append(limit)
            rows = db.execute(
                f"""SELECT id, content, source AS memory_source, tags,
                           importance, decay_score, valence, arousal
                    FROM memories
                    WHERE ({placeholders})
                    AND archived = 0{scoped_clause}
                    ORDER BY decay_score DESC
                    LIMIT ?""",
                params
            ).fetchall()

        results = []
        for r in rows:
            r = dict(r)
            row_tags = set((r.get("tags") or "").split(","))
            overlap = len(row_tags & set(current_tags))
            if overlap > 0:
                results.append({
                    "id": r["id"],
                    "content": r["content"],
                    "tags": r.get("tags", ""),
                    "score": round(0.3 + overlap * 0.2, 4),
                    "source": "tag",
                    "retrieval_source": "tag",
                    "memory_source": r.get("memory_source", ""),
                    "importance": r.get("importance", 0.5),
                    "decay_score": r.get("decay_score", 0.5),
                    "valence": r.get("valence", 0),
                    "arousal": r.get("arousal", 0.5),
                    "tag_overlap": overlap,
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  路径 4: Random Injection
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    def _random_inject(
        self, count: int = 2, *, session_id: str | None = None
    ) -> list[dict]:
        """随机抽记忆 — 给 serendipity 的机会"""
        with get_db() as db:
            scoped_clause = " AND session_id = ?" if session_id is not None else ""
            scoped_params = [session_id] if session_id is not None else []
            total = db.execute(
                f"SELECT COUNT(*) FROM memories WHERE archived = 0{scoped_clause}",
                scoped_params,
            ).fetchone()[0]

            if total < count + 5:
                return []

            rows = db.execute(
                f"""SELECT id, content, source AS memory_source, tags,
                          importance, decay_score, valence, arousal
                   FROM memories
                   WHERE archived = 0{scoped_clause}
                   ORDER BY RANDOM()
                   LIMIT ?""",
                [*scoped_params, count]
            ).fetchall()

        return [{
            "id": r["id"],
            "content": r["content"],
            "tags": r.get("tags", ""),
            "score": 0.1,  # 随机的给低基础分，让 rerank 决定
            "source": "random",
            "retrieval_source": "random",
            "memory_source": r.get("memory_source", ""),
            "importance": r.get("importance", 0.5),
            "decay_score": r.get("decay_score", 0.5),
            "valence": r.get("valence", 0),
            "arousal": r.get("arousal", 0.5),
        } for r in (dict(x) for x in rows)]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  合并去重
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    def _merge_deduplicate(self, *result_lists) -> list[dict]:
        """合并四路结果，按 id 去重，保留最高分来源"""
        seen = {}
        for results in result_lists:
            for r in results:
                rid = r["id"]
                if rid not in seen or r["score"] > seen[rid]["score"]:
                    seen[rid] = r
                elif rid in seen:
                    # 记录多路命中（加分）
                    seen[rid]["score"] = min(
                        seen[rid]["score"] + 0.1, 1.0
                    )
                    seen[rid]["source"] += f"+{r['source']}"

        return list(seen.values())

    @staticmethod
    def _exclude_recent_source_memories(
        candidates: list[dict], exclude_message_ids: set[int] | None
    ) -> list[dict]:
        """Avoid paying to inject a derived memory when its source is already in chat history."""
        excluded = {int(value) for value in (exclude_message_ids or set()) if value is not None}
        memory_ids = [int(item.get("id")) for item in candidates if item.get("id")]
        if not excluded or not memory_ids:
            return candidates
        try:
            with get_db() as db:
                mid_ph = ",".join("?" * len(memory_ids))
                msg_ph = ",".join("?" * len(excluded))
                rows = db.execute(
                    f"""SELECT DISTINCT memory_id FROM memory_source_links
                        WHERE memory_id IN ({mid_ph}) AND message_id IN ({msg_ph})""",
                    [*memory_ids, *sorted(excluded)],
                ).fetchall()
            blocked = {int(row["memory_id"]) for row in rows if row["memory_id"] is not None}
            return [item for item in candidates if int(item.get("id") or 0) not in blocked]
        except Exception:
            return candidates

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Rerank
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    async def _rerank(self, candidates: list[dict],
                      context: str,
                      max_results: int = 4) -> list[dict]:
        """本地 cross-encoder 重排（替代 LLM 调用）。最多 max_results 条，
        模型不可用时自动降级到按四路 score 排序。"""
        if len(candidates) <= max_results:
            return candidates

        # 默认不开第二个大型 reranker 模型。旧版会在聊了几轮、候选记忆刚好
        # 超过 4 条时突然下载并加载 bge-reranker-base，看起来就像“聊着聊着死了”。
        if not MEMORY_CONFIG.get("enable_reranker", False):
            candidates.sort(
                key=lambda x: (
                    x.get("score", 0.0) * 0.65
                    + x.get("importance", 0.5) * 0.20
                    + x.get("decay_score", 0.5) * 0.15
                ),
                reverse=True,
            )
            return candidates[:max_results]

        query = context[:800] if context else ""
        passages = [c.get("content", "")[:400] for c in candidates]

        try:
            import asyncio
            scores = await asyncio.to_thread(_rerank_scores, query, passages)
            order = sorted(range(len(candidates)),
                           key=lambda i: scores[i], reverse=True)
            result = []
            for i in order[:max_results]:
                candidates[i]["reranked"] = True
                candidates[i]["rerank_score"] = round(scores[i], 4)
                result.append(candidates[i])
            if result:
                return result
        except Exception as e:
            print(f"[Rerank] 本地重排降级（按 score 取前 N）: {e}")

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:max_results]

    async def _call_rerank_api(self, prompt: str) -> list[int] | None:
        """Run optional reranking on the DeepSeek Flash mechanical route."""
        from api_cost import auxiliary_chat

        try:
            result = await auxiliary_chat(
                messages=[{"role": "user", "content": prompt}],
                purpose="memory_rerank",
                system_prompt="只返回JSON数组。",
                max_output_tokens=100,
            )
            if not result:
                return None
            text = str(result.get("content") or "").strip()
            text = re.sub(r"```json?\s*", "", text)
            text = re.sub(r"```\s*$", "", text)
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else None
        except Exception:
            return None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  对外接口
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    async def build_recall_block(self, query: str,
                                 recent_messages: list[str] = None,
                                 current_tags: list[str] = None,
                                 session_id: str | None = None,
                                 exclude_message_ids: set[int] | None = None) -> str:
        """
        构建 <recall> 块注入 system prompt
        "你想起了这些事"
        """
        results = await self.retrieve(
            query, recent_messages, current_tags, max_results=4,
            session_id=session_id, exclude_message_ids=exclude_message_ids,
        )

        if not results:
            return ""

        lines = [
            "<recall>",
            "你想起了这些事。必须根据来源判断可信度；标为“助手曾经的回复”的内容"
            "只代表助手当时说过，不能当作用户事实或关系事实：",
        ]
        for i, r in enumerate(results, 1):
            tags = r.get("tags", "")
            provenance = _memory_source_label(r.get("memory_source", ""))
            tag_str = f" [{tags}]" if tags else ""
            lines.append(f"{i}. [{provenance}] {r['content'][:300]}{tag_str}")

        lines.append("</recall>")
        return "\n".join(lines)

    async def close(self):
        await self._client.aclose()


# 全局单例
retriever = RetrieverV4()
