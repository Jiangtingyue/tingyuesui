from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from runtime_paths import DATA_DIR


class CacheKeepaliveManager:
    """Persistent prompt-cache warmer, adapted from cc-cache-warmer's state machine.

    The transport is JTYHome's own gateway instead of Claude Code/systemd, but the
    safety invariants are preserved: per-session state, activity resets the timer,
    self-generated warms never count as chat activity, consecutive/lifetime fuses,
    persistent event log, and restart/watchdog recovery.
    """

    def __init__(self) -> None:
        self.root = Path(DATA_DIR) / "cache-keepalive"
        self.root.mkdir(parents=True, exist_ok=True)
        self.scheduler: Any | None = None
        self.sender: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
        self.warm_after = int(os.getenv("ANTHROPIC_CACHE_WARM_AFTER_SECONDS", "2700"))  # 45m
        self.latest_safe = int(os.getenv("ANTHROPIC_CACHE_WARM_LATEST_SECONDS", "3000"))  # 50m
        self.ttl = int(os.getenv("ANTHROPIC_CACHE_TTL_SECONDS", "3600"))
        self.consecutive_cap = int(os.getenv("ANTHROPIC_CACHE_WARM_CONSEC_CAP", "2"))
        self.lifetime_cap = int(os.getenv("ANTHROPIC_CACHE_WARM_LIFETIME_CAP", "8"))
        self.fail_cap = int(os.getenv("ANTHROPIC_CACHE_WARM_FAIL_CAP", "3"))
        self.cost_spike_usd = float(os.getenv("ANTHROPIC_CACHE_WARM_COST_SPIKE_USD", "0.20"))
        self.enabled = os.getenv("ANTHROPIC_CACHE_KEEPALIVE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _sha(value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _content_through_last_marker(content: Any) -> Any | None:
        """Return a deep copy ending at the last cache_control marker."""
        if not isinstance(content, list):
            return None
        last = -1
        for idx, block in enumerate(content):
            if isinstance(block, dict) and isinstance(block.get("cache_control"), dict):
                last = idx
        if last < 0:
            return None
        return copy.deepcopy(content[: last + 1])

    @classmethod
    def trim_snapshot_to_deepest_marker(cls, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        """Persist/replay only the provider prefix through the deepest cache marker.

        Anthropic prefix order is tools -> system -> messages.  Main-chat runtime
        tail material (memory/RAG/current message/files) lives after the deepest
        history marker and must never be resent by the 45-minute warmer.
        """
        snap = copy.deepcopy(kwargs)
        messages = snap.get("messages")
        if isinstance(messages, list):
            deepest = -1
            deepest_content = None
            for idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                through = cls._content_through_last_marker(msg.get("content"))
                if through is not None:
                    deepest = idx
                    deepest_content = through
            if deepest >= 0:
                kept = copy.deepcopy(messages[: deepest + 1])
                kept[-1]["content"] = deepest_content
                snap["messages"] = kept
                return snap

        system = snap.get("system")
        through_system = cls._content_through_last_marker(system)
        if through_system is not None:
            snap["system"] = through_system
            snap["messages"] = []
            return snap

        tools = snap.get("tools")
        if isinstance(tools, list):
            deepest_tool = -1
            for idx, tool in enumerate(tools):
                if isinstance(tool, dict) and isinstance(tool.get("cache_control"), dict):
                    deepest_tool = idx
            if deepest_tool >= 0:
                snap["tools"] = copy.deepcopy(tools[: deepest_tool + 1])
                snap.pop("system", None)
                snap["messages"] = []
                return snap
        return None

    def bind(self, scheduler: Any, sender: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> None:
        self.scheduler = scheduler
        self.sender = sender

    def _path(self, key: str) -> Path:
        return self.root / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    @staticmethod
    def _atomic_write(path: Path, state: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _load(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save(self, key: str, state: dict[str, Any]) -> None:
        self._atomic_write(self._path(key), state)

    def cache_lifetime_status(self, key: str, *, now: float | None = None) -> dict[str, Any]:
        """Return persisted cache lifetime state for strict-cache decisions.

        Keepalive state survives process restarts, so strict continuity checks must
        use it instead of relying only on in-memory rolling-health counters.
        """
        state = self._load(key) or {}
        current = time.time() if now is None else float(now)
        last_touch = float(state.get("last_cache_touch_at") or 0)
        expires_at = float(state.get("expires_at") or 0)
        expired = bool(
            (expires_at > 0 and current >= expires_at)
            or (last_touch > 0 and current - last_touch >= max(1, self.ttl))
        )
        return {
            "exists": bool(state),
            "last_cache_touch_at": last_touch,
            "expires_at": expires_at,
            "expired": expired,
            "paused": bool(state.get("paused")),
            "pause_reason": str(state.get("pause_reason") or ""),
        }

    @staticmethod
    def _event(state: dict[str, Any], event: str, **extra: Any) -> None:
        state.setdefault("events", []).append({"ts": time.time(), "type": event, **extra})
        state["events"] = state["events"][-200:]

    def fingerprint(self, *, model: str, session_id: str, ttl: str, prefix_hash: str,
                    shape_hash: str, tools_hash: str, breakpoint: str = "") -> str:
        return self._sha({
            "model": model, "session_id": session_id, "ttl": ttl,
            "prefix_hash": prefix_hash, "shape_hash": shape_hash, "tools_hash": tools_hash,
            "breakpoint": breakpoint,
        })

    def record_main_result(self, *, cache_key: str, provider: str, model: str, session_id: str,
                           diagnostics: dict[str, Any], kwargs: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {}
        read = max(0, int(usage.get("cache_read") or 0))
        write = max(0, int(usage.get("cache_creation") or 0))
        if read <= 0 and write <= 0:
            return {}
        cache_ttl = str(diagnostics.get("cache_ttl") or "")
        # The P1 warmer is explicitly the 45/50/60 design for Anthropic's 1h tier.
        # If an operator intentionally selects 5m caching, do not apply a 45m warmer.
        if cache_ttl != "1h":
            return {}
        tools_hash = self._sha(kwargs.get("tools") or [])
        fp = self.fingerprint(
            model=model, session_id=session_id, ttl=cache_ttl,
            prefix_hash=str(diagnostics.get("cache_prefix_hash") or ""),
            shape_hash=str(diagnostics.get("cache_shape_hash") or ""), tools_hash=tools_hash,
            breakpoint=str(diagnostics.get("cache_breakpoint") or ""),
        )
        state = self._load(cache_key) or {}
        old_fp = str(state.get("fingerprint") or "")
        if old_fp and old_fp != fp:
            # Fingerprint rotation invalidates the old touch timestamp, but a
            # protection pause survives until a *real chat* proves read>0/write=0.
            state = {
                "events": state.get("events", [])[-50:],
                "paused": bool(state.get("paused", False)),
                "pause_reason": state.get("pause_reason"),
            }
            self._event(state, "fingerprint_rotated", old=old_fp[:12], new=fp[:12])
        now = time.time()
        state.update({
            "cache_key": cache_key, "provider": provider, "model": model,
            "session_id": session_id, "fingerprint": fp,
            "last_cache_touch_at": now, "next_warm_at": now + self.warm_after,
            "latest_safe_at": now + self.latest_safe, "expires_at": now + self.ttl,
            "snapshot": self.trim_snapshot_to_deepest_marker(kwargs), "healthy": bool(read > 0 and write == 0),
            "paused": False if (read > 0 and write == 0) else bool(state.get("paused", False)),
            "consecutive_zero_reads": 0 if read > 0 else int(state.get("consecutive_zero_reads") or 0),
            "consecutive_failures": 0,
            "warm_count_consecutive": 0,
            "warm_count_total": int(state.get("warm_count_total") or 0),
        })
        self._event(state, "main_cache_touch", read=read, write=write, fingerprint=fp[:12])
        if not state.get("snapshot"):
            state["paused"] = True
            state["pause_reason"] = "no_cache_breakpoint_snapshot"
            self._event(state, "paused", reason="no_cache_breakpoint_snapshot")
        if (read > 0 and write == 0 and state.get("pause_reason")
                and state.get("snapshot")):
            state.pop("pause_reason", None)
            state["paused"] = False
            self._event(state, "health_recovered")
        self._save(cache_key, state)
        self._schedule(cache_key, state)
        return {
            "cache_fingerprint": fp,
            "cache_last_touch_at": float(state.get("last_cache_touch_at") or 0),
            "cache_next_warm_at": float(state.get("next_warm_at") or 0),
            "cache_keepalive_healthy": bool(state.get("healthy")),
        }

    def _schedule(self, cache_key: str, state: dict[str, Any]) -> None:
        if self.scheduler is None or state.get("paused"):
            return
        from datetime import datetime
        job_id = "cachewarm-" + hashlib.sha256(cache_key.encode()).hexdigest()[:16]
        try:
            self.scheduler.add_job(
                self.fire, "date", run_date=datetime.fromtimestamp(float(state["next_warm_at"])),
                args=[cache_key], id=job_id, replace_existing=True, max_instances=1,
                misfire_grace_time=180, coalesce=True,
            )
        except Exception:
            pass

    async def fire(self, cache_key: str) -> None:
        state = self._load(cache_key)
        if not state or state.get("paused") or self.sender is None:
            return
        now = time.time()
        if now >= float(state.get("expires_at") or 0):
            state["paused"] = True; state["pause_reason"] = "ttl_expired"
            self._event(state, "paused", reason="ttl_expired")
            self._save(cache_key, state); return
        if str(state.get("fingerprint") or "") == "":
            return
        try:
            result = await self.sender(copy.deepcopy(state))
            usage = result.get("usage") if isinstance(result, dict) else {}
            usage = usage if isinstance(usage, dict) else {}
            read = max(0, int(usage.get("cache_read") or 0))
            write = max(0, int(usage.get("cache_creation") or 0))
            cost = max(0.0, float(usage.get("cost") or 0.0))
            state["warm_count_total"] = int(state.get("warm_count_total") or 0) + 1
            state["warm_count_consecutive"] = int(state.get("warm_count_consecutive") or 0) + 1
            state["consecutive_failures"] = 0
            state["consecutive_zero_reads"] = 0 if read > 0 else int(state.get("consecutive_zero_reads") or 0) + 1
            self._event(state, "warmed", read=read, write=write, cost=cost)
            reason = ""
            if write > 0:
                reason = "keepalive_caused_cache_write"
            elif int(state["consecutive_zero_reads"]) >= 2:
                reason = "two_consecutive_zero_reads"
            elif cost >= self.cost_spike_usd:
                reason = "cost_spike"
            elif int(state["warm_count_consecutive"]) >= self.consecutive_cap:
                reason = "consecutive_cap"
            elif int(state["warm_count_total"]) >= self.lifetime_cap:
                reason = "lifetime_cap"
            if reason:
                state["paused"] = True; state["pause_reason"] = reason
                self._event(state, "paused", reason=reason)
            else:
                # Only a real read renews provider TTL. A first zero-read gets
                # one bounded retry at the 50m safety point; it must not fake a
                # fresh TTL locally.
                if read > 0 and write == 0:
                    state["last_cache_touch_at"] = now
                    state["healthy"] = True
                    state["next_warm_at"] = now + self.warm_after
                    state["latest_safe_at"] = now + self.latest_safe
                    state["expires_at"] = now + self.ttl
                else:
                    state["next_warm_at"] = max(
                        now, min(now + 300, float(state.get("latest_safe_at") or now + 300))
                    )
        except Exception as exc:
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
            error_text = f"{type(exc).__name__}:{exc}".lower()
            self._event(state, "warm_failed", error=type(exc).__name__)
            fatal_auth_or_model = any(token in error_text for token in (
                "401", "403", "authentication", "unauthorized", "invalid api key",
                "model not found", "unknown model", "model unavailable",
            ))
            if fatal_auth_or_model:
                state["paused"] = True; state["pause_reason"] = "auth_or_model_error"
                self._event(state, "paused", reason="auth_or_model_error")
            elif int(state["consecutive_failures"]) >= self.fail_cap:
                state["paused"] = True; state["pause_reason"] = "failure_cap"
                self._event(state, "paused", reason="failure_cap")
            else:
                state["next_warm_at"] = max(
                    now, min(now + 300, float(state.get("latest_safe_at") or now + 300))
                )
        self._save(cache_key, state)
        self._schedule(cache_key, state)

    async def watchdog(self) -> None:
        now = time.time()
        for path in self.root.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                key = str(state.get("cache_key") or "")
                if not key or state.get("paused"):
                    continue
                due = float(state.get("next_warm_at") or 0)
                if due <= now:
                    await self.fire(key)
                else:
                    self._schedule(key, state)
            except Exception:
                continue

    async def startup_catchup(self) -> None:
        await self.watchdog()

    def summary(self) -> list[dict[str, Any]]:
        rows = []
        for path in self.root.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                rows.append({k: state.get(k) for k in (
                    "provider", "model", "session_id", "fingerprint", "last_cache_touch_at",
                    "next_warm_at", "expires_at", "healthy", "paused", "pause_reason",
                    "warm_count_total", "warm_count_consecutive",
                )})
            except Exception:
                pass
        return rows


cache_keepalive = CacheKeepaliveManager()
