"""
完整记忆管线 v4 — 最终融合版
串联：Chunker → Tagger → Dehydrator → Embedding → 四路检索 → Rerank

全自动，不需要 AI "决定要不要记住"
每条消息都自动存、自动切、自动打标、自动索引

在 app.py 的 /api/chat 里替换旧的记忆逻辑：
  旧：memory_context = vector_search.build_memory_context(message)
  新：memory_context = await pipeline.on_message(session_id, "user", message)
"""
import json
import asyncio
import hashlib
import numpy as np
from datetime import datetime

from models import get_db, save_memory
from config import VECTOR_CONFIG, MEMORY_CONFIG, MEMORY_LOG_DIR
from chunker import chunker
from tagger import tagger
from dehydrator import dehydrator
from decay_engine import activate_memory, get_surfacing_memories, run_decay_cycle
from retriever_v4 import retriever
from embedding_service import embedding_service
from diagnostics import diagnostics
from memory_archive import memory_archive
from natural_memory import natural_memory


class MemoryPipeline:
    """
    完整记忆管线。

    v5.3 稳定性原则：
    - 主聊天不等待慢速写入；
    - 写入统一走单消费者队列，避免每轮产生一堆并发 API/模型任务；
    - 记忆失败时 fail-open，绝不让聊天接口一起挂掉。
    """

    def __init__(self):
        self._queue: asyncio.Queue | None = None
        self._worker_task: asyncio.Task | None = None
        self._recent_jobs: dict[str, float] = {}

    def _ensure_job_schema(self) -> None:
        """Keep pending writes and external-document provenance on disk."""
        with get_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_ingest_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    session_id TEXT,
                    message_id INTEGER,
                    external_key TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_error TEXT DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_memory_jobs_ready
                    ON memory_ingest_jobs(status, available_at, id);
                CREATE TABLE IF NOT EXISTS memory_external_documents (
                    external_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    memory_ids_json TEXT NOT NULL DEFAULT '[]',
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_memory_external_active
                    ON memory_external_documents(active, source);
                """
            )
            # Upgrade databases created before the Life-memory bridge existed.
            cols = {str(row["name"]) for row in db.execute("PRAGMA table_info(memory_ingest_jobs)").fetchall()}
            if "external_key" not in cols:
                db.execute("ALTER TABLE memory_ingest_jobs ADD COLUMN external_key TEXT NOT NULL DEFAULT ''")
            if "metadata_json" not in cols:
                db.execute("ALTER TABLE memory_ingest_jobs ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_jobs_external "
                "ON memory_ingest_jobs(external_key, status, id)"
            )

    def _recover_jobs(self) -> None:
        self._ensure_job_schema()
        with get_db() as db:
            db.execute(
                """UPDATE memory_ingest_jobs
                   SET status='pending', updated_at=datetime('now')
                   WHERE status IN ('queued', 'processing')"""
            )

    def _refill_queue(self) -> None:
        """Move durable pending ids into the bounded in-process worker queue."""
        if self._queue is None:
            return
        slots = max(0, self._queue.maxsize - self._queue.qsize())
        if not slots:
            return
        with get_db() as db:
            rows = db.execute(
                """SELECT id FROM memory_ingest_jobs
                   WHERE status='pending'
                     AND available_at <= datetime('now')
                   ORDER BY id ASC LIMIT ?""",
                (slots,),
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                try:
                    self._queue.put_nowait(job_id)
                except asyncio.QueueFull:
                    break
                db.execute(
                    """UPDATE memory_ingest_jobs
                       SET status='queued', updated_at=datetime('now')
                       WHERE id=? AND status='pending'""",
                    (job_id,),
                )

    async def start(self):
        self._recover_jobs()
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=MEMORY_CONFIG.get("ingest_queue_size", 100))
        self._refill_queue()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="memory-ingest-worker")
        asyncio.create_task(embedding_service.warmup())

    async def stop(self):
        # Give quick local writes a chance to finish. Slow enrichment remains
        # safely pending on disk and is resumed on the next launch.
        if self._queue and self._worker_task and not self._worker_task.done():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        # Discard the in-memory queue after shutdown. Its authoritative copy is
        # the durable SQLite job table, so a same-process restart can rebuild a
        # clean queue without duplicating stale ids.
        self._queue = None

        # Close helper HTTP clients as well as the main gateway.
        for helper in (chunker, tagger, dehydrator, retriever):
            try:
                await helper.close()
            except Exception:
                pass

    def enqueue(
        self, content: str, source: str = "chat", *,
        session_id: str | None = None, message_id: int | None = None,
        external_key: str | None = None, metadata: dict | None = None,
    ) -> bool:
        if not MEMORY_CONFIG.get("enabled", True):
            return False
        text = (content or "").strip()
        external_key = str(external_key or "").strip()[:240]
        metadata = metadata if isinstance(metadata, dict) else {}
        is_user_source = str(source or "").startswith("chat_user")
        # Short chat fragments are filtered, but a deliberately saved Life
        # object (even a one-word diary/todo) must still be indexable.
        if not text or (len(text) < 10 and not is_user_source and not external_key):
            return False
        if self._queue is None:
            return False

        content_digest = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()

        # External Life documents are idempotent by their stable object key.
        # Editing the same diary/todo archives the old derived chunks only after
        # the replacement has been indexed successfully.
        if external_key:
            try:
                self._ensure_job_schema()
                with get_db() as db:
                    row = db.execute(
                        "SELECT content_digest, active FROM memory_external_documents WHERE external_key=?",
                        (external_key,),
                    ).fetchone()
                    if row and int(row["active"] or 0) == 1 and row["content_digest"] == content_digest:
                        return False
            except Exception as exc:
                diagnostics.record_error("memory_external_lookup", exc, metadata={"external_key": external_key})

        # Same text can occasionally be scheduled twice by retries.
        digest = hashlib.sha1(
            f"{source}|{session_id or ''}|{message_id or ''}|{external_key}|{text}".encode(
                "utf-8", "ignore"
            )
        ).hexdigest()
        now = datetime.now().timestamp()
        self._recent_jobs = {k: t for k, t in self._recent_jobs.items() if now - t < 120}
        if digest in self._recent_jobs:
            return False

        try:
            self._ensure_job_schema()
            with get_db() as db:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO memory_ingest_jobs
                       (digest, content, source, session_id, message_id, external_key, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (digest, text, source, session_id, message_id, external_key,
                     json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))),
                )
                inserted = bool(cursor.rowcount)
            if not inserted:
                return False
            self._recent_jobs[digest] = now
            self._refill_queue()
            return True
        except Exception as exc:
            diagnostics.record_error(
                "memory_job_persist", exc, metadata={"source": source}
            )
            return False

    async def _worker(self):
        while True:
            try:
                # A failed job can be delayed for a few seconds. Polling lets
                # that durable retry become runnable even when no new chat
                # message arrives to wake the queue.
                job_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                self._refill_queue()
                continue
            job = None
            try:
                with get_db() as db:
                    row = db.execute(
                        "SELECT * FROM memory_ingest_jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()
                    if row:
                        db.execute(
                            """UPDATE memory_ingest_jobs
                               SET status='processing', attempts=attempts+1,
                                   updated_at=datetime('now')
                               WHERE id=?""",
                            (job_id,),
                        )
                        job = dict(row)
                if not job:
                    continue
                try:
                    job_metadata = json.loads(job.get("metadata_json") or "{}")
                except Exception:
                    job_metadata = {}
                if not isinstance(job_metadata, dict):
                    job_metadata = {}
                memory_ids = await self.ingest(
                    job.get("content", ""),
                    job.get("source", "chat"),
                    session_id=job.get("session_id"),
                    message_id=job.get("message_id"),
                    metadata=job_metadata,
                    external_key=job.get("external_key") or None,
                )
                if job.get("external_key"):
                    self._commit_external_document(
                        external_key=str(job.get("external_key")),
                        source=str(job.get("source") or "external"),
                        content=str(job.get("content") or ""),
                        metadata=job_metadata,
                        memory_ids=memory_ids,
                    )
                with get_db() as db:
                    db.execute("DELETE FROM memory_ingest_jobs WHERE id=?", (job_id,))
            except asyncio.CancelledError:
                with get_db() as db:
                    db.execute(
                        """UPDATE memory_ingest_jobs
                           SET status='pending', updated_at=datetime('now')
                           WHERE id=?""",
                        (job_id,),
                    )
                raise
            except Exception as exc:
                print(f"[Memory] 后台写入失败（已隔离）: {type(exc).__name__}")
                attempts = int((job or {}).get("attempts") or 0) + 1
                status = "failed" if attempts >= 3 else "pending"
                with get_db() as db:
                    db.execute(
                        """UPDATE memory_ingest_jobs
                           SET status=?, last_error=?,
                               available_at=datetime('now', '+10 seconds'),
                               updated_at=datetime('now')
                           WHERE id=?""",
                        (status, "记忆后台写入失败，详情已写入本机诊断记录", job_id),
                    )
                diagnostics.record_error(
                    "memory_ingest_worker",
                    exc,
                    metadata={
                        "source": (job or {}).get("source", "chat"),
                        "job_id": job_id,
                        "attempts": attempts,
                    },
                )
            finally:
                self._queue.task_done()
                self._refill_queue()

    def _commit_external_document(
        self, *, external_key: str, source: str, content: str,
        metadata: dict, memory_ids: list[int],
    ) -> None:
        """Atomically replace the active derived chunks for one Life object."""
        self._ensure_job_schema()
        digest = hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest()
        with get_db() as db:
            old = db.execute(
                "SELECT memory_ids_json FROM memory_external_documents WHERE external_key=?",
                (external_key,),
            ).fetchone()
            old_ids: list[int] = []
            if old:
                try:
                    old_ids = [int(x) for x in json.loads(old["memory_ids_json"] or "[]") if int(x) > 0]
                except Exception:
                    old_ids = []
            if old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                db.execute(
                    f"UPDATE memories SET archived=1, updated_at=datetime('now') WHERE id IN ({placeholders})",
                    old_ids,
                )
            db.execute(
                """INSERT INTO memory_external_documents
                   (external_key, source, content_digest, content, metadata_json, memory_ids_json, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, datetime('now'))
                   ON CONFLICT(external_key) DO UPDATE SET
                     source=excluded.source, content_digest=excluded.content_digest,
                     content=excluded.content, metadata_json=excluded.metadata_json,
                     memory_ids_json=excluded.memory_ids_json, active=1, updated_at=datetime('now')""",
                (external_key, source, digest, content,
                 json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                 json.dumps([int(x) for x in memory_ids], separators=(",", ":"))),
            )

    def archive_external_document(self, external_key: str) -> bool:
        """Stop a deleted diary/todo from being recalled while keeping provenance."""
        key = str(external_key or "").strip()[:240]
        if not key:
            return False
        self._ensure_job_schema()
        with get_db() as db:
            pending = db.execute(
                "DELETE FROM memory_ingest_jobs WHERE external_key=? AND status IN ('pending','queued')", (key,)
            ).rowcount
            row = db.execute(
                "SELECT memory_ids_json FROM memory_external_documents WHERE external_key=?", (key,)
            ).fetchone()
            if not row:
                return bool(pending)
            try:
                ids = [int(x) for x in json.loads(row["memory_ids_json"] or "[]") if int(x) > 0]
            except Exception:
                ids = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE memories SET archived=1, updated_at=datetime('now') WHERE id IN ({placeholders})", ids
                )
            db.execute(
                "UPDATE memory_external_documents SET active=0, updated_at=datetime('now') WHERE external_key=?",
                (key,),
            )
        return True

    def status(self) -> dict:
        durable_pending = 0
        durable_failed = 0
        try:
            self._ensure_job_schema()
            with get_db() as db:
                rows = db.execute(
                    """SELECT status, COUNT(*) AS count
                       FROM memory_ingest_jobs GROUP BY status"""
                ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            durable_pending = sum(
                counts.get(key, 0) for key in ("pending", "queued", "processing")
            )
            durable_failed = counts.get("failed", 0)
        except Exception:
            pass
        return {
            "enabled": bool(MEMORY_CONFIG.get("enabled", True)),
            "queue_size": self._queue.qsize() if self._queue else 0,
            "queue_max": self._queue.maxsize if self._queue else 0,
            "durable_pending": durable_pending,
            "durable_failed": durable_failed,
            "worker_running": bool(self._worker_task and not self._worker_task.done()),
            "embeddings_enabled": bool(MEMORY_CONFIG.get("enable_embeddings", True)),
            "embedding_ready": embedding_service.ready,
            "embedding_error": embedding_service.last_error,
            "remote_enrichment": bool(MEMORY_CONFIG.get("use_remote_enrichment", False)),
            "reranker_enabled": bool(MEMORY_CONFIG.get("enable_reranker", False)),
        }

    async def safe_recall_context(self, session_id: str, message: str) -> str:
        if not MEMORY_CONFIG.get("enabled", True):
            return ""
        timeout = float(MEMORY_CONFIG.get("recall_timeout_seconds", 3.0))
        try:
            return await asyncio.wait_for(
                self.recall_context(session_id, message), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            print(f"[Memory] 召回超过 {timeout:.1f}s，本轮跳过")
            diagnostics.record_error("memory_recall_timeout", f"召回超过 {timeout:.1f}s", metadata={"session_id": session_id})
            return ""
        except Exception as exc:
            print(f"[Memory] 召回失败（已隔离）: {type(exc).__name__}: {exc}")
            diagnostics.record_error("memory_recall", exc, metadata={"session_id": session_id})
            return ""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  写入管线
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    async def ingest(
        self, content: str, source: str = "chat", *,
        session_id: str | None = None, message_id: int | None = None,
        metadata: dict | None = None, external_key: str | None = None,
    ):
        """
        完整写入流程 v5
        消息 → chunk → 标签 → 情感 → embedding → 存DB
             → jieba索引 → 矛盾检测 → 图谱关联
        """
        text_content = str(content or "").strip()
        metadata = metadata if isinstance(metadata, dict) else {}
        external_key = str(external_key or "").strip()[:240]
        is_user_source = str(source or "").startswith("chat_user")
        if is_user_source and text_content:
            # This is a local deterministic write and therefore safe to run
            # before the heavier chunk/tag/embed pipeline.
            natural_memory.capture(
                text_content,
                session_id=session_id,
                message_id=message_id,
            )
        if len(text_content) < 10 and not external_key:
            return []

        from memory_optimizer import (
            contradiction_detector, memory_graph,
            index_single_memory_jieba, jieba_search
        )

        # 1. Chunk 切分
        chunks = await chunker.chunk(content, source)

        memory_ids = []
        is_assistant_source = str(source or "").startswith("chat_assistant")
        for chunk in chunks:
            text = chunk["text"]

            # 2. 氛围标签
            tags = await tagger.tag(text)

            # 3. 情感打标（valence/arousal/importance）
            emotion = await dehydrator.process(text, source)

            # 4. Embedding：单例模型 + 串行编码。失败就只存 FTS/标签记忆。
            emb = await embedding_service.encode(text, allow_load=True)
            emb_bytes = emb.astype(np.float32).tobytes() if emb is not None else None

            # 5. 存入 DB
            tags_str = ",".join(tags)
            domain = emotion.get("domain", "other")
            try:
                importance = float(emotion.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            # Assistant prose is useful continuity evidence, but it must not
            # outrank the user's own statements or silently rewrite them.
            if is_assistant_source:
                importance = min(importance, 0.45)
            stored_content = emotion.get("summary", text)
            topic_hint = chunk.get("topic_hint", "") or str(metadata.get("title") or metadata.get("label") or "")[:80]
            memory_metadata = dict(metadata)
            if external_key:
                memory_metadata["external_key"] = external_key
                memory_metadata["chunk_index"] = int(chunk.get("index") or 0)
                kind_label = str(metadata.get("kind_label") or metadata.get("kind") or "Life")
                details = [kind_label]
                if metadata.get("title"):
                    details.append(str(metadata.get("title")))
                if metadata.get("writer"):
                    details.append(f"写信人：{metadata.get('writer')}")
                if metadata.get("date"):
                    details.append(str(metadata.get("date")))
                stored_content = f"【{'｜'.join(details)}】{stored_content}"
            with get_db() as db:
                cursor = db.execute(
                    """INSERT INTO memories 
                    (session_id, content, source, category, importance, embedding, metadata,
                     valence, arousal, domain, decay_score, tags,
                     resolved, archived, topic_hint, activation_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, ?, 0, 0, ?, 0)""",
                    (
                        session_id,
                        stored_content,
                        source,
                        domain,
                        importance,
                        emb_bytes,
                        json.dumps(memory_metadata, ensure_ascii=False, separators=(",", ":")),
                        emotion.get("valence", 0.0),
                        emotion.get("arousal", 0.5),
                        domain,
                        tags_str,
                        topic_hint,
                    )
                )
                mem_id = cursor.lastrowid
                memory_ids.append(mem_id)

            # v5.8：派生记忆只是索引；给它挂上不可改写的原文来源。
            try:
                memory_archive.link_memory(
                    memory_id=mem_id, session_id=session_id, message_id=message_id, original_text=text
                )
            except Exception as exc:
                diagnostics.record_error("memory_source_link", exc, metadata={"memory_id": mem_id, "message_id": message_id})

            # 6. jieba 中文分词索引
            index_single_memory_jieba(mem_id, text, tags_str, domain)

            # 7. 矛盾检测（跟语义相近的旧记忆比对）
            if emb_bytes is not None and emb is not None and not is_assistant_source and not external_key:
                similar = self._find_similar_for_contradiction(emb, session_id=session_id, limit=5)
                if similar:
                    await contradiction_detector.check_and_resolve(
                        text, mem_id, similar
                    )
                    # 8. 图谱自动关联（高相似度的建 related 边）
                    memory_graph.auto_link_similar(mem_id, similar)

            # 9. 写日志
            self._append_log(text, source, tags)

        return memory_ids

    def _find_similar_for_contradiction(
        self, query_emb, *, session_id: str | None = None, limit: int = 5
    ) -> list[dict]:
        """找同一窗口内语义相近的旧记忆（给矛盾检测用）。"""
        with get_db() as db:
            clauses = ["embedding IS NOT NULL", "archived = 0"]
            params: list = []
            if session_id is not None:
                clauses.append("session_id = ?")
                params.append(session_id)
            rows = db.execute(
                f"""SELECT id, content, embedding, importance
                   FROM memories WHERE {' AND '.join(clauses)}
                   ORDER BY id DESC LIMIT 200""",
                params,
            ).fetchall()

        if not rows:
            return []

        results = []
        for row in rows:
            row = dict(row)
            emb = np.frombuffer(row["embedding"], dtype=np.float32)
            sim = float(np.dot(query_emb, emb)
                        / (np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-8))
            if sim > 0.6:
                results.append({
                    "id": row["id"],
                    "content": row["content"],
                    "score": round(sim, 4),
                    "importance": row.get("importance", 0.5),
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  读取管线
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    async def recall(self, query: str,
                     recent_messages: list[str] = None, *,
                     session_id: str | None = None,
                     exclude_message_ids: set[int] | None = None) -> str:
        """
        完整读取流程
        返回可直接注入 system prompt 的文本
        """
        parts = []

        # 1. 给当前对话打氛围标签
        msgs_for_tag = recent_messages[-6:] if recent_messages else [query]
        current_tags = await tagger.tag_current_conversation(msgs_for_tag)

        # 2. 主动浮现（不依赖搜索的高权重未解决记忆）
        surfacing = get_surfacing_memories(limit=2, session_id=session_id)
        surfacing = retriever._exclude_recent_source_memories(surfacing, exclude_message_ids)
        if surfacing:
            parts.append("<surfacing>")
            parts.append("这些有明确来源的记忆主动浮现：")
            for mem in surfacing:
                source = str(mem.get("source") or "")
                if source == "manual":
                    provenance = "用户手动保存"
                elif source == "merge":
                    provenance = "用户确认合并"
                elif source.startswith("chat_user"):
                    provenance = "用户原话派生记忆"
                else:
                    provenance = "本机长期记忆"
                parts.append(f"- [{provenance}] {mem['content'][:200]}")
            parts.append("</surfacing>")

        # 3. 四路检索 + Rerank
        recall_block = await retriever.build_recall_block(
            query=query,
            recent_messages=recent_messages,
            current_tags=current_tags,
            session_id=session_id,
            exclude_message_ids=exclude_message_ids,
        )
        if recall_block:
            parts.append(recall_block)

        # 4. 当前氛围提示
        if current_tags:
            parts.append(f"<mood>当前氛围: {', '.join(current_tags)}</mood>")

        return "\n\n".join(parts)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  app.py 里用的主接口
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    async def recall_context(self, session_id: str, message: str) -> str:
        """
        【只读】回复前调用 — 快速路径
        检索召回，不做任何写入
        典型耗时：~2次API调用（tagger + rerank）

        在 app.py 里这样用：
            memory_context = await pipeline.recall_context(session_id, message)
            # ...流式回复...
            # 流结束后才后台写入：
            asyncio.create_task(pipeline.ingest(message, "chat_user"))
        """
        from models import get_session_context_messages
        recall_window = get_session_context_messages(
            session_id,
            limit=10,
            max_chars=MEMORY_CONFIG.get("recall_history_max_chars", 24000),
            single_message_max_chars=MEMORY_CONFIG.get(
                "recall_single_message_max_chars", 12000
            ),
        )
        history = recall_window["items"]
        recent = [m["content"] for m in history if m["role"] in ("user", "assistant")]
        recent_message_ids = {
            int(m["id"]) for m in history if m.get("id") is not None
        }
        last_assistant = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")

        # 构建完整记忆上下文
        recall_context = await self.recall(
            message, recent, session_id=session_id,
            exclude_message_ids=recent_message_ids,
        )

        full_context = ""
        if recall_context:
            full_context += recall_context

        # Source-linked natural facts stay inside the window that produced them.
        natural_context = natural_memory.build_context(
            message, session_id=session_id, exclude_message_ids=recent_message_ids
        )
        if natural_context:
            if full_context:
                full_context += "\n\n"
            full_context += natural_context

        # Paramecium-style source-faithful lane：机械检索原话，摘要冲突时原文优先。
        archive_context = memory_archive.build_context(
            message, session_id=session_id, echo=last_assistant[:240], limit=3,
            exclude_message_ids=recent_message_ids,
        )
        if archive_context:
            if full_context:
                full_context += "\n\n"
            full_context += archive_context

        return full_context

    async def on_message(self, session_id: str,
                         role: str, content: str) -> str:
        """
        【兼容旧接口】读写混合版 — 不推荐在主聊天流程用！

        问题：ingest 含 chunker/tagger/dehydrator 多次API调用 + embedding，
        一条长消息可能要等 10s+ 才开始回复。
        主流程请改用：recall_context()（回复前） + ingest()（流结束后台跑）
        """
        # 异步写入（不阻塞响应）
        await self.ingest(content, source=f"chat_{role}")

        if role == "user":
            return await self.recall_context(session_id, content)

        return ""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━
    #  日志
    # ━━━━━━━━━━━━━━━━━━━━━━━━━

    def _append_log(self, content: str, source: str, tags: list[str]):
        """追加到今天的日志文件"""
        import os
        from datetime import date

        log_dir = MEMORY_LOG_DIR
        os.makedirs(log_dir, exist_ok=True)

        today = date.today().isoformat()
        fpath = os.path.join(log_dir, f"{today}.md")
        timestamp = datetime.now().strftime("%H:%M")
        tag_str = ", ".join(tags) if tags else ""

        is_new = not os.path.exists(fpath) or os.path.getsize(fpath) == 0
        with open(fpath, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# {today} 日志\n\n")
            f.write(f"- [{timestamp}] [{source}] [{tag_str}] {content[:200]}\n")


# 全局单例
pipeline = MemoryPipeline()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  数据库 Schema 更新
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCHEMA_V4_SQL = """
-- 在 memories 表上加新字段（如果从旧版升级）
-- 新部署直接用完整 CREATE TABLE

-- 氛围标签
ALTER TABLE memories ADD COLUMN tags TEXT DEFAULT '';
-- 话题提示
ALTER TABLE memories ADD COLUMN topic_hint TEXT DEFAULT '';

-- 索引
CREATE INDEX IF NOT EXISTS idx_mem_tags ON memories(tags);

-- FTS5 虚拟表（如果没建过）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts 
USING fts5(content, category, domain, tags, tokenize='unicode61');

-- 触发器：写入时同步 FTS
CREATE TRIGGER IF NOT EXISTS memories_fts_insert_v4
AFTER INSERT ON memories
BEGIN
    INSERT INTO memories_fts(rowid, content, category, domain, tags)
    VALUES (NEW.id, NEW.content, NEW.category, NEW.domain, NEW.tags);
END;
"""
