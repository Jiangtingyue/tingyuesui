"""
融合版记忆管理器 v3
合并自：大西瓜(向量搜索+SQLite) + Ombre Brain(情感坐标+遗忘) + claude-imprint(FTS5+知识库+日志)

三种检索融合：
1. FTS5 全文搜索（关键词匹配，快）    ← claude-imprint
2. bge-small-zh 向量搜索（语义理解）   ← 大西瓜原版
3. 情感坐标加权 + 遗忘曲线排序         ← Ombre Brain

新增：
- Knowledge Bank（长文知识文件）        ← claude-imprint
- Daily Logs（自动日志）               ← claude-imprint
- Pre-compaction Hook（压缩前保存）     ← claude-imprint
- 混合检索评分                         ← 三者融合
"""
import os
import json
import glob
from datetime import datetime, date
from pathlib import Path

from models import get_db
from decay_engine import calc_decay_score, activate_memory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANK_DIR = os.path.join(BASE_DIR, "memory", "bank")
LOGS_DIR = os.path.join(BASE_DIR, "memory")

os.makedirs(BANK_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)


def init_fts5():
    """初始化 FTS5 全文搜索虚拟表"""
    with get_db() as db:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts 
            USING fts5(content, category, domain, tokenize='unicode61')
        """)
        # 触发器：写入记忆时同步到 FTS
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_insert
            AFTER INSERT ON memories
            BEGIN
                INSERT INTO memories_fts(rowid, content, category, domain)
                VALUES (NEW.id, NEW.content, NEW.category, NEW.domain);
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_delete
            AFTER DELETE ON memories
            BEGIN
                DELETE FROM memories_fts WHERE rowid = OLD.id;
            END
        """)


