"""
# ⚠️ DEPRECATED v1 — 已被 pipeline.py(v5) 替代，主流程不再引用
# 保留仅供对照，新功能请勿在此文件上开发

向量语义搜索
bge-small-zh-v1.5 本地 embedding
发消息时自动搜索语义相关的历史记忆注入 system prompt
不是关键词匹配，是语义理解，模型跑在本地
"""
import json
import struct
import numpy as np
from typing import Optional

from config import VECTOR_CONFIG
from models import get_db, save_memory

# 延迟加载模型（第一次调用时才初始化）
_model = None
_tokenizer = None


def _load_model():
    """加载 bge-small-zh-v1.5 模型"""
    global _model, _tokenizer
    if _model is not None:
        return

    from sentence_transformers import SentenceTransformer
    model_name = VECTOR_CONFIG["model_name"]
    _model = SentenceTransformer(model_name)
    print(f"[VectorSearch] 模型加载完成: {model_name}")


def encode_text(text: str) -> np.ndarray:
    """文本 → 向量"""
    _load_model()
    embedding = _model.encode(text, normalize_embeddings=True)
    return embedding


def embedding_to_bytes(embedding: np.ndarray) -> bytes:
    """向量 → 二进制（存 SQLite）"""
    return embedding.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    """二进制 → 向量"""
    return np.frombuffer(data, dtype=np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """余弦相似度"""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


class VectorSearch:
    """语义搜索引擎"""

    def index_message(self, content: str, source: str = "chat",
                      category: str = "general", importance: float = 0.5):
        """
        新消息入库时自动 embedding
        在发消息时调用
        """
        # 太短的内容不值得索引
        if len(content.strip()) < 10:
            return None

        embedding = encode_text(content)
        emb_bytes = embedding_to_bytes(embedding)
        mem_id = save_memory(
            content=content,
            source=source,
            category=category,
            importance=importance,
            embedding=emb_bytes,
        )
        return mem_id

    def search(self, query: str, top_k: int = None,
               threshold: float = None) -> list:
        """
        语义搜索
        返回最相关的记忆列表
        """
        top_k = top_k or VECTOR_CONFIG["top_k"]
        threshold = threshold or VECTOR_CONFIG["similarity_threshold"]

        query_vec = encode_text(query)

        # 从数据库读取所有有 embedding 的记忆
        with get_db() as db:
            rows = db.execute(
                "SELECT id, content, source, category, importance, embedding "
                "FROM memories WHERE embedding IS NOT NULL"
            ).fetchall()

        if not rows:
            return []

        # 计算相似度
        results = []
        for row in rows:
            row_dict = dict(row)
            emb = bytes_to_embedding(row_dict["embedding"])
            sim = cosine_similarity(query_vec, emb)

            if sim >= threshold:
                results.append({
                    "id": row_dict["id"],
                    "content": row_dict["content"],
                    "source": row_dict["source"],
                    "category": row_dict["category"],
                    "importance": row_dict["importance"],
                    "similarity": round(sim, 4),
                })

        # 按相似度 × 重要性排序
        results.sort(
            key=lambda x: x["similarity"] * 0.7 + x["importance"] * 0.3,
            reverse=True
        )
        return results[:top_k]

    def build_memory_context(self, query: str) -> str:
        """
        搜索相关记忆，拼成可注入 system prompt 的文本
        """
        memories = self.search(query)
        if not memories:
            return ""

        lines = ["以下是与当前对话相关的历史记忆："]
        for i, mem in enumerate(memories, 1):
            lines.append(
                f"[记忆{i}] (相似度:{mem['similarity']}) {mem['content']}"
            )

        context = "\n".join(lines)

        # 限制注入 token 数（粗略估计）
        max_chars = VECTOR_CONFIG["max_inject_tokens"] * 2  # 中文约2字符/token
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...(记忆过多，已截断)"

        return context

    def batch_index(self, items: list):
        """
        批量索引
        items: [{"content": "...", "source": "...", "category": "..."}]
        """
        for item in items:
            self.index_message(
                content=item["content"],
                source=item.get("source", "manual"),
                category=item.get("category", "general"),
                importance=item.get("importance", 0.5),
            )


# 全局单例
vector_search = VectorSearch()
