"""v5.6 只读工具桥。

工具只允许读取项目状态、错误、记忆和源码；不执行 shell、不写数据库、
不读取 .env/API Key。GPT/Claude 可通过原生工具协议调用；兼容模型在
不支持函数调用时由 app.py 预取真实诊断快照。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from diagnostics import diagnostics


PROJECT_ROOT = Path(__file__).resolve().parent
ALLOWED_SUFFIXES = {
    ".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".sql",
    ".sh", ".conf", ".toml", ".yaml", ".yml",
}
BLOCKED_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "data",  # 避免把数据库、日志与对话数据直接暴露给模型
}
BLOCKED_NAMES = {".env", ".env.example", "diagnostics_errors.jsonl"}
MAX_FILE_BYTES = 512_000


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ReadOnlyToolBridge:
    def __init__(self) -> None:
        self.tools = [
            {
                "name": "get_system_health",
                "description": "读取本地 大西瓜 的真实健康状态，包括数据库、记忆队列、当前模型和最近错误数量。用于系统自检或排查无法回复。",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_recent_errors",
                "description": "读取最近的脱敏错误记录。用于定位 API、记忆、数据库或流式输出问题。",
                "input_schema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "inspect_memory_queue",
                "description": "检查记忆写入队列、worker、向量模型和远程增强状态。",
                "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "search_memory",
                "description": "只读搜索本地记忆库。返回匹配内容、分类、重要度和时间。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "关键词；留空则读取最近记忆"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "list_project_files",
                "description": "列出项目中的安全文本文件。不会显示 .env、数据库、日志或虚拟环境。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "max_depth": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80},
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "read_project_file",
                "description": "按行读取项目内的安全文本文件，用于检查代码。不能读取 .env、数据库或项目外路径。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1, "default": 1},
                        "end_line": {"type": "integer", "minimum": 1, "default": 220},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "search_project_code",
                "description": "在项目安全文本文件中搜索代码或关键词，返回文件、行号和片段。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 80, "default": 30},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_request_trace",
                "description": "读取某次聊天请求的运行轨迹，包括记忆召回、模型请求、保存和工具调用耗时。",
                "input_schema": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                    "additionalProperties": False,
                },
            },
        ]

    def anthropic_tools(self) -> list[dict[str, Any]]:
        return [dict(tool) for tool in self.tools]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            }
            for tool in self.tools
        ]

    def compat_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in self.tools
        ]

    def should_offer_tools(self, message: str, tool_mode: str = "auto") -> bool:
        mode = (tool_mode or "auto").lower()
        if mode == "off":
            return False
        if mode == "always":
            return True
        text = (message or "").lower()
        keywords = (
            "自查", "检查系统", "系统检查", "诊断", "报错", "错误日志", "为什么不能",
            "为什么卡", "记忆队列", "记忆系统", "数据库", "运行轨迹", "请求轨迹",
            "项目文件", "读取代码", "看代码", "搜代码", "检查代码", "哪个文件",
            "system health", "recent errors", "memory queue", "trace", "project code",
        )
        return any(k in text for k in keywords)

    async def prefetch_for_compatible_model(self, message: str) -> tuple[str, list[dict[str, Any]]]:
        """兼容模型没有可靠工具协议时，先执行最相关的只读检查。"""
        text = (message or "").lower()
        calls: list[tuple[str, dict[str, Any]]] = []
        if any(k in text for k in ("代码", "项目文件", "哪个文件", "搜代码")):
            # 没有明确关键词时先给文件清单，不擅自读取大量源码。
            calls.append(("list_project_files", {"path": ".", "max_depth": 2, "limit": 80}))
        if any(k in text for k in ("记忆", "queue", "队列")):
            calls.append(("inspect_memory_queue", {}))
        if any(k in text for k in ("报错", "错误", "卡", "不能", "诊断", "自查", "检查系统")):
            calls.append(("get_system_health", {}))
            calls.append(("get_recent_errors", {"limit": 8}))
        if not calls:
            calls.append(("get_system_health", {}))

        results = []
        blocks = ["<local_tool_snapshot>", "以下是程序刚刚读取的真实本机数据，不是模型猜测："]
        for name, args in calls[:4]:
            started = time.perf_counter()
            try:
                result = await self.execute(name, args)
                ok = not (isinstance(result, dict) and result.get("error"))
            except Exception as exc:
                diagnostics.record_error(
                    "tool_bridge_prefetch", exc, metadata={"tool": name}
                )
                result = {"error": "本机只读工具执行失败，详情已写入诊断记录"}
                ok = False
            duration = (time.perf_counter() - started) * 1000
            results.append({"name": name, "arguments": args, "result": result, "duration_ms": round(duration, 2), "ok": ok})
            blocks.append(f"\n## {name}\n{json.dumps(result, ensure_ascii=False, default=str)[:12000]}")
        blocks.append("\n请只根据这些真实数据分析；不知道的部分明确说不知道。\n</local_tool_snapshot>")
        return "\n".join(blocks), results

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        args = arguments or {}
        handlers = {
            "get_system_health": self._get_system_health,
            "get_recent_errors": self._get_recent_errors,
            "inspect_memory_queue": self._inspect_memory_queue,
            "search_memory": self._search_memory,
            "list_project_files": self._list_project_files,
            "read_project_file": self._read_project_file,
            "search_project_code": self._search_project_code,
            "get_request_trace": self._get_request_trace,
        }
        handler = handlers.get(name)
        if not handler:
            return {"error": f"未知或未授权的只读工具: {name}"}
        return handler(args)

    # ── handlers ──
    def _get_system_health(self, _: dict[str, Any]) -> dict[str, Any]:
        import config
        from models import get_db
        from pipeline import pipeline

        db_ok = False
        db_error = None
        db_counts: dict[str, Any] = {}
        try:
            with get_db() as db:
                db.execute("SELECT 1").fetchone()
                for table in ("sessions", "messages", "memories"):
                    try:
                        db_counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except sqlite3.Error:
                        db_counts[table] = None
            db_ok = True
        except Exception as exc:
            diagnostics.record_error("tool_bridge_database_health", exc)
            db_error = "数据库检查失败，详情已写入诊断记录"

        provider = config.ACTIVE_PROVIDER
        conf = config.get_provider_config(provider)
        memory = pipeline.status()
        errors = diagnostics.recent_errors(5)
        return {
            "ok": bool(db_ok and memory.get("worker_running")),
            "runtime": diagnostics.runtime_info(),
            "provider": {
                "name": provider,
                "model": config.get_active_model(provider),
                "protocol": conf.get("protocol"),
                "api_key_configured": bool(conf.get("api_key")),
            },
            "database": {"ok": db_ok, "error": db_error, "counts": db_counts},
            "memory": memory,
            "recent_errors": errors,
        }

    def _get_recent_errors(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = self._bounded_int(args.get("limit"), 10, 1, 30)
        errors = diagnostics.recent_errors(limit)
        return {"count": len(errors), "errors": errors}

    def _inspect_memory_queue(self, _: dict[str, Any]) -> dict[str, Any]:
        from pipeline import pipeline
        return pipeline.status()

    def _search_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        from models import search_memories
        query = str(args.get("query", ""))[:300]
        limit = self._bounded_int(args.get("limit"), 5, 1, 20)
        rows = search_memories(query, limit)
        items = []
        for row in rows:
            item = dict(row)
            item.pop("embedding", None)
            item.pop("metadata", None)
            items.append({
                "id": item.get("id"),
                "content": item.get("content", "")[:1000],
                "category": item.get("category"),
                "importance": item.get("importance"),
                "created_at": item.get("created_at"),
                "tags": item.get("tags", ""),
            })
        return {"query": query, "count": len(items), "memories": items}

    def _list_project_files(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self._safe_path(str(args.get("path", ".")), must_exist=True, allow_dir=True)
        max_depth = self._bounded_int(args.get("max_depth"), 2, 1, 4)
        limit = self._bounded_int(args.get("limit"), 80, 1, 200)
        files = []
        base_depth = len(root.parts)
        for path in sorted(root.rglob("*")):
            rel_from_root = path.relative_to(PROJECT_ROOT)
            if self._is_blocked(rel_from_root):
                continue
            depth = len(path.parts) - base_depth
            if depth > max_depth:
                continue
            if path.is_file() and self._is_allowed_file(path):
                files.append({
                    "path": str(rel_from_root),
                    "size": path.stat().st_size,
                })
                if len(files) >= limit:
                    break
        return {"root": str(root.relative_to(PROJECT_ROOT)) if root != PROJECT_ROOT else ".", "count": len(files), "files": files}

    def _read_project_file(self, args: dict[str, Any]) -> dict[str, Any]:
        path = self._safe_path(str(args.get("path", "")), must_exist=True, allow_dir=False)
        if not self._is_allowed_file(path):
            return {"error": "该文件类型不在只读白名单内"}
        if path.stat().st_size > MAX_FILE_BYTES:
            return {"error": f"文件过大（>{MAX_FILE_BYTES} bytes），请缩小范围"}
        start = self._bounded_int(args.get("start_line"), 1, 1, 100000)
        end = self._bounded_int(args.get("end_line"), start + 219, start, start + 399)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start - 1:end]
        numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=start))
        return {
            "path": str(path.relative_to(PROJECT_ROOT)),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "total_lines": len(lines),
            "content": numbered[:60000],
        }

    def _search_project_code(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "query 不能为空"}
        root = self._safe_path(str(args.get("path", ".")), must_exist=True, allow_dir=True)
        limit = self._bounded_int(args.get("limit"), 30, 1, 80)
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches = []
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(PROJECT_ROOT)
            if self._is_blocked(rel) or not path.is_file() or not self._is_allowed_file(path):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                    if pattern.search(line):
                        matches.append({"path": str(rel), "line": line_no, "text": line.strip()[:500]})
                        if len(matches) >= limit:
                            return {"query": query, "count": len(matches), "matches": matches}
            except Exception:
                continue
        return {"query": query, "count": len(matches), "matches": matches}

    def _get_request_trace(self, args: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(args.get("request_id", "")).strip()
        trace = diagnostics.get_trace(trace_id)
        return trace or {"error": f"没有找到请求轨迹 {trace_id}"}

    # ── path safety ──
    def _safe_path(self, raw: str, *, must_exist: bool, allow_dir: bool) -> Path:
        raw = (raw or ".").strip()
        candidate = (PROJECT_ROOT / raw).resolve()
        try:
            rel = candidate.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError("路径越界：只能读取当前项目目录") from exc
        if self._is_blocked(rel):
            raise ValueError("该路径被安全策略禁止")
        if must_exist and not candidate.exists():
            raise ValueError(f"路径不存在: {raw}")
        if not allow_dir and candidate.is_dir():
            raise ValueError("需要文件路径，不能读取目录")
        return candidate

    @staticmethod
    def _is_blocked(rel: Path) -> bool:
        return any(part in BLOCKED_PARTS for part in rel.parts) or rel.name in BLOCKED_NAMES or rel.name.startswith(".env")

    @staticmethod
    def _is_allowed_file(path: Path) -> bool:
        return path.name not in BLOCKED_NAMES and not path.name.startswith(".env") and path.suffix.lower() in ALLOWED_SUFFIXES

    @staticmethod
    def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))


tool_bridge = ReadOnlyToolBridge()
