"""Short-TTL cache for static policy retrieval only.

Never cache order/user-specific tool results. Keys include the full policy scope
and corpus revision so market or policy changes cannot reuse another result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from app.schemas.retrieval import PolicyScope, RetrievedChunk

logger = logging.getLogger(__name__)


def _looks_like_order_query(query: str) -> bool:
    compact = query.upper().replace(" ", "")
    return "ORD" in compact or "订单" in query


def cache_key(
    query: str,
    top_k: int,
    *,
    scope: PolicyScope | None = None,
    revision: str = "",
) -> str:
    resolved_scope = scope or PolicyScope()
    payload = {
        "query": query,
        "top_k": top_k,
        "scope": resolved_scope.cache_payload(),
        "revision": revision,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()[:32]
    return f"ret:{digest}"


class RetrievalCache:
    async def get(
        self,
        query: str,
        top_k: int,
        *,
        scope: PolicyScope | None = None,
        revision: str = "",
    ) -> list[RetrievedChunk] | None:
        return None

    async def set(
        self,
        query: str,
        top_k: int,
        chunks: list[RetrievedChunk],
        *,
        scope: PolicyScope | None = None,
        revision: str = "",
    ) -> None:
        return None


class MemoryRetrievalCache(RetrievalCache):
    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, list[RetrievedChunk]]] = {}

    async def get(
        self,
        query: str,
        top_k: int,
        *,
        scope: PolicyScope | None = None,
        revision: str = "",
    ) -> list[RetrievedChunk] | None:
        if _looks_like_order_query(query):
            return None
        key = cache_key(query, top_k, scope=scope, revision=revision)
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, chunks = item
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return chunks

    async def set(
        self,
        query: str,
        top_k: int,
        chunks: list[RetrievedChunk],
        *,
        scope: PolicyScope | None = None,
        revision: str = "",
    ) -> None:
        if _looks_like_order_query(query):
            return
        key = cache_key(query, top_k, scope=scope, revision=revision)
        self._store[key] = (time.monotonic() + self._ttl, chunks)


class RedisRetrievalCache(RetrievalCache):
    def __init__(self, redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(
        self,
        query: str,
        top_k: int,
        *,
        scope: PolicyScope | None = None,
        revision: str = "",
    ) -> list[RetrievedChunk] | None:
        if _looks_like_order_query(query):
            return None
        try:
            raw = await self._redis.get(cache_key(query, top_k, scope=scope, revision=revision))
        except Exception:
            logger.warning("retrieval_cache_redis_get_failed")
            return None
        if not raw:
            return None
        payload = json.loads(raw)
        return [RetrievedChunk.model_validate(item) for item in payload]

    async def set(
        self,
        query: str,
        top_k: int,
        chunks: list[RetrievedChunk],
        *,
        scope: PolicyScope | None = None,
        revision: str = "",
    ) -> None:
        if _looks_like_order_query(query):
            return
        try:
            await self._redis.set(
                cache_key(query, top_k, scope=scope, revision=revision),
                json.dumps([c.model_dump(mode="json") for c in chunks], ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception:
            logger.warning("retrieval_cache_redis_set_failed")
