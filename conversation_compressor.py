"""Compatibility bridge for the v6.2 source-linked context compactor.

Older integrations imported :func:`auto_compress_if_needed` from this module.
Keep that symbol, but route it to the local incremental implementation so a
legacy caller can no longer trigger paid whole-history re-summarisation.
"""
from __future__ import annotations

import asyncio

from context_compactor import IncrementalContextCompactor, context_compactor


ConversationCompressor = IncrementalContextCompactor


async def auto_compress_if_needed(session_id: str, gateway=None) -> str | None:
    del gateway  # v6.2 intentionally does not call a model while compacting.
    result = await asyncio.to_thread(context_compactor.prepare_context, session_id, "")
    return result.get("context") or None


__all__ = [
    "ConversationCompressor",
    "IncrementalContextCompactor",
    "auto_compress_if_needed",
    "context_compactor",
]
