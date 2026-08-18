"""Claude Code `-p` + stream-json persistent companion transport.

This adapter follows the supplied P-mode tutorial's core constraints:
- one long-lived Claude Code process per local chat session;
- stdin stays open and accepts stream-json user turns;
- stdout is consumed as NDJSON with a tail-safe line reader;
- browser disconnects do not own the process lifetime;
- ANTHROPIC_API_KEY is always removed from the child environment so Claude Code
  subscription OAuth cannot silently become API billing;
- model/system/MCP changes respawn the process;
- local DB remains the source of truth. Claude Code native session persistence is
  used as an exact-resume accelerator, never as the only conversation store.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator

import config
from runtime_paths import DATA_DIR


class ClaudeCodePError(RuntimeError):
    """A local Claude Code process or one of its result events failed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "claude_code_p_error",
        usage: dict[str, Any] | None = None,
        native_envelope: dict[str, Any] | None = None,
        terminal_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.usage = dict(usage or {})
        self.native_envelope = dict(native_envelope or {})
        self.terminal_reason = str(terminal_reason or "")


@dataclass
class _ProcessState:
    local_session_id: str
    cc_session_id: str
    generation: int
    model: str
    system_hash: str
    spawn_hash: str
    process: asyncio.subprocess.Process
    queue: asyncio.Queue
    stdout_task: asyncio.Task
    stderr_task: asyncio.Task
    stderr_tail: str = ""
    resumed: bool = False
    native_session_started: bool = False
    thinking_flag: bool = True
    created_at: float = field(default_factory=time.monotonic)
    last_result_text: str = ""
    last_used_at: float = field(default_factory=time.monotonic)
    show_thinking: bool = True
    # A native round is not canonical until the visible assistant message has
    # been committed to SQLite by the application layer.
    round_inflight: bool = False
    pending_canonical_commit: bool = False
    pending_commit_token: str = ""
    canonical_commit_event: asyncio.Event = field(default_factory=asyncio.Event)


