"""轻量可观测性：请求轨迹、错误日志与本机自检。

只为单机私人部署设计：不上传日志，不记录完整对话，只保存必要的
阶段、耗时与脱敏错误信息。
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import DIAGNOSTICS_DIR
from typing import Any


ERROR_LOG_PATH = DIAGNOSTICS_DIR / "errors.jsonl"
MAX_TRACES = 100
MAX_ERRORS = 60


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _duration_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


class DiagnosticsStore:
    def __init__(self) -> None:
        DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        self.started_at = _utc_now()
        self.started_monotonic = time.monotonic()
        self._lock = threading.RLock()
        self._traces: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._errors: deque[dict[str, Any]] = deque(maxlen=MAX_ERRORS)
        self._load_recent_errors()

    # ── 脱敏 ──
    def redact(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            return {str(k): self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, (int, float, bool)):
            return value
        text = str(value)

        secrets = []
        for key, secret in os.environ.items():
            upper = key.upper()
            if secret and any(marker in upper for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
                if len(secret) >= 6:
                    secrets.append(secret)
        for secret in secrets:
            text = text.replace(secret, "***REDACTED***")

        # 常见 bearer/key 片段，避免上游错误原样回显。
        text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1***", text)
        text = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+", r"\1***", text)
        return text[:8000]

    # ── 请求轨迹 ──
    def start_trace(
        self,
        *,
        session_id: str,
        provider: str,
        model: str,
        message_preview: str = "",
    ) -> str:
        trace_id = uuid.uuid4().hex[:12]
        trace = {
            "id": trace_id,
            "session_id": session_id,
            "provider": provider,
            "model": model,
            "message_preview": (message_preview or "")[:80],
            "status": "running",
            "started_at": _utc_now(),
            "started_perf": time.perf_counter(),
            "duration_ms": None,
            "stages": [],
            "tools": [],
            "usage": None,
            "error": None,
        }
        with self._lock:
            self._traces[trace_id] = trace
            self._traces.move_to_end(trace_id)
            while len(self._traces) > MAX_TRACES:
                self._traces.popitem(last=False)
        return trace_id

    def add_stage(
        self,
        trace_id: str,
        name: str,
        *,
        label: str | None = None,
        status: str = "ok",
        duration_ms: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        stage = {
            "name": name,
            "label": label or name,
            "status": status,
            "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
            "at": _utc_now(),
            "details": self.redact(details or {}),
        }
        with self._lock:
            trace = self._traces.get(trace_id)
            if not trace:
                return None
            trace["stages"].append(stage)
        return stage

    def add_tool(
        self,
        trace_id: str,
        *,
        name: str,
        arguments: dict[str, Any] | None,
        result: Any,
        duration_ms: float,
        ok: bool,
    ) -> None:
        item = {
            "name": name,
            "arguments": self.redact(arguments or {}),
            "result_preview": self.redact(result),
            "duration_ms": round(duration_ms, 2),
            "ok": bool(ok),
            "at": _utc_now(),
        }
        # 诊断结果可能很长，轨迹里只留预览。
        preview = item["result_preview"]
        if isinstance(preview, str):
            item["result_preview"] = preview[:1200]
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace:
                trace["tools"].append(item)

    def finish_trace(
        self,
        trace_id: str,
        *,
        status: str = "completed",
        usage: dict[str, Any] | None = None,
        error: Any = None,
    ) -> None:
        with self._lock:
            trace = self._traces.get(trace_id)
            if not trace:
                return
            trace["status"] = status
            trace["duration_ms"] = _duration_ms(trace["started_perf"])
            trace["usage"] = self.redact(usage) if usage else None
            trace["error"] = self.redact(error) if error else None

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        with self._lock:
            trace = self._traces.get(trace_id)
            if not trace:
                return None
            return self._public_trace(trace)

    def recent_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_TRACES))
        with self._lock:
            values = list(self._traces.values())[-limit:]
            return [self._public_trace(t) for t in reversed(values)]

    @staticmethod
    def _public_trace(trace: dict[str, Any]) -> dict[str, Any]:
        result = dict(trace)
        result.pop("started_perf", None)
        return json.loads(json.dumps(result, ensure_ascii=False, default=str))

    # ── 错误 ──
    def record_error(
        self,
        source: str,
        error: Any,
        *,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        exc_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
        entry = {
            "id": uuid.uuid4().hex[:10],
            "at": _utc_now(),
            "source": source,
            "type": exc_type,
            "message": self.redact(str(error)),
            "request_id": request_id,
            "metadata": self.redact(metadata or {}),
        }
        with self._lock:
            self._errors.append(entry)
        try:
            with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass
        return entry

    def recent_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_ERRORS))
        with self._lock:
            return list(reversed(list(self._errors)[-limit:]))

    def clear_errors(self) -> None:
        with self._lock:
            self._errors.clear()
        try:
            ERROR_LOG_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    def _load_recent_errors(self) -> None:
        if not ERROR_LOG_PATH.exists():
            return
        try:
            lines = ERROR_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_ERRORS:]
            for line in lines:
                try:
                    self._errors.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass

    def runtime_info(self, *, include_paths: bool = False) -> dict[str, Any]:
        info = {
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 1),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "recent_error_count": len(self._errors),
            "trace_count": len(self._traces),
        }
        if include_paths:
            info["data_dir"] = str(DIAGNOSTICS_DIR.parent)
        return info


diagnostics = DiagnosticsStore()


class StageTimer:
    """小工具：手动测阶段耗时。"""

    def __init__(self) -> None:
        self.started = time.perf_counter()

    @property
    def ms(self) -> float:
        return _duration_ms(self.started)
