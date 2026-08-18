"""
MCP 工具处理器
AI 在聊天中直接执行服务器命令、操作数据库
通过 tool_use 机制让 AI 调用服务器端功能
"""
import json
import asyncio
import sqlite3
import time
import subprocess
import shlex
from datetime import datetime

from config import DB_PATH, MCP_CONFIG
from models import get_db, save_memory, search_memories


class MCPHandler:
    """MCP 工具注册 & 执行"""

    def __init__(self):
        self.tools = self._register_tools()

    def _register_tools(self) -> list:
        """注册所有可用工具（Anthropic tool_use 格式）"""
        return [
            {
                "name": "memory_breath",
                "description": "主动浮现：返回当前权重最高的未了结/高情绪记忆。对话开始时呼吸一口记忆。",
                "input_schema": {"type": "object", "properties": {
                    "limit": {"type": "integer", "default": 5}}}
            },
            {
                "name": "memory_hold",
                "description": "存单条记忆，自动过完整管线（切分/打标/情感/索引）。",
                "input_schema": {"type": "object", "properties": {
                    "content": {"type": "string"}}, "required": ["content"]}
            },
            {
                "name": "memory_dream",
                "description": "自省消化：回顾最近的记忆，第一人称过一遍。",
                "input_schema": {"type": "object", "properties": {
                    "days": {"type": "integer", "default": 3}}}
            },
            {
                "name": "memory_trace",
                "description": "改记忆元数据：pin置顶/取消、resolve沉底、归档。",
                "input_schema": {"type": "object", "properties": {
                    "memory_id": {"type": "integer"},
                    "op": {"type": "string", "enum": ["pin", "unpin", "resolve", "archive"]}},
                    "required": ["memory_id", "op"]}
            },
            {
                "name": "memory_pulse",
                "description": "记忆全景诊断；只有显式给出当前聊天 session_id 时，才附上该会话 canonical state 翻译后的主观感受与行为倾向。",
                "input_schema": {"type": "object", "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "当前聊天窗口 ID；缺失时不会猜测或读取其他会话的状态。",
                    },
                }}
            },
            {
                "name": "memory_read",
                "description": "读取记忆库中的记忆。可以按关键词搜索或获取最近的记忆。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "搜索关键词，留空则返回最近记忆"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回数量，默认5",
                            "default": 5
                        }
                    }
                }
            },
            {
                "name": "memory_write",
                "description": "写入新的记忆到记忆库。记录重要的信息、偏好、事件等。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "要记住的内容"
                        },
                        "category": {
                            "type": "string",
                            "description": "分类: preference/event/fact/emotion/other",
                            "enum": ["preference", "event", "fact", "emotion", "other"]
                        },
                        "importance": {
                            "type": "number",
                            "description": "重要性 0-1，默认0.5",
                            "default": 0.5
                        }
                    },
                    "required": ["content"]
                }
            },
            {
                "name": "memory_search",
                "description": "语义搜索记忆库，找到与查询语义相关的记忆。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索查询"
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "返回数量",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "db_query",
                "description": "执行只读 SQL 查询（SELECT）。用于查看聊天记录、统计数据等。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "SQL SELECT 查询语句"
                        }
                    },
                    "required": ["sql"]
                }
            },
            {
                "name": "system_status",
                "description": "获取系统状态：内存、磁盘、运行时间、数据库大小等。",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
        ]

    def get_tools_for_api(self) -> list:
        """返回给 Anthropic API 的 tools 参数"""
        if not MCP_CONFIG.get("enable"):
            return []
        return self.tools


    async def _exec_v52(self, tool_name: str, tool_input: dict):
        """五件套执行（v5.2）"""
        from models import get_db
        if tool_name == "memory_breath":
            from decay_engine import get_surfacing_memories
            return json.dumps({"memories": get_surfacing_memories(
                tool_input.get("limit", 5))}, ensure_ascii=False)
        if tool_name == "memory_hold":
            from pipeline import pipeline
            ids = await pipeline.ingest(tool_input.get("content", ""), source="mcp_hold")
            return json.dumps({"ok": True, "ids": ids}, ensure_ascii=False)
        if tool_name == "memory_dream":
            days = tool_input.get("days", 3)
            with get_db() as db:
                rows = db.execute(
                    """SELECT content FROM memories WHERE archived = 0
                       AND created_at >= datetime('now', ?)
                       ORDER BY importance DESC LIMIT 15""",
                    (f"-{days} days",)).fetchall()
            return json.dumps({"recent": [r["content"] for r in rows],
                               "hint": "用第一人称把这些过一遍"}, ensure_ascii=False)
        if tool_name == "memory_trace":
            mid, op = tool_input.get("memory_id"), tool_input.get("op")
            col = {"pin": ("is_pinned", 1), "unpin": ("is_pinned", 0),
                   "resolve": ("resolved", 1), "archive": ("archived", 1)}.get(op)
            if not col or not mid:
                return json.dumps({"error": "参数不对"})
            with get_db() as db:
                db.execute(f"UPDATE memories SET {col[0]} = ? WHERE id = ?",
                           (col[1], mid))
            return json.dumps({"ok": True, "op": op, "id": mid}, ensure_ascii=False)
        if tool_name == "memory_pulse":
            with get_db() as db:
                total = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                active = db.execute(
                    "SELECT COUNT(*) FROM memories WHERE archived = 0").fetchone()[0]
                pinned = db.execute(
                    "SELECT COUNT(*) FROM memories WHERE is_pinned = 1").fetchone()[0]
            result = {
                "memories": {"total": total, "active": active, "pinned": pinned},
                "canonical_state": {
                    "available": False,
                    "reason": "需要当前聊天 session_id；不会用全局欲望单例代替。",
                },
            }
            session_id = str(tool_input.get("session_id") or "").strip()[:100]
            if session_id:
                from inner_state import inner_state
                view = inner_state.state_view(
                    advance_living=False, session_id=session_id
                )
                result["canonical_state"] = {
                    "available": True,
                    "session_id": session_id,
                    "experience": view.get("experience") or {},
                }
            return json.dumps(result, ensure_ascii=False)
        return None

    async def execute(self, tool_name: str, tool_input: dict) -> str:
        """执行经过统一开关、白名单与参数边界检查的工具调用。"""
        if not MCP_CONFIG.get("enable"):
            return json.dumps({"error": "MCP 工具未启用"}, ensure_ascii=False)
        if tool_name not in MCP_CONFIG["allowed_commands"]:
            return json.dumps({"error": f"工具 {tool_name} 未授权"}, ensure_ascii=False)
        if not isinstance(tool_input, dict):
            return json.dumps({"error": "工具参数必须是对象"}, ensure_ascii=False)

        _v52 = await self._exec_v52(tool_name, tool_input)
        if _v52 is not None:
            return _v52

        handlers = {
            "memory_read": self._memory_read,
            "memory_write": self._memory_write,
            "memory_search": self._memory_search,
            "db_query": self._db_query,
            "system_status": self._system_status,
        }

        handler = handlers.get(tool_name)
        if not handler:
            return json.dumps({"error": f"未知工具: {tool_name}"})

        try:
            result = (
                await asyncio.to_thread(handler, tool_input)
                if tool_name in {"db_query", "system_status"}
                else handler(tool_input)
            )
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps({"error": "工具执行失败，请查看诊断记录"}, ensure_ascii=False)

    # ── 工具实现 ──

    def _memory_read(self, params: dict) -> dict:
        keyword = params.get("keyword", "")
        limit = params.get("limit", 5)
        memories = search_memories(keyword, limit)
        return {
            "count": len(memories),
            "memories": [
                {
                    "id": m["id"],
                    "content": m["content"],
                    "category": m.get("category", ""),
                    "importance": m.get("importance", 0),
                    "created_at": m.get("created_at", ""),
                }
                for m in memories
            ]
        }

    def _memory_write(self, params: dict) -> dict:
        content = params.get("content", "")
        if not content:
            return {"error": "内容不能为空"}

        mem_id = save_memory(
            content=content,
            source="mcp",
            category=params.get("category", "general"),
            importance=params.get("importance", 0.5),
        )

        # 同时做向量索引
        try:
            from vector_search import vector_search
            vector_search.index_message(
                content=content,
                source="mcp",
                category=params.get("category", "general"),
                importance=params.get("importance", 0.5),
            )
        except Exception:
            pass  # 向量索引失败不影响主流程

        return {"success": True, "memory_id": mem_id}

    def _memory_search(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"error": "查询不能为空"}

        try:
            from vector_search import vector_search
            results = vector_search.search(
                query, top_k=params.get("top_k", 5)
            )
            return {"count": len(results), "results": results}
        except Exception as e:
            # 降级到关键词搜索
            memories = search_memories(query, params.get("top_k", 5))
            return {
                "count": len(memories),
                "results": memories,
                "note": "语义搜索不可用，降级为关键词搜索"
            }

    def _db_query(self, params: dict) -> dict:
        sql = params.get("sql", "")
        if not isinstance(sql, str):
            return {"error": "SQL 必须是字符串"}
        sql = sql.strip()
        if not sql:
            return {"error": "SQL 不能为空"}
        if len(sql) > 4000:
            return {"error": "SQL 过长"}
        first = sql.lstrip().split(None, 1)[0].upper() if sql.split() else ""
        if first not in {"SELECT", "WITH"}:
            return {"error": "只允许只读 SELECT/CTE 查询"}

        denied_actions = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_TRANSACTION,
        }
        deadline = time.monotonic() + 0.25

        def authorizer(action, _arg1, _arg2, _db_name, _trigger):
            return sqlite3.SQLITE_DENY if action in denied_actions else sqlite3.SQLITE_OK

        def progress() -> int:
            return 1 if time.monotonic() > deadline else 0

        with get_db() as db:
            try:
                db.execute("PRAGMA query_only=ON")
                db.set_authorizer(authorizer)
                db.set_progress_handler(progress, 1000)
                cursor = db.execute(sql)
                rows = cursor.fetchmany(101)
                truncated = len(rows) > 100
                rows = rows[:100]
                output = []
                total_bytes = 0
                for row in rows:
                    clean = {}
                    for key, value in dict(row).items():
                        if isinstance(value, (bytes, bytearray)):
                            value = f"<binary {len(value)} bytes>"
                        elif isinstance(value, str) and len(value) > 4000:
                            value = value[:4000] + "…"
                        total_bytes += len(str(value).encode("utf-8", "replace"))
                        if total_bytes > 256 * 1024:
                            truncated = True
                            break
                        clean[str(key)] = value
                    if clean:
                        output.append(clean)
                    if total_bytes > 256 * 1024:
                        break
                return {
                    "count": len(output),
                    "rows": output,
                    "truncated": truncated,
                }
            except sqlite3.OperationalError as exc:
                if "interrupted" in str(exc).lower():
                    return {"error": "查询超过时间预算，已停止"}
                return {"error": "查询无效或不被允许"}
            finally:
                db.set_progress_handler(None, 0)
                db.set_authorizer(None)


    def _system_status(self, params: dict) -> dict:
        import os
        import psutil

        db_size = 0
        db_path = DB_PATH
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)

        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        # 数据库统计
        with get_db() as db:
            msg_count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            mem_count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            session_count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        return {
            "timestamp": datetime.now().isoformat(),
            "system": {
                "memory_used_gb": round(memory.used / 1024**3, 2),
                "memory_total_gb": round(memory.total / 1024**3, 2),
                "memory_percent": memory.percent,
                "disk_used_gb": round(disk.used / 1024**3, 2),
                "disk_total_gb": round(disk.total / 1024**3, 2),
                "disk_percent": round(disk.used / disk.total * 100, 1),
            },
            "database": {
                "size_mb": round(db_size / 1024**2, 2),
                "messages": msg_count,
                "memories": mem_count,
                "sessions": session_count,
            },
        }


# 全局单例
mcp_handler = MCPHandler()
