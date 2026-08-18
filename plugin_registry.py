"""Lightweight plugin registry for 大西瓜.

The registry deliberately keeps plugins in-process.  A plugin may expose health
and metadata, but it cannot silently replace the chat gateway or database.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Any


@dataclass
class PluginInfo:
    name: str
    display_name: str
    version: str
    description: str
    enabled: bool = True
    source: str = "大西瓜内置"
    health: str = "unknown"
    detail: str = ""
    checked_at: str = ""


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, PluginInfo] = {}
        self._health_checks: dict[str, Callable[[], Any]] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        name: str,
        display_name: str,
        version: str,
        description: str,
        enabled: bool = True,
        source: str = "大西瓜内置",
        health_check: Callable[[], Any] | None = None,
    ) -> None:
        with self._lock:
            self._plugins[name] = PluginInfo(
                name=name,
                display_name=display_name,
                version=version,
                description=description,
                enabled=enabled,
                source=source,
            )
            if health_check:
                self._health_checks[name] = health_check

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            item = self._plugins.get(name)
            if not item:
                return False
            item.enabled = bool(enabled)
            return True

    def snapshot(self, run_checks: bool = True) -> list[dict]:
        with self._lock:
            names = list(self._plugins)
        if run_checks:
            for name in names:
                self._check(name)
        with self._lock:
            return [asdict(self._plugins[name]) for name in sorted(self._plugins)]

    def _check(self, name: str) -> None:
        with self._lock:
            item = self._plugins.get(name)
            check = self._health_checks.get(name)
            if not item:
                return
            if not item.enabled:
                item.health = "disabled"
                item.detail = "已关闭"
                item.checked_at = datetime.now(timezone.utc).isoformat()
                return
        health = "ok"
        detail = "运行正常"
        if check:
            try:
                result = check()
                if isinstance(result, dict):
                    health = str(result.get("health", "ok"))
                    detail = str(result.get("detail", "运行正常"))
                elif result is False:
                    health, detail = "error", "健康检查未通过"
                elif isinstance(result, str):
                    detail = result
            except Exception as exc:  # plugin health must never break the app
                health, detail = "error", f"{type(exc).__name__}: {exc}"
        with self._lock:
            item = self._plugins.get(name)
            if item:
                item.health = health
                item.detail = detail
                item.checked_at = datetime.now(timezone.utc).isoformat()


plugin_registry = PluginRegistry()
