"""Rerank providers: local Cross-Encoder and lexical fallback."""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Any

import jieba

from app.config.settings import Settings
from app.providers.device import resolve_torch_device

logger = logging.getLogger(__name__)


class LexicalRerankProvider:
    """Weighted token/title overlap fallback. CI only — not a Cross-Encoder."""

    backend = "lexical"

    @staticmethod
    def _coverage(terms: list[str], text: str) -> float:
        total = sum(len(term) for term in terms)
        if not total:
            return 0.0
        lowered = text.lower()
        return sum(len(term) for term in terms if term in lowered) / total

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        terms = list(
            dict.fromkeys(
                token.strip().lower()
                for token in jieba.lcut_for_search(query)
                if any(char.isalnum() for char in token)
            )
        )
        scored: list[tuple[int, float]] = []
        for index, document in enumerate(documents):
            title, separator, content = document.partition("\n")
            if not separator:
                title, content = "", document
            score = 0.5 * self._coverage(terms, content) + 0.5 * self._coverage(terms, title)
            scored.append((index, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_n]


class CrossEncoderRerankProvider:
    """Local Cross-Encoder rerank (query+doc joint encoding).

    Default checkpoint is BAAI/bge-reranker-base. Tests do not download weights:
    APP_ENV=testing uses lexical unless a model is injected or CROSS_ENCODER_FORCE=true.
    """

    def __init__(self, settings: Settings, model: Any | None = None) -> None:
        self._settings = settings
        self._model = model
        self._fallback = LexicalRerankProvider()
        self._load_lock = Lock()
        self._load_error: str | None = None

    @property
    def backend(self) -> str:
        return "cross-encoder" if self.available else "lexical"

    @property
    def available(self) -> bool:
        return self._ensure_model() is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def _should_load(self) -> bool:
        if not self._settings.cross_encoder_enabled:
            return False
        if self._settings.app_env == "testing" and not self._settings.cross_encoder_force:
            return False
        return True

    def _ensure_model(self) -> Any | None:
        if self._model is not None:
            return self._model
        if not self._should_load():
            return None
        with self._load_lock:
            if self._model is not None:
                return self._model
            if self._load_error is not None:
                return None
            try:
                from sentence_transformers import CrossEncoder
            except ImportError:
                self._load_error = "sentence-transformers missing; pip install -e '.[rerank]'"
                logger.warning("cross_encoder_sdk_missing")
                return None
            device = resolve_torch_device(self._settings.cross_encoder_device)
            try:
                logger.info(
                    "cross_encoder_loading",
                    extra={"model": self._settings.cross_encoder_model, "device": device},
                )
                self._model = CrossEncoder(
                    self._settings.cross_encoder_model,
                    device=device,
                    max_length=self._settings.cross_encoder_max_length,
                )
            except Exception as exc:  # model download / CUDA / Hub errors
                self._load_error = str(exc)
                logger.warning("cross_encoder_load_failed", extra={"error": str(exc)})
                return None
            return self._model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[tuple[int, float]]:
        model = self._ensure_model()
        if model is None:
            return await self._fallback.rerank(query, documents, top_n=top_n)
        pairs = [(query, document) for document in documents]
        scores = await asyncio.to_thread(self._predict, model, pairs)
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        return [(index, score) for index, score in ranked[:top_n]]

    @staticmethod
    def _predict(model: Any, pairs: list[tuple[str, str]]) -> list[float]:
        raw = model.predict(pairs, show_progress_bar=False)
        return [float(score) for score in raw]