class ClaudeCodePManager:
    _UUID_NAMESPACE = uuid.UUID("c6325387-2609-4b37-b607-0fd027cc434a")

    def __init__(self) -> None:
        self._states: dict[str, _ProcessState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()
        self._state_dir = DATA_DIR / "claude-code-p"
        self._state_path = self._state_dir / "sessions.json"
        self._persona_dir = self._state_dir / "personas"
        self._persisted = self._load_persisted_state()
        self._thinking_flag_supported: bool | None = None
        self._auth_checked_at: float = 0.0
        self._auth_status: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public status / lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def binary_path() -> str | None:
        binary = str(config.CLAUDE_CODE_P_CONFIG.get("binary") or "claude")
        return config.resolve_claude_code_binary(binary)

    def available(self) -> bool:
        return bool(self.binary_path())

    async def close(self) -> None:
        states = list(self._states.values())
        self._states.clear()
        self._locks.clear()
        for state in states:
            await self._terminate_state(state)

    async def invalidate_session(
        self,
        local_session_id: str,
        *,
        discard_native: bool = False,
        reason: str = "",
    ) -> None:
        """Drop one live process; optionally rotate away from its native transcript.

        ``discard_native`` is required after the application materially changes a
        model draft (for example a strict pre-send rewrite). The local DB is then
        canonical and the next P-mode process receives a recovery transcript from
        that DB instead of resuming the stale Claude Code transcript.
        """
        sid = str(local_session_id or "")
        state = self._states.pop(sid, None)
        persistence_error: Exception | None = None
        if discard_native:
            # Rotate metadata before awaiting process termination. Otherwise a
            # concurrent next request can see the old resumable generation
            # during that await and attach to the transcript being discarded.
            record = dict(self._persisted.get(sid) or {})
            generation = max(
                int(record.get("generation") or 0),
                int(state.generation) if state is not None else 0,
            ) + 1
            self._persisted[sid] = {
                "generation": generation,
                "cc_session_id": self._cc_session_id(sid, generation),
                "native_session_started": False,
                "round_inflight": False,
                "pending_canonical_commit": False,
                "pending_commit_token": "",
                "model": "",
                "system_hash": "",
                "spawn_hash": "",
                "reason": str(reason or "local_canonical_reset")[:120],
                "updated_at": int(time.time()),
            }
            try:
                self._save_persisted_state()
            except Exception as exc:
                # Still terminate the unsafe live process below.  The older
                # on-disk record remains in-flight/pending whenever a native
                # boundary had already been crossed, so restart will fail closed.
                persistence_error = exc
        if state is not None:
            state.round_inflight = False
            state.pending_canonical_commit = False
            state.pending_commit_token = ""
            state.canonical_commit_event.set()
            await self._terminate_state(state)
        if persistence_error is not None:
            raise persistence_error

    async def remove_session(self, local_session_id: str) -> None:
        """Remove every live/native P-mode resource owned by a deleted chat."""
        sid = str(local_session_id or "")
        state = self._states.pop(sid, None)
        if state is not None:
            await self._terminate_state(state)
        self._locks.pop(sid, None)
        self._persisted.pop(sid, None)
        self._save_persisted_state()

    async def commit_visible_turn(
        self,
        local_session_id: str,
        *,
        generation: int,
        commit_token: str,
    ) -> bool:
        """Acknowledge one exact native result after its canonical DB commit.

        Session id alone is insufficient: a delayed request must never clear the
        pending flag belonging to a newer process generation/round.
        """
        sid = str(local_session_id or "")
        token = str(commit_token or "")
        if not sid or not token:
            return False
        state = self._states.get(sid)
        record = dict(self._persisted.get(sid) or {})
        state_matches = bool(
            state is not None
            and not state.round_inflight
            and state.pending_canonical_commit
            and int(state.generation) == int(generation)
            and state.pending_commit_token == token
        )
        record_matches = bool(
            record.get("pending_canonical_commit")
            and not record.get("round_inflight")
            and int(record.get("generation") or 0) == int(generation)
            and str(record.get("pending_commit_token") or "") == token
        )
        # The durable record is the crash boundary. A live in-memory match alone
        # is not enough, and a conflicting live generation must not be cleared by
        # an older receipt.
        if not record_matches or (state is not None and not state_matches):
            return False
        record["pending_canonical_commit"] = False
        record["pending_commit_token"] = ""
        record["updated_at"] = int(time.time())
        self._persisted[sid] = record
        # Persist before waking a request that may be waiting to reuse this
        # transcript. If disk write fails the live pending flag remains closed.
        self._save_persisted_state()
        if state_matches and state is not None:
            state.pending_canonical_commit = False
            state.pending_commit_token = ""
            state.canonical_commit_event.set()
        return True

    async def _reap_live_processes(self, keep_session: str = "") -> None:
        now = time.monotonic()
        idle = float(config.CLAUDE_CODE_P_CONFIG.get("idle_timeout_seconds") or 1800)
        # Dead children do not count toward capacity and should never linger in
        # the state map.  A locked session is in a round and is never an LRU
        # victim, even if that round has been quiet for a long time.
        for sid, state in list(self._states.items()):
            if state.process.returncode is not None:
                self._states.pop(sid, None)
                if state.pending_canonical_commit or state.round_inflight:
                    self._rotate_uncommitted_record(
                        sid,
                        reason=(
                            "process_exited_during_native_round"
                            if state.round_inflight
                            else "process_exited_before_canonical_commit"
                        ),
                    )
                lock = self._locks.get(sid)
                if lock is not None and not lock.locked():
                    self._locks.pop(sid, None)
        victims = [
            (sid, state) for sid, state in list(self._states.items())
            if sid != keep_session and state.process.returncode is None
            and not bool(self._locks.get(sid) and self._locks[sid].locked())
            and now - float(state.last_used_at or state.created_at) >= idle
        ]
        for sid, state in victims:
            if self._states.get(sid) is state:
                self._states.pop(sid, None)
            await self._terminate_state(state)
            lock = self._locks.get(sid)
            if lock is not None and not lock.locked():
                self._locks.pop(sid, None)
        max_live = int(config.CLAUDE_CODE_P_CONFIG.get("max_live_processes") or 8)
        keep_is_live = bool(
            keep_session in self._states
            and self._states[keep_session].process.returncode is None
        )
        needs_new_slot = 0 if keep_is_live else 1
        live_count = sum(
            1 for state in self._states.values() if state.process.returncode is None
        )
        live = [
            (sid, st) for sid, st in self._states.items()
            if sid != keep_session and st.process.returncode is None
            and not bool(self._locks.get(sid) and self._locks[sid].locked())
        ]
        while live_count + needs_new_slot > max_live and live:
            sid, state = min(
                live, key=lambda pair: float(pair[1].last_used_at or pair[1].created_at)
            )
            self._states.pop(sid, None)
            await self._terminate_state(state)
            live_count -= 1
            lock = self._locks.get(sid)
            if lock is not None and not lock.locked():
                self._locks.pop(sid, None)
            live = [
                (x, st) for x, st in self._states.items()
                if x != keep_session and st.process.returncode is None
                and not bool(self._locks.get(x) and self._locks[x].locked())
            ]

    async def request(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        stable_system_prompt: str,
        memory_context: str | None,
        turn_instructions: str | None,
        options: dict[str, Any] | None,
        local_session_id: str | None,
        stream: bool,
        purpose: str,
    ) -> dict[str, Any] | AsyncGenerator[dict[str, Any], None]:
        if not self.available():
            raise ClaudeCodePError(
                "未找到 Claude Code CLI。请先安装 Claude Code 并运行 claude auth login。",
                code="claude_code_cli_missing",
            )
        await self._assert_subscription_auth()
        if purpose != "main_chat":
            generator = self._one_shot_stream(
                messages=messages,
                model=model,
                stable_system_prompt=stable_system_prompt,
                memory_context=memory_context,
                turn_instructions=turn_instructions,
                options=options or {},
                purpose=purpose,
            )
        else:
            if not local_session_id:
                raise ClaudeCodePError(
                    "P 模式需要稳定 session_id 才能维持一聊天一进程。",
                    code="claude_code_session_missing",
                )
            generator = self._persistent_stream(
                messages=messages,
                model=model,
                stable_system_prompt=stable_system_prompt,
                memory_context=memory_context,
                turn_instructions=turn_instructions,
                options=options or {},
                local_session_id=str(local_session_id),
            )
        if stream:
            return generator
        return await self._collect(generator, model=model)

    # ------------------------------------------------------------------
    # Persistent session path
    # ------------------------------------------------------------------
    async def _persistent_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        stable_system_prompt: str,
        memory_context: str | None,
        turn_instructions: str | None,
        options: dict[str, Any],
        local_session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        lock = self._locks.setdefault(local_session_id, asyncio.Lock())
        async with lock:
            previous = self._states.get(local_session_id)
            if previous and previous.round_inflight:
                # A coroutine vanished after the user event entered Claude but
                # before a terminal result was durably classified. Its native
                # transcript is unknowable, so rebuild from the local DB now.
                await self.invalidate_session(
                    local_session_id,
                    discard_native=True,
                    reason="orphaned_inflight_native_round",
                )
                previous = None
            if previous and previous.pending_canonical_commit:
                # Do not hold the global process-pool lock while the app finishes
                # its tiny SQLite commit; unrelated conversations stay free.
                try:
                    await asyncio.wait_for(
                        previous.canonical_commit_event.wait(), timeout=10.0
                    )
                except asyncio.TimeoutError as exc:
                    await self.invalidate_session(
                        local_session_id,
                        discard_native=True,
                        reason="previous_visible_commit_unconfirmed",
                    )
                    raise ClaudeCodePError(
                        "上一轮可见回复尚未完成本机提交，P 模式已安全重建；请重试本轮。",
                        code="claude_code_canonical_commit_pending",
                    ) from exc
            # Lock this conversation before the pool scans for idle/LRU
            # victims.  Pool mutation and spawn are serialized so two new tabs
            # cannot both observe the same free slot and exceed capacity.
            async with self._pool_lock:
                await self._reap_live_processes(keep_session=local_session_id)
                state = await self._ensure_persistent_state(
                    local_session_id=local_session_id,
                    model=model,
                    stable_system_prompt=stable_system_prompt,
                    options=options,
                )
            latest = self._latest_user_content(messages)
            recovery = ""
            if not state.resumed and not state.native_session_started:
                recovery = self._recovery_context(messages[:-1])
            content = self._attach_turn_context(
                latest,
                memory_context=memory_context,
                turn_instructions=turn_instructions,
                recovery_context=recovery,
            )

            # Recover old local history in the same input as the actual user
            # turn.  A separate hidden “read this and confirm” round polluted
            # Claude Code's native transcript, consumed usage, and made turn
            # counts disagree with the visible application conversation.
            try:
                self._begin_native_round(state)
            except Exception:
                # No user event is written until the safety marker is durable.
                # Still terminate the live child so an in-memory half-state is
                # never reused if the application catches this exception.
                try:
                    await self.invalidate_session(
                        local_session_id,
                        discard_native=True,
                        reason="begin_round_state_persist_failed",
                    )
                except Exception:
                    pass
                raise

            try:
                async for item in self._run_round(state, content=content, emit=True):
                    yield item
            except ClaudeCodePError as exc:
                if self._retryable_launch_failure(state, exc):
                    state = await self._respawn_without_problem_flag(
                        state,
                        local_session_id=local_session_id,
                        model=model,
                        stable_system_prompt=stable_system_prompt,
                        options=options,
                        force_new=not state.resumed,
                    )
                    # If exact native resume could not be used, attach recovery
                    # to the retried actual turn rather than creating a ghost turn.
                    if not state.resumed:
                        recovery = self._recovery_context(messages[:-1])
                        content = self._attach_turn_context(
                            latest,
                            memory_context=memory_context,
                            turn_instructions=turn_instructions,
                            recovery_context=recovery,
                        )
                    try:
                        self._begin_native_round(state)
                    except Exception:
                        try:
                            await self.invalidate_session(
                                local_session_id,
                                discard_native=True,
                                reason="retry_begin_round_state_persist_failed",
                            )
                        except Exception:
                            pass
                        raise
                    try:
                        async for item in self._run_round(state, content=content, emit=True):
                            yield item
                    except ClaudeCodePError as retry_exc:
                        await self.invalidate_session(
                            local_session_id, discard_native=True,
                            reason=f"retry_round_failed:{retry_exc.code}",
                        )
                        raise
                else:
                    await self.invalidate_session(
                        local_session_id, discard_native=True,
                        reason=f"failed_round:{exc.code}",
                    )
                    raise
            except asyncio.CancelledError:
                # An explicit application cancellation leaves the child agent's
                # internal turn boundary uncertain. Kill and rotate so the next
                # request recovers from the local canonical DB.
                await self.invalidate_session(
                    local_session_id,
                    discard_native=True,
                    reason="cancelled_turn",
                )
                raise

    async def _ensure_persistent_state(
        self,
        *,
        local_session_id: str,
        model: str,
        stable_system_prompt: str,
        options: dict[str, Any],
    ) -> _ProcessState:
        system_hash = self._hash_text(stable_system_prompt)
        spawn_hash = self._spawn_signature(
            model=model, system_hash=system_hash, options=options
        )
        current = self._states.get(local_session_id)
        if current and current.process.returncode is None:
            if current.spawn_hash == spawn_hash:
                current.last_used_at = time.monotonic()
                return current
            await self.invalidate_session(
                local_session_id,
                discard_native=True,
                reason="spawn_frozen_option_changed",
            )

        record = dict(self._persisted.get(local_session_id) or {})
        if record.get("pending_canonical_commit") or record.get("round_inflight"):
            # Either the user event entered an unfinished native round, or a
            # terminal native result was never matched to its visible SQLite
            # commit (crash/power loss). Rotate instead of resuming across an
            # unknown native boundary or exposing a reply the user never saw.
            self._rotate_uncommitted_record(
                local_session_id,
                reason=(
                    "startup_inflight_native_round"
                    if record.get("round_inflight")
                    else "startup_uncommitted_native_round"
                ),
            )
            record = dict(self._persisted.get(local_session_id) or {})
        frozen_mismatch = bool(
            record.get("native_session_started")
            and (
                str(record.get("model") or "") != str(model or "")
                or str(record.get("system_hash") or "") != system_hash
                or str(record.get("spawn_hash") or "") != spawn_hash
            )
        )
        if frozen_mismatch:
            generation = int(record.get("generation") or 0) + 1
            record = {
                "generation": generation,
                "cc_session_id": self._cc_session_id(local_session_id, generation),
                "native_session_started": False,
                "round_inflight": False,
                "pending_canonical_commit": False,
                "pending_commit_token": "",
                "model": str(model or ""),
                "system_hash": system_hash,
                "spawn_hash": spawn_hash,
                "reason": "persisted_spawn_signature_changed",
                "updated_at": int(time.time()),
            }
            self._persisted[local_session_id] = record
            self._save_persisted_state()
        generation = int(record.get("generation") or 0)
        cc_session_id = str(record.get("cc_session_id") or self._cc_session_id(local_session_id, generation))
        should_resume = bool(record.get("native_session_started"))
        state = await self._spawn(
            local_session_id=local_session_id,
            cc_session_id=cc_session_id,
            generation=generation,
            model=model,
            system_prompt=stable_system_prompt,
            options=options,
            resume=should_resume,
            no_session_persistence=False,
        )
        self._states[local_session_id] = state
        return state

    async def _respawn_without_problem_flag(
        self,
        state: _ProcessState,
        *,
        local_session_id: str,
        model: str,
        stable_system_prompt: str,
        options: dict[str, Any],
        force_new: bool,
    ) -> _ProcessState:
        await self._terminate_state(state)
        if self._states.get(local_session_id) is state:
            self._states.pop(local_session_id, None)

        stderr = state.stderr_tail.lower()
        # Only the undocumented thinking-display flag gets an automatic
        # compatibility downgrade. An unrelated unknown option must not be
        # misdiagnosed as a thinking problem.
        disable_thinking = "thinking-display" in stderr
        if disable_thinking:
            self._thinking_flag_supported = False

        record = dict(self._persisted.get(local_session_id) or {})
        generation = int(record.get("generation") or state.generation)
        if force_new or self._looks_like_resume_failure(stderr):
            generation += 1
            cc_session_id = self._cc_session_id(local_session_id, generation)
            resume = False
        else:
            cc_session_id = str(record.get("cc_session_id") or state.cc_session_id)
            resume = bool(record.get("native_session_started"))

        next_state = await self._spawn(
            local_session_id=local_session_id,
            cc_session_id=cc_session_id,
            generation=generation,
            model=model,
            system_prompt=stable_system_prompt,
            options=options,
            resume=resume,
            no_session_persistence=False,
        )
        self._states[local_session_id] = next_state
        self._persisted[local_session_id] = {
            "generation": generation,
            "cc_session_id": cc_session_id,
            "native_session_started": bool(resume),
            "round_inflight": False,
            "pending_canonical_commit": False,
            "pending_commit_token": "",
            "model": str(model or ""),
            "system_hash": next_state.system_hash,
            "spawn_hash": next_state.spawn_hash,
            "updated_at": int(time.time()),
        }
        self._save_persisted_state()
        return next_state

    # ------------------------------------------------------------------
    # One-shot helper path for hidden rewrites / memory helpers
    # ------------------------------------------------------------------
    async def _one_shot_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        stable_system_prompt: str,
        memory_context: str | None,
        turn_instructions: str | None,
        options: dict[str, Any],
        purpose: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        synthetic = f"helper:{purpose}:{uuid.uuid4()}"
        cc_session_id = str(uuid.uuid4())
        state = await self._spawn(
            local_session_id=synthetic,
            cc_session_id=cc_session_id,
            generation=0,
            model=model,
            system_prompt=stable_system_prompt,
            options=options,
            resume=False,
            no_session_persistence=True,
        )
        try:
            # A helper must see the whole supplied mini-conversation because it
            # deliberately does not share the companion's persistent process.
            transcript = self._recovery_context(messages[:-1], include_header=False)
            latest = self._latest_user_content(messages)
            latest_text = self._content_to_text(latest)
            if transcript:
                latest_text = transcript + "\n\n[当前请求]\n" + latest_text
            content = self._attach_turn_context(
                latest_text,
                memory_context=memory_context,
                turn_instructions=turn_instructions,
            )
            async for item in self._run_round(state, content=content, emit=True):
                yield item
        finally:
            await self._terminate_state(state)

    # ------------------------------------------------------------------
    # Spawn / stream event handling
    # ------------------------------------------------------------------
    async def _spawn(
        self,
        *,
        local_session_id: str,
        cc_session_id: str,
        generation: int,
        model: str,
        system_prompt: str,
        options: dict[str, Any],
        resume: bool,
        no_session_persistence: bool,
    ) -> _ProcessState:
        binary = self.binary_path()
        if not binary:
            raise ClaudeCodePError(
                "未找到 Claude Code CLI。请先安装 Claude Code 并运行 claude auth login。",
                code="claude_code_cli_missing",
            )
        persona_path, system_hash = self._persona_file(system_prompt)
        args = self._build_args(
            binary=binary,
            model=model,
            persona_path=persona_path,
            cc_session_id=cc_session_id,
            options=options,
            resume=resume,
            no_session_persistence=no_session_persistence,
        )
        spawn_hash = self._spawn_signature(
            model=model, system_hash=system_hash, options=options
        )
        env = self._child_env(options)

        cwd = Path(str(config.CLAUDE_CODE_P_CONFIG.get("cwd") or config.BASE_DIR)).expanduser()
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=int(config.CLAUDE_CODE_P_CONFIG.get("stdout_line_limit_bytes") or 4_194_304),
            )
        except (OSError, FileNotFoundError) as exc:
            raise ClaudeCodePError(
                f"Claude Code P 模式无法启动：{type(exc).__name__}",
                code="claude_code_spawn_failed",
            ) from exc
        queue: asyncio.Queue = asyncio.Queue(maxsize=int(config.CLAUDE_CODE_P_CONFIG.get("event_queue_max") or 512))
        placeholder = asyncio.current_task()
        state = _ProcessState(
            local_session_id=local_session_id,
            cc_session_id=cc_session_id,
            generation=generation,
            model=model,
            system_hash=system_hash,
            spawn_hash=spawn_hash,
            process=proc,
            queue=queue,
            stdout_task=placeholder,  # replaced immediately below
            stderr_task=placeholder,
            show_thinking=(str(options.get("thinking_visibility") or "full").lower() == "full"),
            resumed=resume,
            native_session_started=resume,
            thinking_flag=("--thinking-display" in args),
        )
        state.stdout_task = asyncio.create_task(self._stdout_reader(state))
        state.stderr_task = asyncio.create_task(self._stderr_reader(state))
        return state

    def _spawn_signature(
        self, *, model: str, system_hash: str, options: dict[str, Any]
    ) -> str:
        """Fingerprint every option that is frozen when the CLI process spawns.

        A live stream-json process cannot hot-apply model/persona/MCP/tool/permission
        or thinking/effort flags.  Reusing it after one of those changes would make
        the UI claim a setting that the actual Claude process never received.
        """
        cfg = config.CLAUDE_CODE_P_CONFIG
        binary = self.binary_path() or str(cfg.get("binary") or "claude")
        cwd = str(Path(str(cfg.get("cwd") or config.BASE_DIR)).expanduser().resolve())
        payload = {
            "binary": binary,
            "cwd": cwd,
            "model": str(model or "sonnet"),
            "system_hash": system_hash,
            "thinking_display": str(cfg.get("thinking_display") or ""),
            "thinking_flag_supported": self._thinking_flag_supported is not False,
            "thinking_visibility": str(options.get("thinking_visibility") or "full").lower(),
            "thinking_mode": str(options.get("thinking_mode") or "auto").lower(),
            "max_output_tokens": int(options.get("max_output_tokens") or 0),
            "reasoning_effort": str(options.get("reasoning_effort") or "").lower(),
            "fallback_model": str(cfg.get("fallback_model") or ""),
            "max_turns": int(cfg.get("max_turns") or 0),
            "max_budget_usd": float(cfg.get("max_budget_usd") or 0.0),
            "tools": str(cfg.get("tools") or ""),
            "mcp_config": str(cfg.get("mcp_config") or ""),
            "mcp_config_hash": self._mcp_config_hash(
                str(cfg.get("mcp_config") or "")
            ),
            "strict_mcp": bool(cfg.get("strict_mcp", True)),
            "permission_mode": str(cfg.get("permission_mode") or ""),
            "dangerously_skip_permissions": bool(cfg.get("dangerously_skip_permissions")),
            "allowed_tools": str(cfg.get("allowed_tools") or ""),
            "disallowed_tools": str(cfg.get("disallowed_tools") or ""),
            "permission_prompt_tool": str(cfg.get("permission_prompt_tool") or ""),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _mcp_config_hash(raw_path: str) -> str:
        """Fingerprint MCP contents, not only its path."""
        value = str(raw_path or "").strip()
        if not value:
            return ""
        path = Path(value).expanduser()
        try:
            data = path.read_bytes()
        except OSError:
            return "missing:" + str(path)
        return hashlib.sha256(data).hexdigest()

    def _build_args(
        self,
        *,
        binary: str,
        model: str,
        persona_path: Path,
        cc_session_id: str,
        options: dict[str, Any],
        resume: bool,
        no_session_persistence: bool,
    ) -> list[str]:
        args = [
            binary,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", str(model or "sonnet"),
            "--system-prompt-file", str(persona_path),
        ]
        if resume:
            args += ["--resume", cc_session_id]
        else:
            args += ["--session-id", cc_session_id]
        if no_session_persistence:
            args.append("--no-session-persistence")

        thinking = str(config.CLAUDE_CODE_P_CONFIG.get("thinking_display") or "").strip()
        visibility = str(options.get("thinking_visibility") or "full").lower()
        thinking_mode = str(options.get("thinking_mode") or "auto").lower()
        if (
            thinking
            and visibility != "hidden"
            and thinking_mode != "disabled"
            and self._thinking_flag_supported is not False
        ):
            args += ["--thinking-display", thinking]

        effort = str(options.get("reasoning_effort") or "").strip().lower()
        if effort and effort not in {"auto", "none"}:
            args += ["--effort", effort]

        fallback_model = str(config.CLAUDE_CODE_P_CONFIG.get("fallback_model") or "").strip()
        if fallback_model:
            args += ["--fallback-model", fallback_model]

        max_turns = int(config.CLAUDE_CODE_P_CONFIG.get("max_turns") or 0)
        if max_turns > 0:
            args += ["--max-turns", str(max_turns)]
        max_budget = float(config.CLAUDE_CODE_P_CONFIG.get("max_budget_usd") or 0)
        if max_budget > 0:
            args += ["--max-budget-usd", f"{max_budget:g}"]

        tools = str(config.CLAUDE_CODE_P_CONFIG.get("tools") or "").strip()
        if not tools:
            # Tutorial's pure-persona recipe: no built-in tool schema/token cost.
            args += ["--tools", ""]
        elif tools.lower() != "default":
            args += ["--tools", tools]

        mcp = str(config.CLAUDE_CODE_P_CONFIG.get("mcp_config") or "").strip()
        if mcp:
            args += ["--mcp-config", str(Path(mcp).expanduser())]
            if config.CLAUDE_CODE_P_CONFIG.get("strict_mcp", True):
                args.append("--strict-mcp-config")

        permission_prompt_tool = str(
            config.CLAUDE_CODE_P_CONFIG.get("permission_prompt_tool") or ""
        ).strip()
        allowed_tools = str(config.CLAUDE_CODE_P_CONFIG.get("allowed_tools") or "").strip()
        disallowed_tools = str(config.CLAUDE_CODE_P_CONFIG.get("disallowed_tools") or "").strip()
        if allowed_tools:
            args += ["--allowedTools", allowed_tools]
        if disallowed_tools:
            args += ["--disallowedTools", disallowed_tools]

        if config.CLAUDE_CODE_P_CONFIG.get("dangerously_skip_permissions"):
            args.append("--dangerously-skip-permissions")
        elif permission_prompt_tool:
            args += ["--permission-prompt-tool", permission_prompt_tool]
        elif tools or mcp:
            # MCP tools can request permission even when built-in tools are
            # disabled with ``--tools ""``. In non-interactive ``-p`` mode we
            # must still choose an explicit permission policy or the subprocess
            # can stall waiting for a prompt that our web UI cannot answer.
            permission_mode = str(
                config.CLAUDE_CODE_P_CONFIG.get("permission_mode") or "dontAsk"
            ).strip()
            if permission_mode:
                args += ["--permission-mode", permission_mode]
        return args

    @staticmethod
    def _child_env(options: dict[str, Any] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        # Subscription-only lane. Claude Code auth precedence puts cloud routes,
        # bearer/API credentials and custom base URLs ahead of `/login` OAuth.
        # Scrub those inherited knobs so a half-configured proxy or old shell
        # cannot silently change the billing/provider path. Keep
        # CLAUDE_CODE_OAUTH_TOKEN: it is itself a subscription OAuth credential.
        for name in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
            "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_FOUNDRY_BASE_URL",
            "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_API_KEY",
            "ANTHROPIC_AWS_BASE_URL", "ANTHROPIC_AWS_API_KEY",
        ):
            env.pop(name, None)
        requested = options or {}
        max_output = int(requested.get("max_output_tokens") or 0)
        if max_output > 0:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max(1024, min(max_output, 131072)))
        return env

    async def _assert_subscription_auth(self) -> dict[str, Any]:
        """Verify the effective Claude Code credential is first-party OAuth.

        This catches a user-level ``apiKeyHelper`` too, which cannot be neutralized
        safely by deleting environment variables. The check is cached briefly so
        normal chat turns do not spawn a status command every time.
        """
        ttl = int(config.CLAUDE_CODE_P_CONFIG.get("auth_check_ttl_seconds") or 60)
        now = time.monotonic()
        if self._auth_status and now - self._auth_checked_at < ttl:
            return dict(self._auth_status)
        binary = self.binary_path()
        if not binary:
            raise ClaudeCodePError(
                "未找到 Claude Code CLI。请先安装 Claude Code 并运行 claude auth login。",
                code="claude_code_cli_missing",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "auth", "status",
                cwd=str(config.CLAUDE_CODE_P_CONFIG.get("cwd") or config.BASE_DIR),
                env=self._child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise ClaudeCodePError(
                "Claude Code 登录状态检查超时。", code="claude_code_auth_check_failed"
            ) from exc
        except OSError as exc:
            raise ClaudeCodePError(
                "Claude Code 登录状态无法检查。", code="claude_code_auth_check_failed"
            ) from exc
        text = stdout.decode("utf-8", errors="replace").strip()
        try:
            status = json.loads(text) if text else {}
        except json.JSONDecodeError:
            status = {}
        if proc.returncode != 0 or not bool(status.get("loggedIn")):
            detail = " ".join(stderr.decode("utf-8", errors="replace").split())[-300:]
            raise ClaudeCodePError(
                "Claude Code P 模式没有可用的订阅登录。请先运行 claude auth login。"
                + (f" {detail}" if detail else ""),
                code="claude_code_not_logged_in",
            )
        auth_method = str(status.get("authMethod") or "").lower()
        api_provider = str(status.get("apiProvider") or "").lower()
        subscription_auth = (
            "oauth" in auth_method
            or auth_method in {"claude.ai", "claudeai", "claude_ai"}
        )
        if not subscription_auth or api_provider not in {"", "firstparty", "first_party"}:
            raise ClaudeCodePError(
                "Claude Code 当前不是 Claude 订阅 OAuth / first-party 认证；P 模式已阻止启动，避免误走 API Key、apiKeyHelper 或云厂商计费。请运行 claude auth status 核对后再试。",
                code="claude_code_not_subscription_auth",
            )
        self._auth_checked_at = now
        self._auth_status = {
            "loggedIn": True, "authMethod": auth_method,
            "apiProvider": api_provider or "firstparty",
            "subscriptionType": status.get("subscriptionType"),
        }
        return dict(self._auth_status)

    async def _stdout_reader(self, state: _ProcessState) -> None:
        assert state.process.stdout is not None
        try:
            while True:
                raw = await state.process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    await state.queue.put({"type": "_malformed", "raw": line[:1000]})
                    continue
                await state.queue.put(event)
        finally:
            await state.queue.put({"type": "_eof", "returncode": state.process.returncode})

    async def _stderr_reader(self, state: _ProcessState) -> None:
        assert state.process.stderr is not None
        limit = int(config.CLAUDE_CODE_P_CONFIG.get("stderr_tail_chars") or 12000)
        while True:
            raw = await state.process.stderr.read(2048)
            if not raw:
                return
            state.stderr_tail = (state.stderr_tail + raw.decode("utf-8", errors="replace"))[-limit:]

    async def _run_round(
        self,
        state: _ProcessState,
        *,
        content: Any,
        emit: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        state.last_used_at = time.monotonic()
        await self._write_user_event(state, content)
        text_seen = False
        reasoning_seen = False
        final_text = ""
        timeout = int(config.CLAUDE_CODE_P_CONFIG.get("round_timeout_seconds") or 600)
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                event = await asyncio.wait_for(state.queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise ClaudeCodePError(
                    "Claude Code P 模式本轮超时。",
                    code="claude_code_round_timeout",
                ) from exc
            etype = str(event.get("type") or "")
            subtype = str(event.get("subtype") or "")

            if etype == "_eof":
                # Let the stderr reader drain CLI option/login errors before we
                # decide whether this launch can be retried without a hidden flag.
                try:
                    await asyncio.wait_for(asyncio.shield(state.stderr_task), timeout=0.25)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                detail = self._safe_stderr(state)
                raise ClaudeCodePError(
                    "Claude Code P 模式进程提前退出。" + (f" {detail}" if detail else ""),
                    code="claude_code_process_exited",
                )
            if etype == "_malformed":
                # NDJSON parsing discipline: one malformed line does not corrupt
                # the tail buffer or the entire session; keep waiting for result.
                continue

            if etype == "system" and subtype == "init":
                returned = str(event.get("session_id") or "").strip()
                if returned:
                    state.cc_session_id = returned
                state.native_session_started = True
                self._mark_native_started(state)
                continue
            if etype == "system" and subtype == "api_retry":
                if emit:
                    error = str(event.get("error") or "retry")
                    attempt = event.get("attempt")
                    yield {
                        "type": "status",
                        "stage": "claude_code_retry",
                        "label": f"Claude Code 正在重试（{error}，第 {attempt or '?'} 次）…",
                    }
                continue
            if etype == "system" and subtype == "compact_boundary":
                if emit:
                    yield {
                        "type": "status",
                        "stage": "claude_code_compact",
                        "label": "Claude Code 已自动整理长期上下文…",
                    }
                continue
            if etype == "stream_event":
                nested = event.get("event") if isinstance(event.get("event"), dict) else {}
                delta = nested.get("delta") if isinstance(nested.get("delta"), dict) else {}
                dtype = str(delta.get("type") or "")
                if dtype == "text_delta":
                    chunk = str(delta.get("text") or "")
                    if chunk:
                        text_seen = True
                        final_text += chunk
                        if emit:
                            yield {"type": "text", "text": chunk}
                elif dtype == "thinking_delta":
                    chunk = str(delta.get("thinking") or delta.get("text") or "")
                    if chunk and state.show_thinking:
                        reasoning_seen = True
                        if emit:
                            yield {"type": "thinking", "text": chunk}
                continue
            if etype == "assistant":
                message = event.get("message") if isinstance(event.get("message"), dict) else event
                content_blocks = message.get("content") if isinstance(message, dict) else None
                complete_text, complete_reasoning, tool_names = self._assistant_blocks(content_blocks)
                if complete_text and not text_seen:
                    final_text = complete_text
                    text_seen = True
                    if emit:
                        yield {"type": "text", "text": complete_text}
                if complete_reasoning and not reasoning_seen and emit and state.show_thinking:
                    reasoning_seen = True
                    yield {"type": "thinking", "text": complete_reasoning}
                if tool_names and emit:
                    yield {
                        "type": "status",
                        "stage": "claude_code_tools",
                        "label": "Claude Code 正在使用工具：" + "、".join(tool_names[:4]),
                    }
                continue
            if etype == "result":
                # Current Agent SDK/Claude Code can emit synthetic result events
                # for background-task notifications. They do not terminate the
                # user's active round and must never be mistaken for its answer.
                origin = event.get("origin") if isinstance(event.get("origin"), dict) else {}
                if str(origin.get("kind") or "") == "task-notification":
                    if emit:
                        yield {
                            "type": "status",
                            "stage": "claude_code_background_task",
                            "label": "Claude Code 后台任务已更新…",
                        }
                    continue
                usage = self._parse_usage(event)
                result_text = str(event.get("result") or "")
                if result_text and not text_seen and not final_text:
                    final_text = result_text
                    if emit:
                        yield {"type": "text", "text": result_text}
                terminal_reason = str(
                    event.get("terminal_reason")
                    or event.get("stop_reason")
                    or ""
                ).strip().lower()
                failed = bool(event.get("is_error")) or subtype != "success"
                if terminal_reason in {
                    "aborted", "cancelled", "canceled", "error", "failed"
                }:
                    failed = True
                if failed:
                    error_code = (
                        subtype
                        if subtype and subtype != "success"
                        else terminal_reason or "error_during_execution"
                    )
                    details: list[str] = []
                    if event.get("error"):
                        details.append(str(event.get("error")))
                    if isinstance(event.get("errors"), list):
                        details.extend(str(item) for item in event.get("errors") if item)
                    detail = " · ".join(details)[:500]
                    raise ClaudeCodePError(
                        f"Claude Code 本轮失败：{error_code}" + (f" · {detail}" if detail else ""),
                        code=f"claude_code_{error_code}",
                        usage=usage,
                        native_envelope={
                            "api_family": "claude_code_stream_json",
                            "session_id": state.cc_session_id,
                            "num_turns": event.get("num_turns"),
                        },
                        terminal_reason=terminal_reason,
                    )
                state.last_result_text = final_text or result_text
                state.last_used_at = time.monotonic()
                state.native_session_started = True
                state.round_inflight = False
                state.pending_canonical_commit = not state.local_session_id.startswith(
                    "helper:"
                )
                if state.pending_canonical_commit:
                    state.pending_commit_token = str(uuid.uuid4())
                    state.canonical_commit_event.clear()
                else:
                    state.pending_commit_token = ""
                self._mark_native_started(state)
                if emit:
                    yield {
                        "type": "done",
                        "usage": usage,
                        "reasoning_content": "",
                        "native_envelope": {
                            "api_family": "claude_code_stream_json",
                            "session_id": state.cc_session_id,
                            "num_turns": event.get("num_turns"),
                            "generation": state.generation,
                            "canonical_commit_token": state.pending_commit_token,
                        },
                    }
                return

    async def _write_user_event(self, state: _ProcessState, content: Any) -> None:
        if state.process.returncode is not None or state.process.stdin is None:
            raise ClaudeCodePError(
                "Claude Code P 模式进程已经退出。",
                code="claude_code_process_exited",
            )
        payload = {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        limit = int(config.CLAUDE_CODE_P_CONFIG.get("stdin_max_bytes") or 9_500_000)
        if len(raw) > limit:
            raise ClaudeCodePError(
                f"P 模式单条 stream-json 输入超过安全上限（{len(raw):,} bytes）。",
                code="claude_code_input_too_large",
            )
        state.process.stdin.write(raw)
        try:
            await state.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClaudeCodePError(
                "Claude Code P 模式 stdin 已断开。",
                code="claude_code_process_exited",
            ) from exc

    async def _drain_round(self, state: _ProcessState, *, content: Any) -> None:
        async for _ in self._run_round(state, content=content, emit=False):
            pass

    async def _collect(
        self,
        generator: AsyncGenerator[dict[str, Any], None],
        *,
        model: str,
    ) -> dict[str, Any]:
        text = ""
        reasoning = ""
        usage: dict[str, Any] = {}
        native = None
        async for chunk in generator:
            if chunk.get("type") == "text":
                text += str(chunk.get("text") or "")
            elif chunk.get("type") == "thinking":
                reasoning += str(chunk.get("text") or "")
            elif chunk.get("type") == "done":
                usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else {}
                native = chunk.get("native_envelope")
        return {
            "content": text,
            "reasoning_content": reasoning,
            "usage": usage,
            "provider": "claude_code_p",
            "model": model,
            "native_envelope": native,
        }

    # ------------------------------------------------------------------
    # Event / usage normalization
    # ------------------------------------------------------------------
    @staticmethod
    def _assistant_blocks(blocks: Any) -> tuple[str, str, list[str]]:
        if not isinstance(blocks, list):
            return "", "", []
        texts: list[str] = []
        thinking: list[str] = []
        tools: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype == "text":
                texts.append(str(block.get("text") or ""))
            elif btype == "thinking":
                thinking.append(str(block.get("thinking") or ""))
            elif btype in {"tool_use", "server_tool_use"}:
                tools.append(str(block.get("name") or "tool"))
        return "".join(texts), "".join(thinking), tools

    @staticmethod
    def _parse_usage(event: dict[str, Any]) -> dict[str, Any]:
        raw = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        def iv(*names: str) -> int:
            for name in names:
                try:
                    if name in raw:
                        return max(0, int(raw.get(name) or 0))
                except (TypeError, ValueError):
                    pass
            return 0
        estimated = event.get("total_cost_usd")
        try:
            estimated_value = max(0.0, float(estimated or 0.0))
        except (TypeError, ValueError):
            estimated_value = 0.0
        return {
            "input_tokens": iv("input_tokens"),
            "output_tokens": iv("output_tokens"),
            "reasoning_tokens": 0,
            "cache_read": iv("cache_read_input_tokens", "cache_read"),
            "cache_creation": iv("cache_creation_input_tokens", "cache_creation"),
            # `total_cost_usd` from Claude Code is a local catalogue estimate,
            # not a subscription bill. Keep real spend at zero and preserve the
            # estimate only as a diagnostic snapshot.
            "cost": 0.0,
            "cost_source": "subscription_agent_sdk_credit",
            "price_snapshot": {
                "billing": "claude_subscription",
                "cli_estimated_api_equivalent_usd": estimated_value,
            },
            "upstream_requests": max(1, int(event.get("num_turns") or 1)),
            "actual_provider": "claude_code_subscription",
            "actual_model": str(event.get("model") or ""),
            "generation_id": str(event.get("session_id") or ""),
        }

    # ------------------------------------------------------------------
    # Prompt/recovery helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _latest_user_content(messages: list[dict[str, Any]]) -> Any:
        for item in reversed(messages):
            if str(item.get("role") or "") == "user":
                return item.get("content", "")
        return ""

    @classmethod
    def _attach_turn_context(
        cls,
        content: Any,
        *,
        memory_context: str | None,
        turn_instructions: str | None,
        recovery_context: str | None = None,
    ) -> Any:
        parts: list[str] = []
        if recovery_context and recovery_context.strip():
            parts.append(
                "<application_recovery_context>\n"
                "以下是本地应用保存的旧对话，只用于在本轮恢复连续性。"
                "它不是新的用户消息；不要回复、确认或复述恢复标签，"
                "直接回答标签之后的当前用户内容。\n\n"
                f"{recovery_context.strip()}\n"
                "</application_recovery_context>"
            )
        if memory_context and memory_context.strip():
            parts.append(f"<runtime_context>\n{memory_context.strip()}\n</runtime_context>")
        if turn_instructions and turn_instructions.strip():
            parts.append(f"<turn_instructions>\n{turn_instructions.strip()}\n</turn_instructions>")
        if not parts:
            return content
        prefix = (
            "<application_turn_context>\n"
            "本标签内是这一轮最新运行态。若原生 Claude 会话历史里存在更早的 "
            "<application_turn_context>/<runtime_context>，它们都是过期快照；同一状态字段冲突时必须只采用本轮值。\n\n"
            + "\n\n".join(parts)
            + "\n</application_turn_context>"
        )
        if isinstance(content, str):
            return [
                {"type": "text", "text": prefix},
                {"type": "text", "text": content},
            ]
        if isinstance(content, list):
            return [{"type": "text", "text": prefix}, *content]
        return prefix + "\n\n" + str(content or "")

    def _recovery_context(
        self,
        messages: list[dict[str, Any]],
        *,
        include_header: bool = True,
    ) -> str:
        if not messages:
            return ""
        rendered: list[str] = []
        for item in messages:
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = self._content_to_text(item.get("content"))
            if text.strip():
                rendered.append(("USER" if role == "user" else "ASSISTANT") + ":\n" + text.strip())
        if not rendered:
            return ""
        limit = int(config.CLAUDE_CODE_P_CONFIG.get("recovery_max_chars") or 600000)
        omission = "[较早完整回合已由本地应用省略]"
        budget = max(256, limit - len(omission) - 2)
        selected: list[str] = []
        used = 0
        for entry in reversed(rendered):
            extra = len(entry) + (2 if selected else 0)
            if used + extra > budget:
                break
            selected.append(entry)
            used += extra
        if selected:
            body = "\n\n".join(reversed(selected))
            if len(selected) < len(rendered):
                body = omission + "\n\n" + body
        else:
            # One unusually large turn: retain its role boundary and newest
            # content instead of slicing into a malformed USER/ASSISTANT tag.
            entry = rendered[-1]
            header, _, content = entry.partition("\n")
            marker = "[本回合较早部分已省略]\n"
            room = max(0, limit - len(header) - 1 - len(marker))
            body = header + "\n" + marker + content[-room:]
        if include_header:
            return "[本地会话历史恢复]\n" + body
        return body

    @classmethod
    def _content_to_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in {"text", "input_text"}:
                parts.append(str(block.get("text") or ""))
            elif btype in {"image", "input_image"}:
                parts.append("[图片：原图仅在当轮发送，恢复历史时不重复嵌入二进制]")
            elif btype in {"document", "input_file"}:
                parts.append("[文件：恢复历史使用本地已提取文字，不重复嵌入二进制]")
        return "\n".join(part for part in parts if part)

    def _persona_file(self, text: str) -> tuple[Path, str]:
        digest = self._hash_text(text)
        self._persona_dir.mkdir(parents=True, exist_ok=True)
        path = self._persona_dir / f"persona-{digest[:20]}.md"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        return path, digest

    # ------------------------------------------------------------------
    # Persistent metadata only (no message text / no credentials)
    # ------------------------------------------------------------------
    def _load_persisted_state(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _rotate_uncommitted_record(self, local_session_id: str, *, reason: str) -> None:
        """Discard a native transcript whose round boundary or commit is unknown."""
        sid = str(local_session_id or "")
        record = dict(self._persisted.get(sid) or {})
        generation = int(record.get("generation") or 0) + 1
        self._persisted[sid] = {
            "generation": generation,
            "cc_session_id": self._cc_session_id(sid, generation),
            "native_session_started": False,
            "round_inflight": False,
            "pending_canonical_commit": False,
            "pending_commit_token": "",
            "model": "",
            "system_hash": "",
            "spawn_hash": "",
            "reason": str(reason or "uncommitted_native_round")[:120],
            "updated_at": int(time.time()),
        }
        self._save_persisted_state()

    def _save_persisted_state(self) -> None:
        """Atomically persist P-mode safety metadata or fail closed.

        Silently swallowing this write is unsafe: after a native user event or
        result, a crash could otherwise resume a transcript whose local commit
        boundary was never recorded.
        """
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._persisted, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            raise ClaudeCodePError(
                "P 模式无法持久化原生回合安全状态，已停止继续使用该会话。",
                code="claude_code_state_persist_failed",
            ) from exc

    def _mark_native_started(self, state: _ProcessState) -> None:
        if state.local_session_id.startswith("helper:"):
            return
        self._persisted[state.local_session_id] = {
            "generation": state.generation,
            "cc_session_id": state.cc_session_id,
            "native_session_started": True,
            "round_inflight": bool(state.round_inflight),
            "model": state.model,
            "system_hash": state.system_hash,
            "spawn_hash": state.spawn_hash,
            "pending_canonical_commit": bool(state.pending_canonical_commit),
            "pending_commit_token": str(state.pending_commit_token or ""),
            "updated_at": int(time.time()),
        }
        self._save_persisted_state()

    def _begin_native_round(self, state: _ProcessState) -> None:
        """Persist the uncertainty window before writing a native user event."""
        state.native_session_started = True
        state.round_inflight = True
        state.pending_canonical_commit = False
        state.pending_commit_token = ""
        state.canonical_commit_event.set()
        self._mark_native_started(state)

    @classmethod
    def _cc_session_id(cls, local_session_id: str, generation: int) -> str:
        return str(uuid.uuid5(cls._UUID_NAMESPACE, f"{local_session_id}:{int(generation)}"))

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _looks_like_resume_failure(stderr: str) -> bool:
        lowered = str(stderr or "").lower()
        return any(marker in lowered for marker in (
            "resume", "session not found", "no session", "conversation not found",
            "could not find session", "unknown session",
        ))

    def _retryable_launch_failure(self, state: _ProcessState, exc: ClaudeCodePError) -> bool:
        if exc.code != "claude_code_process_exited":
            return False
        stderr = state.stderr_tail.lower()
        return (
            "thinking-display" in stderr
            or self._looks_like_resume_failure(stderr)
        )

    @staticmethod
    def _safe_stderr(state: _ProcessState) -> str:
        text = " ".join(str(state.stderr_tail or "").split())
        # Never echo environment values or arbitrary long process output.
        return text[-800:]

    async def _terminate_state(self, state: _ProcessState) -> None:
        proc = state.process
        if proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        for task in (state.stdout_task, state.stderr_task):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (state.stdout_task, state.stderr_task) if task is not asyncio.current_task()),
            return_exceptions=True,
        )


claude_code_p = ClaudeCodePManager()
