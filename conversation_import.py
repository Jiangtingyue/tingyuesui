"""Source-faithful conversation and relationship-memory import adapters.

Visible user/assistant text is restored as ordinary history.  Reasoning that a
provider export *explicitly contains* is preserved in the separate local
thinking vault, while tool payloads and hidden envelopes remain excluded.  A
restored thought is therefore viewable by the owner but is never replayed to a
different model as chat history.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from memory_archive import memory_archive
from models import get_db
from thinking_vault import thinking_vault


MAX_CONVERSATIONS = 1000
MAX_MESSAGES = 150_000
MAX_TOTAL_CHARS = 80_000_000
VISIBLE_ROLES = {"user", "assistant"}


def _iso_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return text[:80]


def _role(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return {
        "human": "user", "you": "user", "owner": "user",
        "model": "assistant", "ai": "assistant", "bot": "assistant",
        "chatgpt": "assistant", "claude": "assistant", "gemini": "assistant",
    }.get(raw, raw)


def _flatten_text(value: Any, *, depth: int = 0) -> str:
    if depth > 8 or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(
            part for part in (_flatten_text(item, depth=depth + 1) for item in value)
            if part.strip()
        )
    if not isinstance(value, dict):
        return ""

    # Explicitly ignore hidden/provider-private fields. They may be useful to
    # the source API but must not become visible history for Claude/GPT.
    block_type = str(
        value.get("type") or value.get("content_type") or value.get("kind") or ""
    ).strip().lower().replace("-", "_")
    private_types = {
        "thinking", "redacted_thinking", "reasoning", "reasoning_text",
        "tool", "tool_use", "tool_result", "function", "function_call",
        "function_result", "computer", "computer_use", "computer_result",
        "web_search", "web_search_result",
    }
    if (
        value.get("thought") is True
        or block_type in private_types
        or block_type.startswith(("tool_", "function_", "reasoning_", "thinking_"))
    ):
        return ""
    for key in ("text", "output_text", "result", "message"):
        if key in value and isinstance(value[key], str):
            return value[key]
    for key in ("parts", "content", "segments", "chunks"):
        if key in value:
            return _flatten_text(value[key], depth=depth + 1)
    return ""


def _plain_reasoning_text(value: Any, *, depth: int = 0) -> str:
    """Flatten text only after its container was positively identified as thought."""
    if depth > 8 or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(
            part for part in (
                _plain_reasoning_text(item, depth=depth + 1) for item in value
            ) if part.strip()
        )
    if not isinstance(value, dict):
        return ""
    for key in ("text", "thinking", "reasoning", "reasoning_content", "content"):
        if key in value:
            found = _plain_reasoning_text(value[key], depth=depth + 1)
            if found.strip():
                return found
    return ""


def _flatten_reasoning(value: Any, *, depth: int = 0) -> str:
    """Return only thought text explicitly marked by the source export."""
    if depth > 8 or value is None:
        return ""
    if isinstance(value, list):
        parts = [_flatten_reasoning(item, depth=depth + 1) for item in value]
        return "\n\n".join(part for part in parts if part.strip())
    if not isinstance(value, dict):
        return ""

    block_type = str(
        value.get("type") or value.get("content_type") or value.get("kind") or ""
    ).strip().lower().replace("-", "_")
    if block_type == "redacted_thinking" or block_type.startswith(("tool_", "function_")):
        return ""

    parts: list[str] = []
    explicit_block = (
        value.get("thought") is True
        or block_type in {"thinking", "reasoning", "reasoning_text"}
        or block_type.startswith(("reasoning_", "thinking_"))
    )
    if explicit_block:
        found = _plain_reasoning_text(value, depth=depth + 1).strip()
        if found:
            parts.append(found)
    else:
        # These field names are provider-level declarations, even when the
        # surrounding message has no typed content blocks.
        for key in ("reasoning_content", "thinking"):
            if key in value:
                found = _plain_reasoning_text(value[key], depth=depth + 1).strip()
                if found:
                    parts.append(found)
        for key in ("content", "parts", "segments", "chunks", "message"):
            if key in value:
                found = _flatten_reasoning(value[key], depth=depth + 1).strip()
                if found:
                    parts.append(found)

    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        marker = part.strip()
        if marker and marker not in seen:
            seen.add(marker)
            unique.append(marker)
    return "\n\n".join(unique)


def _message(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    author = value.get("author")
    if isinstance(author, dict):
        author = author.get("role") or author.get("name")
    role = _role(
        value.get("role") or value.get("sender") or value.get("speaker")
        or value.get("author_role") or author
    )
    if role not in VISIBLE_ROLES:
        return None
    content = _flatten_text(
        value.get("content") if "content" in value else
        value.get("parts") if "parts" in value else
        value.get("text") if "text" in value else value.get("message")
    ).strip()
    if not content:
        return None
    reasoning = _flatten_reasoning(value).strip() if role == "assistant" else ""
    return {
        "role": role,
        "content": content,
        "created_at": _iso_time(
            value.get("created_at") or value.get("create_time")
            or value.get("timestamp") or value.get("time")
        ),
        "source_id": str(value.get("id") or value.get("uuid") or "")[:200],
        "reasoning": reasoning[: thinking_vault.MAX_CHARS],
    }


def _chatgpt_messages(item: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = item.get("mapping")
    if not isinstance(mapping, dict):
        return []
    current = item.get("current_node")
    ordered_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current and str(current) not in seen:
        seen.add(str(current))
        node = mapping.get(current) or mapping.get(str(current))
        if not isinstance(node, dict):
            break
        ordered_nodes.append(node)
        current = node.get("parent")
    ordered_nodes.reverse()
    if not ordered_nodes:
        # Missing current_node must not flatten every branch into one fake chat.
        # Pick one real leaf (latest visible leaf), then follow only its parents.
        parent_ids = {
            str(node.get("parent")) for node in mapping.values()
            if isinstance(node, dict) and node.get("parent")
        }
        leaves = [
            (str(node_id), node) for node_id, node in mapping.items()
            if isinstance(node, dict) and str(node_id) not in parent_ids
        ]
        def leaf_time(pair):
            node = pair[1]
            return float(((node.get("message") or {}).get("create_time") or 0))
        if leaves:
            current = max(leaves, key=leaf_time)[0]
            seen.clear()
            while current and str(current) not in seen:
                seen.add(str(current))
                node = mapping.get(current) or mapping.get(str(current))
                if not isinstance(node, dict):
                    break
                ordered_nodes.append(node)
                current = node.get("parent")
            ordered_nodes.reverse()
    result: list[dict[str, Any]] = []
    for node in ordered_nodes:
        parsed = _message(node.get("message"))
        if parsed:
            result.append(parsed)
    return result


def _message_list(item: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    if isinstance(item.get("mapping"), dict):
        return _chatgpt_messages(item), "chatgpt"
    for key, source in (
        ("chat_messages", "claude"),
        ("messages", "generic"),
        ("history", "generic"),
        ("turns", "generic"),
        ("contents", "gemini"),
    ):
        raw = item.get(key)
        if isinstance(raw, list):
            messages = [parsed for parsed in (_message(value) for value in raw) if parsed]
            if messages:
                if source == "generic":
                    roles = {str(value.get("sender") or "").lower() for value in raw if isinstance(value, dict)}
                    if "human" in roles:
                        source = "claude"
                return messages, source
    return [], "generic"


def _conversation(item: Any, index: int = 0) -> tuple[dict[str, Any] | None, str]:
    if isinstance(item, list):
        item = {"messages": item, "title": "导入的对话"}
    if not isinstance(item, dict):
        return None, "generic"
    messages, source = _message_list(item)
    if not messages:
        return None, source
    source_id = str(
        item.get("id") or item.get("uuid") or item.get("conversation_id")
        or item.get("chat_id") or ""
    )
    title = str(
        item.get("title") or item.get("name") or item.get("summary")
        or (messages[0]["content"][:36] if messages else f"导入对话 {index + 1}")
    ).strip()[:120] or f"导入对话 {index + 1}"
    provider = {
        "chatgpt": "openai", "claude": "anthropic", "gemini": "gemini",
    }.get(source, "imported")
    return {
        "source_id": source_id,
        "title": title,
        "created_at": _iso_time(
            item.get("created_at") or item.get("create_time") or item.get("timestamp")
        ) or messages[0].get("created_at", ""),
        "updated_at": _iso_time(
            item.get("updated_at") or item.get("update_time")
        ) or messages[-1].get("created_at", ""),
        "provider": provider,
        "model": str(item.get("model") or "")[:120],
        "messages": messages,
    }, source


def normalize_conversation_item(
    item: Any, index: int = 0,
) -> tuple[dict[str, Any] | None, str]:
    """Public, side-effect-free adapter used by the v6.1 stream importer."""
    return _conversation(item, index)


def conversation_fingerprint(conversation: dict[str, Any]) -> str:
    """Stable identity; source-less conversations hash the complete visible branch."""
    source_id = str(conversation.get("source_id") or "")
    if source_id:
        return source_id
    digest = hashlib.sha256()
    digest.update(str(conversation.get("title") or "").encode("utf-8", "ignore"))
    for item in conversation.get("messages") or []:
        digest.update(b"\x1e")
        digest.update(str(item.get("role") or "").encode("utf-8", "ignore"))
        digest.update(b"\x1f")
        digest.update(str(item.get("content") or "").encode("utf-8", "ignore"))
        digest.update(b"\x1f")
        digest.update(str(item.get("created_at") or "").encode("utf-8", "ignore"))
    return digest.hexdigest()


def imported_session_id(source_format: str, conversation: dict[str, Any]) -> str:
    namespace = uuid.UUID("0f06d733-dbb6-49d6-a769-7c5f64f5d749")
    return str(uuid.uuid5(namespace, f"{source_format}:{conversation_fingerprint(conversation)}"))


def _candidate_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        # A flat list of role-bearing messages is one conversation, while a
        # list of conversation objects contains one of the known containers.
        if data and all(
            isinstance(value, dict)
            and any(key in value for key in ("role", "sender", "speaker", "author"))
            for value in data[: min(8, len(data))]
        ):
            return [data]
        return data
    if not isinstance(data, dict):
        return []
    if any(key in data for key in ("mapping", "chat_messages", "messages", "history", "turns", "contents")):
        return [data]
    for key in ("conversations", "chats", "sessions", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def parse_conversation_bytes(filename: str, raw: bytes) -> dict[str, Any]:
    if not raw:
        raise ValueError("导入文件是空的")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise ValueError("文件必须是 UTF-8/GB18030 文本或 JSON") from exc
    suffix = str(filename or "").lower().rsplit(".", 1)[-1]
    if suffix not in {"json", "jsonl"}:
        return _parse_text_conversation(text, filename)
    if suffix == "jsonl":
        values = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {line_no} 行无法解析") from exc
        data: Any = values
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 无法解析：第 {exc.lineno} 行第 {exc.colno} 列") from exc

    conversations: list[dict[str, Any]] = []
    sources: list[str] = []
    total_messages = 0
    total_chars = 0
    reasoning_count = 0
    reasoning_chars = 0
    warnings: list[str] = []
    for index, item in enumerate(_candidate_items(data)):
        conversation, source = _conversation(item, index)
        if not conversation:
            continue
        count = len(conversation["messages"])
        chars = sum(len(message["content"]) for message in conversation["messages"])
        thought_messages = [
            message for message in conversation["messages"]
            if str(message.get("reasoning") or "").strip()
        ]
        thought_chars = sum(len(str(message.get("reasoning") or "")) for message in thought_messages)
        if len(conversations) >= MAX_CONVERSATIONS or total_messages + count > MAX_MESSAGES or total_chars + chars > MAX_TOTAL_CHARS:
            warnings.append("导出规模超过安全上限，后续对话已停止载入。")
            break
        conversations.append(conversation)
        sources.append(source)
        total_messages += count
        total_chars += chars
        reasoning_count += len(thought_messages)
        reasoning_chars += thought_chars
    if not conversations:
        raise ValueError("没有识别到可见的 user / assistant 对话；可把它作为“关系总记忆”导入")
    detected = max(set(sources), key=sources.count) if sources else "generic"
    return {
        "format": detected,
        "filename": str(filename or "import.json")[:180],
        "conversations": conversations,
        "conversation_count": len(conversations),
        "message_count": total_messages,
        "character_count": total_chars,
        "reasoning_count": reasoning_count,
        "reasoning_character_count": reasoning_chars,
        "warnings": warnings,
    }


_ROLE_LINE = re.compile(
    r"^\s*(user|you|human|用户|我|assistant|ai|bot|model|助手|claude|chatgpt|gemini)\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)


def _parse_text_conversation(text: str, filename: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    current_role = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        content = "\n".join(buffer).strip()
        if current_role in VISIBLE_ROLES and content:
            messages.append({"role": current_role, "content": content, "created_at": "", "source_id": ""})
        buffer = []

    for line in text.splitlines():
        match = _ROLE_LINE.match(line)
        if match:
            flush()
            label = match.group(1).lower()
            current_role = "assistant" if label in {"assistant", "ai", "bot", "model", "助手", "claude", "chatgpt", "gemini"} else "user"
            buffer = [match.group(2)] if match.group(2) else []
        elif current_role:
            buffer.append(line)
    flush()
    if not messages:
        raise ValueError("文本里没有识别到 User:/Assistant: 分隔；可把它作为“关系总记忆”导入")
    conversation = {
        "source_id": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:24],
        "title": str(filename or "导入的文本对话").rsplit(".", 1)[0][:120],
        "created_at": "", "updated_at": "", "provider": "imported", "model": "",
        "messages": messages,
    }
    return {
        "format": "text", "filename": filename, "conversations": [conversation],
        "conversation_count": 1, "message_count": len(messages),
        "character_count": sum(len(item["content"]) for item in messages),
        "reasoning_count": 0, "reasoning_character_count": 0, "warnings": [],
    }


def preview_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    conversations = bundle.get("conversations") or []
    return {
        key: bundle.get(key) for key in (
            "format", "filename", "conversation_count", "message_count",
            "character_count", "reasoning_count", "reasoning_character_count", "warnings",
        )
    } | {
        "samples": [
            {
                "title": item.get("title"),
                "messages": len(item.get("messages") or []),
                "preview": (item.get("messages") or [{}])[0].get("content", "")[:180],
            }
            for item in conversations[:5]
        ]
    }


def import_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    conversations = bundle.get("conversations") if isinstance(bundle, dict) else None
    if not isinstance(conversations, list) or not conversations:
        raise ValueError("没有可以导入的对话")
    imported_ids: list[str] = []
    skipped = 0
    inserted_messages = 0
    imported_reasoning = 0
    imported_reasoning_chars = 0
    now = datetime.now(timezone.utc).isoformat()
    thinking_vault.ensure_schema()
    with get_db() as db:
        for index, conversation in enumerate(conversations):
            messages = conversation.get("messages") or []
            if not messages:
                continue
            session_id = imported_session_id(
                str(bundle.get("format") or "generic"), conversation,
            )
            existing_session = db.execute(
                "SELECT id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            created_at = conversation.get("created_at") or messages[0].get("created_at") or now
            updated_at = conversation.get("updated_at") or messages[-1].get("created_at") or created_at
            title = str(conversation.get("title") or f"导入对话 {index + 1}")[:120]
            provider = str(conversation.get("provider") or "imported")[:40]
            model = str(conversation.get("model") or "")[:120]
            if not existing_session:
                db.execute(
                    """INSERT INTO sessions
                       (id, title, created_at, updated_at, provider, model, total_cost, message_count)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                    (session_id, title, created_at, updated_at, provider, model, len(messages)),
                )
            existing_source_ids: dict[str, tuple[int, dict[str, Any]]] = {}
            existing_signatures: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
            if existing_session:
                for old_message in db.execute(
                    "SELECT id, role, content, created_at, metadata FROM messages WHERE session_id=?",
                    (session_id,),
                ).fetchall():
                    try:
                        old_meta = json.loads(old_message["metadata"] or "{}")
                    except Exception:
                        old_meta = {}
                    source_mid = str(old_meta.get("source_message_id") or "")
                    if source_mid:
                        existing_source_ids[source_mid] = (int(old_message["id"]), old_meta)
                    existing_signatures[(str(old_message["role"] or ""), str(old_message["content"] or ""), str(old_message["created_at"] or ""))] = (int(old_message["id"]), old_meta)
            added_here = 0
            for message in messages:
                source_mid = str(message.get("source_id") or "")
                signature = (str(message.get("role") or ""), str(message.get("content") or ""), str(message.get("created_at") or created_at))
                duplicate = (
                    existing_source_ids.get(source_mid) if source_mid
                    else existing_signatures.get(signature)
                ) if existing_session else None
                reasoning = str(message.get("reasoning") or "").strip()[: thinking_vault.MAX_CHARS]
                metadata_dict = {
                    "imported": True,
                    "import_format": bundle.get("format") or "generic",
                    "source_message_id": message.get("source_id") or "",
                }
                if reasoning:
                    metadata_dict.update({
                        "thinking_available": True,
                        "thinking_chars": len(reasoning),
                        "thinking_label": "导出文件中明确提供的思考",
                        "thinking_provider": provider,
                    })
                if duplicate:
                    message_id, old_meta = duplicate
                    if reasoning:
                        merged_meta = {**old_meta, **metadata_dict}
                        db.execute(
                            "UPDATE messages SET metadata=? WHERE id=?",
                            (json.dumps(merged_meta, ensure_ascii=False), message_id),
                        )
                        prior = db.execute(
                            "SELECT content FROM reasoning_traces WHERE message_id=?", (message_id,)
                        ).fetchone()
                        if not prior or str(prior["content"] or "") != reasoning:
                            db.execute(
                                """INSERT INTO reasoning_traces
                                   (message_id, session_id, provider, model, content,
                                    char_count, token_estimate, created_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                   ON CONFLICT(message_id) DO UPDATE SET
                                     provider=excluded.provider, model=excluded.model,
                                     content=excluded.content, char_count=excluded.char_count,
                                     token_estimate=excluded.token_estimate,
                                     created_at=excluded.created_at""",
                                (message_id, session_id, provider, model, reasoning, len(reasoning),
                                 thinking_vault.estimate_tokens(reasoning), now),
                            )
                            imported_reasoning += 1
                            imported_reasoning_chars += len(reasoning)
                    continue
                metadata = json.dumps(metadata_dict, ensure_ascii=False)
                cursor = db.execute(
                    """INSERT INTO messages
                       (session_id, role, content, created_at, provider, model, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id, message["role"], message["content"],
                        message.get("created_at") or created_at, provider, model, metadata,
                    ),
                )
                message_id = int(cursor.lastrowid)
                if reasoning:
                    db.execute(
                        """INSERT INTO reasoning_traces
                           (message_id, session_id, provider, model, content,
                            char_count, token_estimate, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (message_id, session_id, provider, model, reasoning, len(reasoning),
                         thinking_vault.estimate_tokens(reasoning), now),
                    )
                    imported_reasoning += 1
                    imported_reasoning_chars += len(reasoning)
                inserted_messages += 1
                added_here += 1
                if source_mid:
                    existing_source_ids[source_mid] = (message_id, metadata_dict)
                existing_signatures[signature] = (message_id, metadata_dict)
            if existing_session:
                if not added_here:
                    skipped += 1
                    continue
                db.execute(
                    "UPDATE sessions SET title=?, updated_at=?, provider=?, model=?, message_count=message_count+? WHERE id=?",
                    (title, updated_at, provider, model, added_here, session_id),
                )
            imported_ids.append(session_id)
    archived = memory_archive.archive_existing_messages()
    return {
        "ok": True,
        "format": bundle.get("format") or "generic",
        "imported_conversations": len(imported_ids),
        "imported_messages": inserted_messages,
        "imported_reasoning_traces": imported_reasoning,
        "imported_reasoning_characters": imported_reasoning_chars,
        "skipped_duplicates": skipped,
        "archived_messages": archived,
        "session_ids": imported_ids,
        "first_session_id": imported_ids[0] if imported_ids else "",
    }


_MEMORY_KEYS = {
    "memory", "memories", "relationship", "relationship_memory", "relationshipmemory",
    "facts", "profile", "preferences", "instructions", "user_profile", "gemini_memory",
    "saved_info", "context",
}


def _memory_lines(value: Any, heading: str = "", depth: int = 0) -> list[str]:
    if depth > 8 or value is None:
        return []
    if isinstance(value, str):
        clean = value.strip()
        return ([f"## {heading}", clean] if heading and clean else ([clean] if clean else []))
    if isinstance(value, (int, float, bool)):
        return [f"- {heading}：{value}" if heading else f"- {value}"]
    if isinstance(value, list):
        lines: list[str] = [f"## {heading}"] if heading else []
        for item in value:
            if isinstance(item, str) and item.strip():
                lines.append(f"- {item.strip()}")
            else:
                lines.extend(_memory_lines(item, "", depth + 1))
        return lines
    if isinstance(value, dict):
        lines = [f"## {heading}"] if heading else []
        for key, item in value.items():
            key_text = str(key).replace("_", " ").strip()
            if isinstance(item, (str, int, float, bool)):
                text = str(item).strip()
                if text:
                    lines.append(f"- {key_text}：{text}")
            else:
                lines.extend(_memory_lines(item, key_text, depth + 1))
        return lines
    return []


def extract_foundation_bytes(filename: str, raw: bytes) -> dict[str, Any]:
    if not raw:
        raise ValueError("记忆文件是空的")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    suffix = str(filename or "").lower().rsplit(".", 1)[-1]
    detected = "text"
    content = text.strip()
    if suffix in {"json", "jsonl"}:
        try:
            data = json.loads(text) if suffix == "json" else [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ValueError(f"记忆 JSON 无法解析：第 {exc.lineno} 行第 {exc.colno} 列") from exc
        detected = "json-memory"
        candidates: list[str] = []
        if isinstance(data, dict):
            for key, value in data.items():
                normalized = str(key).lower().replace(" ", "_")
                if normalized in _MEMORY_KEYS:
                    candidates.extend(_memory_lines(value, str(key)))
        elif isinstance(data, list) and all(isinstance(value, (str, dict)) for value in data):
            candidates.extend(_memory_lines(data, "导入记忆"))
        if not candidates:
            # A user explicitly chose the relationship-memory importer, so a
            # generic object can still be represented faithfully as sections.
            candidates = _memory_lines(data, "导入记忆")
        content = "\n".join(line for line in candidates if line.strip()).strip()
    if not content:
        raise ValueError("没有从文件中提取到记忆文本")
    return {
        "format": detected,
        "filename": str(filename or "memory.txt")[:180],
        "content": content,
        "character_count": len(content),
        "token_estimate": max(1, (len(content) + 1) // 2),
    }
