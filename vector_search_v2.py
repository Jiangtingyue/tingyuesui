"""
向量语义搜索 v2 — 融合 Ombre Brain
搜索排序 = 语义相似度 × 情感权重 × 衰减分数
新增：主动浮现（对话开头注入未解决高权重记忆）
"""
import numpy as np
from typing import Optional

from config import VECTOR_CONFIG
from models import get_db, save_memory
from decay_engine import activate_memory, get_surfacing_memories

_model = None


def _load_model():
    global _model
    if _model is not None:
        return
    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer(VECTOR_CONFIG["model_name"])


def encode_text(text: str) -> np.ndarray:
    _load_model()
    return _model.encode(text, normalize_embeddings=True)


def embedding_to_bytes(emb: np.ndarray) -> bytes:
    return emb.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


class VectorSearchV2:
    """融合情感权重的语义搜索"""

    async def index_message(self, content: str, source: str = "chat",
                            emotion_data: dict = None) -> int | None:
        """
        新消息入库：embedding + 情感打标
        emotion_data 来自 dehydrator
        """
        if len(content.strip()) < 10:
            return None

        emb = encode_text(content)
        emb_bytes = embedding_to_bytes(emb)

        ed = emotion_data or {}

        with get_db() as db:
            cursor = db.execute(
                """INSERT INTO memories 
                (content, source, category, importance, embedding,
                 valence, arousal, domain, decay_score, resolved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ed.get("summary", content),
                    source,
                    ed.get("domain", "other"),
                    ed.get("importance", 0.5),
                    emb_bytes,
                    ed.get("valence", 0.0),
                    ed.get("arousal", 0.5),
                    ed.get("domain", "other"),
                    1.0,  # 新记忆初始分数
                    int(ed.get("resolved", False)),
                )
            )
            return cursor.lastrowid

    def search(self, query: str, top_k: int = None,
               threshold: float = None) -> list:
        """
        语义搜索 + 情感加权 + 衰减分数
        综合评分 = similarity × 0.4 + decay_score × 0.3
                   + arousal × 0.15 + importance × 0.15
        """
        top_k = top_k or VECTOR_CONFIG["top_k"]
        threshold = threshold or VECTOR_CONFIG["similarity_threshold"]

        query_vec = encode_text(query)

        with get_db() as db:
            rows = db.execute(
                """SELECT id, content, source, category, importance,
                          embedding, valence, arousal, decay_score,
                          domain, resolved
                   FROM memories
                   WHERE embedding IS NOT NULL AND archived = 0"""
            ).fetchall()

        if not rows:
            return []

        results = []
        for row in rows:
            rd = dict(row)
            emb = bytes_to_embedding(rd["embedding"])
            sim = cosine_similarity(query_vec, emb)

            if sim < threshold:
                continue

            # 综合评分
            score = (
                sim * 0.4
                + (rd.get("decay_score") or 0.5) * 0.3
                + (rd.get("arousal") or 0.5) * 0.15
                + (rd.get("importance") or 0.5) * 0.15
            )

            results.append({
                "id": rd["id"],
                "content": rd["content"],
                "source": rd["source"],
                "domain": rd.get("domain", "other"),
                "similarity": round(sim, 4),
                "score": round(score, 4),
                "valence": rd.get("valence", 0),
                "arousal": rd.get("arousal", 0),
                "decay_score": rd.get("decay_score", 0),
                "resolved": bool(rd.get("resolved")),
            })

        results.sort(key=lambda x: x["score"], reverse=True)

        # 激活被检索到的记忆（抗衰减）
        for r in results[:top_k]:
            activate_memory(r["id"])

        return results[:top_k]

    def build_memory_context(self, query: str,
                             include_surfacing: bool = True) -> str:
        """
        构建注入 system prompt 的记忆上下文
        = 主动浮现记忆 + 语义搜索记忆
        """
        parts = []

        # ① 主动浮现（不依赖搜索，高权重未解决记忆自己冒出来）
        if include_surfacing:
            surfacing = get_surfacing_memories(limit=3)
            if surfacing:
                parts.append("【主动浮现的记忆】")
                for i, mem in enumerate(surfacing, 1):
                    emotion_tag = self._emotion_label(
                        mem.get("valence", 0), mem.get("arousal", 0)
                    )
                    parts.append(
                        f"[浮现{i}] [{emotion_tag}] {mem['content']}"
                    )
                parts.append("")

        # ② 语义搜索（根据当前消息找相关记忆）
        search_results = self.search(query)
        if search_results:
            parts.append("【相关记忆】")
            for i, mem in enumerate(search_results, 1):
                emotion_tag = self._emotion_label(
                    mem.get("valence", 0), mem.get("arousal", 0)
                )
                parts.append(
                    f"[记忆{i}] [{emotion_tag}] (相关度:{mem['score']}) "
                    f"{mem['content']}"
                )

        context = "\n".join(parts)

        # 限制长度
        max_chars = VECTOR_CONFIG["max_inject_tokens"] * 2
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...(已截断)"

        return context

    def _emotion_label(self, valence: float, arousal: float) -> str:
        """把 valence/arousal 坐标翻译成人话"""
        if arousal > 0.6:
            if valence > 0.3:
                return "😊 兴奋/开心"
            elif valence < -0.3:
                return "😢 痛苦/愤怒"
            else:
                return "⚡ 紧张/激动"
        else:
            if valence > 0.3:
                return "☁️ 平静/安心"
            elif valence < -0.3:
                return "🌧️ 低落/疲惫"
            else:
                return "📝 平淡"


# 全局单例
vector_search_v2 = VectorSearchV2()