class MemoryManagerV3:
    """融合版记忆管理器"""

    def __init__(self):
        init_fts5()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  混合检索（核心）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def hybrid_search(self, query: str, top_k: int = 8) -> list:
        """
        三路检索融合：
        最终分数 = fts_score × 0.25 + vector_score × 0.35 
                   + decay_score × 0.2 + arousal × 0.1 + importance × 0.1
        """
        # 1. FTS5 关键词搜索
        fts_results = self._fts_search(query, limit=top_k * 2)
        fts_map = {r["id"]: r["fts_score"] for r in fts_results}

        # 2. 向量语义搜索
        vec_results = self._vector_search(query, limit=top_k * 2)
        vec_map = {r["id"]: r["similarity"] for r in vec_results}

        # 合并候选集
        all_ids = set(fts_map.keys()) | set(vec_map.keys())

        if not all_ids:
            # 两种搜索都没结果，试试知识库
            return self._search_knowledge_bank(query, top_k)

        # 3. 从数据库取完整记录 + 情感/衰减信息
        with get_db() as db:
            placeholders = ",".join("?" * len(all_ids))
            rows = db.execute(
                f"""SELECT id, content, category, domain, importance,
                           valence, arousal, decay_score, resolved
                    FROM memories
                    WHERE id IN ({placeholders}) AND archived = 0""",
                list(all_ids)
            ).fetchall()

        # 4. 计算融合分数
        results = []
        for row in rows:
            rid = row["id"]
            fts_s = fts_map.get(rid, 0)
            vec_s = vec_map.get(rid, 0)
            decay_s = row["decay_score"] or 0.5
            arousal = row["arousal"] or 0.5
            importance = row["importance"] or 0.5

            score = (
                fts_s * 0.25
                + vec_s * 0.35
                + decay_s * 0.2
                + arousal * 0.1
                + importance * 0.1
            )

            results.append({
                "id": rid,
                "content": row["content"],
                "category": row["category"],
                "domain": row.get("domain", "other"),
                "score": round(score, 4),
                "fts_score": round(fts_s, 4),
                "vector_score": round(vec_s, 4),
                "decay_score": round(decay_s, 4),
                "valence": row.get("valence", 0),
                "arousal": arousal,
                "resolved": bool(row.get("resolved")),
            })

        results.sort(key=lambda x: x["score"], reverse=True)

        # 激活被检索的记忆
        for r in results[:top_k]:
            activate_memory(r["id"])

        # 补充知识库结果
        bank_results = self._search_knowledge_bank(query, 3)
        if bank_results:
            results.extend(bank_results)

        return results[:top_k]

    def _fts_search(self, query: str, limit: int = 10) -> list:
        """FTS5 全文搜索"""
        try:
            with get_db() as db:
                rows = db.execute(
                    """SELECT rowid as id, rank
                       FROM memories_fts
                       WHERE memories_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (query, limit)
                ).fetchall()
                # FTS5 rank 是负数（越小越好），归一化到 0-1
                if not rows:
                    return []
                max_rank = abs(rows[-1]["rank"]) or 1
                return [
                    {"id": r["id"], "fts_score": 1 - abs(r["rank"]) / max_rank}
                    for r in rows
                ]
        except Exception:
            return []

    def _vector_search(self, query: str, limit: int = 10) -> list:
        """向量语义搜索"""
        try:
            from vector_search_v2 import vector_search_v2
            return vector_search_v2.search(query, top_k=limit)
        except Exception:
            return []

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Knowledge Bank（长文知识文件）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _search_knowledge_bank(self, query: str, limit: int = 3) -> list:
        """搜索 memory/bank/*.md 知识文件"""
        results = []
        md_files = glob.glob(os.path.join(BANK_DIR, "*.md"))

        for fpath in md_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                # 简单关键词匹配（向量搜索太贵，知识库文件通常不多）
                query_words = set(query)
                hits = sum(1 for w in query_words if w in content)
                if hits > 0:
                    fname = os.path.basename(fpath)
                    results.append({
                        "id": -1,
                        "content": content[:500],
                        "category": "knowledge_bank",
                        "domain": fname.replace(".md", ""),
                        "score": hits / max(len(query_words), 1),
                        "source": f"bank/{fname}",
                    })
            except Exception:
                pass

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def save_to_knowledge_bank(self, filename: str, content: str):
        """写入知识库文件"""
        fpath = os.path.join(BANK_DIR, filename)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Daily Logs（自动日志）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def append_daily_log(self, content: str, source: str = "chat"):
        """追加到今天的日志"""
        today = date.today().isoformat()
        fpath = os.path.join(LOGS_DIR, f"{today}.md")

        timestamp = datetime.now().strftime("%H:%M")
        entry = f"\n- [{timestamp}] [{source}] {content}\n"

        with open(fpath, "a", encoding="utf-8") as f:
            if os.path.getsize(fpath) == 0 if os.path.exists(fpath) else True:
                f.write(f"# {today} 日志\n")
            f.write(entry)

    def get_daily_log(self, target_date: str = None) -> str:
        """读取指定日期的日志"""
        target = target_date or date.today().isoformat()
        fpath = os.path.join(LOGS_DIR, f"{target}.md")
        if os.path.exists(fpath):
            with open(fpath, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Pre-compaction Hook
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def pre_compact_save(self, session_id: str):
        """
        压缩前保存（来自 claude-imprint）
        在对话被压缩前，把重要内容存进记忆 + 日志
        """
        messages = get_session_messages(session_id, limit=50)

        if not messages:
            return

        # 提取最近的对话内容
        recent_content = []
        for m in messages[-20:]:
            if m["role"] in ("user", "assistant"):
                recent_content.append(f"[{m['role']}] {m['content'][:200]}")

        summary = "\n".join(recent_content)

        # 写入日志
        self.append_daily_log(
            f"[压缩前保存] Session {session_id[:8]}...\n{summary[:1000]}",
            source="compaction"
        )

        # 写入记忆（高重要性）
        from vector_search_v2 import vector_search_v2
        await vector_search_v2.index_message(
            content=f"对话摘要 ({session_id[:8]}): {summary[:500]}",
            source="compaction",
            emotion_data={"importance": 0.8, "domain": "conversation"},
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  MEMORY.md 自动索引
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def rebuild_index(self):
        """重建 MEMORY.md 索引文件"""
        with get_db() as db:
            categories = db.execute(
                """SELECT domain, COUNT(*) as cnt,
                          AVG(importance) as avg_imp
                   FROM memories 
                   WHERE archived = 0
                   GROUP BY domain
                   ORDER BY cnt DESC"""
            ).fetchall()

            total = db.execute(
                "SELECT COUNT(*) FROM memories WHERE archived = 0"
            ).fetchone()[0]

            archived = db.execute(
                "SELECT COUNT(*) FROM memories WHERE archived = 1"
            ).fetchone()[0]

        lines = [
            f"# 记忆索引",
            f"_自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n",
            f"总计: {total} 条活跃记忆, {archived} 条已归档\n",
            "## 分类统计",
        ]

        for cat in categories:
            lines.append(
                f"- **{cat['domain']}**: {cat['cnt']}条 "
                f"(平均重要性: {cat['avg_imp']:.2f})"
            )

        # 知识库文件列表
        bank_files = glob.glob(os.path.join(BANK_DIR, "*.md"))
        if bank_files:
            lines.append("\n## 知识库文件")
            for f in bank_files:
                fname = os.path.basename(f)
                size = os.path.getsize(f)
                lines.append(f"- `{fname}` ({size}B)")

        index_path = os.path.join(BASE_DIR, "MEMORY.md")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  构建完整上下文（给 gateway 用）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def build_full_context(self, query: str,
                           include_surfacing: bool = True) -> str:
        """
        构建注入 system prompt 的完整记忆上下文
        融合：主动浮现 + 混合搜索 + 知识库 + 今日日志
        """
        parts = []

        # ① 主动浮现
        if include_surfacing:
            from decay_engine import get_surfacing_memories
            surfacing = get_surfacing_memories(limit=3)
            if surfacing:
                parts.append("【主动浮现】")
                for mem in surfacing:
                    parts.append(f"- {mem['content'][:200]}")

        # ② 混合搜索
        search_results = self.hybrid_search(query, top_k=5)
        if search_results:
            parts.append("\n【相关记忆】")
            for r in search_results:
                parts.append(
                    f"- [{r.get('domain','?')}] "
                    f"(得分:{r['score']}) {r['content'][:200]}"
                )

        # ③ 今日日志摘要
        today_log = self.get_daily_log()
        if today_log and len(today_log) > 50:
            # 只取最后几条
            log_lines = today_log.strip().split("\n")[-5:]
            parts.append("\n【今日日志】")
            parts.extend(log_lines)

        context = "\n".join(parts)

        # 限制长度
        if len(context) > 3000:
            context = context[:3000] + "\n...(已截断)"

        return context


# 全局单例
memory_v3 = MemoryManagerV3()
