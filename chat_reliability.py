"""Idempotent chat request ledger used by the API and mobile web client."""
from __future__ import annotations

import json
import re
import asyncio
from dataclasses import dataclass

from models import get_db


CLIENT_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
TERMINAL_STATUSES = frozenset({"completed", "interrupted", "failed", "blocked"})


class InvalidClientRequestId(ValueError):
    pass


class RequestIdentityConflict(ValueError):
    pass


@dataclass(frozen=True)
class ClaimResult:
    created: bool
    request: dict


def normalize_client_request_id(value: object, *, create_if_missing: bool = False) -> str:
    text = str(value or "").strip()
    if not text and create_if_missing:
        import uuid

        text = uuid.uuid4().hex
    if not CLIENT_REQUEST_ID_RE.fullmatch(text):
        raise InvalidClientRequestId(
            "client_request_id 必须是 16—128 位字母、数字、下划线或连字符"
        )
    return text


class ChatRequestStore:
    """Small database service; it never duplicates message content."""

    public_columns = (
        "client_request_id", "session_id", "provider", "model", "status",
        "trace_id", "user_message_id", "assistant_message_id",
        "cancel_requested", "error_code", "created_at", "updated_at",
        "completed_at",
    )

    def claim(
        self,
        client_request_id: str,
        *,
        session_id: str,
        provider: str,
        model: str,
        trace_id: str,
    ) -> ClaimResult:
        request_id = normalize_client_request_id(client_request_id)
        with get_db() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO chat_requests
                   (client_request_id, session_id, provider, model, status, trace_id)
                   VALUES (?, ?, ?, ?, 'processing', ?)""",
                (request_id, session_id, provider, model, trace_id),
            )
            row = db.execute(
                "SELECT * FROM chat_requests WHERE client_request_id=?",
                (request_id,),
            ).fetchone()
        if not row:
            raise RuntimeError("聊天请求登记失败")
        item = self._public(dict(row))
        if str(item["session_id"]) != str(session_id):
            raise RequestIdentityConflict("这个请求编号已经属于另一个会话")
        return ClaimResult(created=bool(cursor.rowcount), request=item)

    def attach_user_message(self, client_request_id: str, message_id: int) -> None:
        self._update(
            client_request_id,
            "user_message_id=?, updated_at=datetime('now')",
            (int(message_id),),
        )

    def complete(self, client_request_id: str, assistant_message_id: int) -> None:
        self._update(
            client_request_id,
            """status='completed', assistant_message_id=?, error_code='',
               updated_at=datetime('now'), completed_at=datetime('now')""",
            (int(assistant_message_id),),
        )

    def interrupt(
        self,
        client_request_id: str,
        *,
        assistant_message_id: int | None = None,
        error_code: str = "client_disconnected",
    ) -> None:
        self._update(
            client_request_id,
            """status='interrupted', assistant_message_id=COALESCE(?, assistant_message_id),
               error_code=?, updated_at=datetime('now'), completed_at=datetime('now')""",
            (assistant_message_id, str(error_code or "interrupted")[:80]),
        )

    def fail(self, client_request_id: str, error_code: str) -> None:
        self._update(
            client_request_id,
            """status='failed', error_code=?, updated_at=datetime('now'),
               completed_at=datetime('now')""",
            (str(error_code or "request_failed")[:80],),
        )

    def block(self, client_request_id: str, assistant_message_id: int | None = None) -> None:
        self._update(
            client_request_id,
            """status='blocked', assistant_message_id=COALESCE(?, assistant_message_id),
               error_code='relational_honesty_blocked', updated_at=datetime('now'),
               completed_at=datetime('now')""",
            (assistant_message_id,),
        )

    def request_cancel(self, client_request_id: str) -> dict | None:
        request_id = normalize_client_request_id(client_request_id)
        with get_db() as db:
            db.execute(
                """UPDATE chat_requests SET cancel_requested=1,
                   updated_at=datetime('now')
                   WHERE client_request_id=? AND status='processing'""",
                (request_id,),
            )
            row = db.execute(
                "SELECT * FROM chat_requests WHERE client_request_id=?",
                (request_id,),
            ).fetchone()
        return self._public(dict(row)) if row else None

    def is_cancel_requested(self, client_request_id: str) -> bool:
        request_id = normalize_client_request_id(client_request_id)
        with get_db() as db:
            row = db.execute(
                "SELECT cancel_requested FROM chat_requests WHERE client_request_id=?",
                (request_id,),
            ).fetchone()
        return bool(row and int(row["cancel_requested"] or 0))

    def get(self, client_request_id: str) -> dict | None:
        request_id = normalize_client_request_id(client_request_id)
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM chat_requests WHERE client_request_id=?",
                (request_id,),
            ).fetchone()
        return self._public(dict(row)) if row else None

    def replay_payload(self, client_request_id: str) -> dict:
        item = self.get(client_request_id)
        if not item:
            return {}
        message = None
        message_id = item.get("assistant_message_id")
        if message_id:
            with get_db() as db:
                row = db.execute(
                    "SELECT id, content, metadata FROM messages WHERE id=? AND role='assistant'",
                    (int(message_id),),
                ).fetchone()
            if row:
                message = dict(row)
                try:
                    message["metadata"] = json.loads(message.get("metadata") or "{}")
                except Exception:
                    message["metadata"] = {}
        return {"request": item, "assistant_message": message}

    def recover_stale_processing(self) -> int:
        """A process restart cannot still own an old in-flight model stream."""
        with get_db() as db:
            cursor = db.execute(
                """UPDATE chat_requests
                   SET status='interrupted', error_code='server_restarted',
                       updated_at=datetime('now'), completed_at=datetime('now')
                   WHERE status='processing'"""
            )
        return max(0, int(cursor.rowcount or 0))

    def _update(self, client_request_id: str, clause: str, params: tuple) -> None:
        request_id = normalize_client_request_id(client_request_id)
        with get_db() as db:
            cursor = db.execute(
                f"UPDATE chat_requests SET {clause} WHERE client_request_id=?",
                (*params, request_id),
            )
        if not cursor.rowcount:
            raise KeyError(request_id)

    def _public(self, row: dict) -> dict:
        result = {key: row.get(key) for key in self.public_columns}
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        result["terminal"] = str(result.get("status") or "") in TERMINAL_STATUSES
        return result


chat_request_store = ChatRequestStore()


class ChatRequestRuntime:
    """Keep server-side producers alive when a mobile SSE connection detaches."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def register(self, client_request_id: str, task: asyncio.Task) -> None:
        self._tasks[normalize_client_request_id(client_request_id)] = task

    def unregister(self, client_request_id: str, task: asyncio.Task | None = None) -> None:
        request_id = normalize_client_request_id(client_request_id)
        current = self._tasks.get(request_id)
        if task is None or current is task:
            self._tasks.pop(request_id, None)

    def cancel(self, client_request_id: str) -> bool:
        request_id = normalize_client_request_id(client_request_id)
        task = self._tasks.get(request_id)
        if not task or task.done():
            return False
        task.cancel()
        return True

    def active(self, client_request_id: str) -> bool:
        request_id = normalize_client_request_id(client_request_id)
        task = self._tasks.get(request_id)
        return bool(task and not task.done())


