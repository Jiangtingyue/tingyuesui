"""Shared, fail-open embedding service.

The old build loaded the same sentence-transformers model independently in
pipeline.py and retriever_v4.py, and could start several concurrent downloads /
loads.  This module keeps one process-wide model, serialises loading/encoding,
and remembers failures briefly so chat is never blocked by repeated retries.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

import numpy as np

from config import MEMORY_CONFIG, VECTOR_CONFIG


class EmbeddingService:
    def __init__(self) -> None:
        self._model = None
        self._load_lock = threading.Lock()
        self._encode_lock = threading.Lock()
        self._last_error: str | None = None
        self._retry_after = 0.0

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _load_sync(self):
        if not MEMORY_CONFIG.get("enable_embeddings", True):
            return None
        if self._model is not None:
            return self._model
        if time.monotonic() < self._retry_after:
            return None

        with self._load_lock:
            if self._model is not None:
                return self._model
            if time.monotonic() < self._retry_after:
                return None
            try:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(VECTOR_CONFIG["model_name"])
                self._last_error = None
                return self._model
            except Exception as exc:  # fail-open: memory can still use FTS/tags
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._retry_after = time.monotonic() + float(
                    MEMORY_CONFIG.get("embedding_retry_seconds", 300)
                )
                print(f"[Embedding] 暂时不可用，已降级到关键词记忆: {self._last_error}")
                return None

    def encode_sync(self, text: str, *, allow_load: bool = True) -> Optional[np.ndarray]:
        if not text or not MEMORY_CONFIG.get("enable_embeddings", True):
            return None
        model = self._model
        if model is None and allow_load:
            model = self._load_sync()
        if model is None:
            return None

        # Most transformer backends are safer and much calmer when one encode
        # runs at a time.  This also prevents a pile-up after several messages.
        with self._encode_lock:
            try:
                vec = model.encode(text, normalize_embeddings=True)
                return np.asarray(vec, dtype=np.float32)
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                print(f"[Embedding] 编码失败，跳过本条向量: {self._last_error}")
                return None

    async def encode(self, text: str, *, allow_load: bool = True) -> Optional[np.ndarray]:
        return await asyncio.to_thread(self.encode_sync, text, allow_load=allow_load)

    async def warmup(self) -> None:
        if not MEMORY_CONFIG.get("warmup_embeddings", False):
            return
        await asyncio.to_thread(self._load_sync)


embedding_service = EmbeddingService()