chat_request_runtime = ChatRequestRuntime()


async def resilient_request_events(source, client_request_id: str):
    """Detach the model producer from one fragile browser connection.

    A suspended iPhone can drop the SSE consumer while the producer finishes
    and writes the answer. Explicit cancellation still cancels the registered
    producer through the reliability API.
    """
    request_id = normalize_client_request_id(client_request_id)
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    consumer_detached = asyncio.Event()

    async def pump() -> None:
        task = asyncio.current_task()
        try:
            async for item in source:
                # After Safari/iOS drops the SSE socket there is deliberately no
                # live consumer. Keep draining the model/source so persistence
                # completes, but stop retaining an unbounded copy of every SSE
                # chunk in RAM; recovery comes from the durable request ledger.
                if not consumer_detached.is_set():
                    await queue.put(item)
        except asyncio.CancelledError:
            try:
                current = chat_request_store.get(request_id)
            except Exception:
                current = None
            if current and current.get("status") == "processing":
                try:
                    chat_request_store.interrupt(
                        request_id,
                        error_code=(
                            "user_cancelled" if current.get("cancel_requested")
                            else "server_shutdown"
                        ),
                    )
                except Exception:
                    pass
            raise
        except Exception:
            try:
                current = chat_request_store.get(request_id)
            except Exception:
                current = None
            if current and current.get("status") == "processing":
                try:
                    chat_request_store.fail(request_id, "stream_producer_failed")
                except Exception:
                    pass
            try:
                current = chat_request_store.get(request_id) or {}
            except Exception:
                current = {}
            if not consumer_detached.is_set():
                await queue.put(
                    f"data: {json.dumps({'type': 'error', 'error': '聊天收尾发生本机错误，已保留你发出的消息。'}, ensure_ascii=False)}\n\n"
                )
                await queue.put(
                    f"data: {json.dumps({'type': 'request_state', **current}, ensure_ascii=False)}\n\n"
                )
                await queue.put("data: [DONE]\n\n")
        finally:
            if not consumer_detached.is_set():
                await queue.put(None)
            chat_request_runtime.unregister(request_id, task)

    producer = asyncio.create_task(pump(), name=f"chat:{request_id}")
    chat_request_runtime.register(request_id, producer)
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    except asyncio.CancelledError:
        # Deliberately leave the producer alive. The request ledger becomes the
        # reconnection channel for this or another browser window. Mark this
        # consumer detached so the producer drops transport-only SSE chunks
        # instead of buffering them forever after a mobile disconnect.
        consumer_detached.set()
        raise
    finally:
        # ASGI servers may close an async response generator with GeneratorExit
        # rather than CancelledError. Normal completion reaches here only after
        # the producer is done; every other close is transport-only detachment.
        if not producer.done():
            consumer_detached.set()


async def replay_request_events(client_request_id: str):
    """Return one already-owned request as SSE without creating another turn."""
    payload = chat_request_store.replay_payload(client_request_id)
    request = payload.get("request") or {}
    message = payload.get("assistant_message")

    def event(value: dict) -> str:
        return f"data: {json.dumps(value, ensure_ascii=False)}\n\n"

    yield event({
        "type": "request",
        "request_id": request.get("trace_id") or "",
        "client_request_id": request.get("client_request_id") or client_request_id,
        "user_message_id": request.get("user_message_id"),
        "replayed": True,
    })
    yield event({
        "type": "recovery",
        "status": request.get("status") or "processing",
        "client_request_id": request.get("client_request_id") or client_request_id,
    })
    if message:
        content = str(message.get("content") or "")
        if content:
            yield event({"type": "text", "text": content})
        metadata = message.get("metadata") or {}
        sticker = metadata.get("sticker") if isinstance(metadata, dict) else None
        if isinstance(sticker, dict):
            yield event({"type": "sticker", "sticker": sticker})
        yield event({
            "type": "message_saved",
            "message_id": message.get("id"),
            "role": "assistant",
        })
    if request.get("status") == "failed":
        yield event({
            "type": "error",
            "error": "这次请求没有完成，已保留你发出的消息。",
            "request_id": request.get("trace_id") or "",
        })
    yield event({"type": "request_state", **request})
    yield "data: [DONE]\n\n"
