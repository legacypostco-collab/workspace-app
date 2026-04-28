"""Embedding provider abstraction.

Supports OpenAI text-embedding-3-small (1536d) and Voyage AI voyage-3 (1024d → padded).
Falls back to deterministic hash-based stub for local dev / tests with no API keys.
"""
from __future__ import annotations

import hashlib
import logging
import os
import struct

import httpx

from django.conf import settings

logger = logging.getLogger(__name__)

EMBEDDING_DIMENSIONS = 1536


def _stub_embedding(text: str) -> list[float]:
    """Deterministic hash-based pseudo-embedding for tests / dev with no API key.

    Splits text into ~32-char shingles, hashes each into a deterministic float,
    and aggregates into a 1536-dim vector. Same text → identical vec.
    Different but related text shares some hash buckets → some similarity.
    NOT a real semantic embedding, but functional for dev/testing.
    """
    text = text or ""
    vec = [0.0] * EMBEDDING_DIMENSIONS
    if not text.strip():
        return vec
    # Hash whole text + word-level features
    tokens = text.lower().split()
    # Add char-trigram features for fuzzy matching
    features = list(tokens) + [text[i:i+3] for i in range(0, len(text) - 2, 3)]
    if not features:
        features = [text[:64]]
    for f in features:
        h = hashlib.md5(f.encode("utf-8", errors="ignore")).digest()
        # Use first 4 bytes as int → bucket index
        idx = struct.unpack("<I", h[:4])[0] % EMBEDDING_DIMENSIONS
        # Use next 4 bytes as int → magnitude in [-1, 1]
        mag = (struct.unpack("<i", h[4:8])[0] / 2147483648.0)
        vec[idx] += mag
    # L2 normalize
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm else vec


def _openai_embedding(text: str) -> list[float]:
    api_key = getattr(settings, "OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "text-embedding-3-small",
                "input": text[:8000],
                "dimensions": EMBEDDING_DIMENSIONS,
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def _voyage_embedding(text: str) -> list[float]:
    api_key = getattr(settings, "VOYAGE_API_KEY", "") or os.getenv("VOYAGE_API_KEY", "")
    if not api_key:
        raise RuntimeError("VOYAGE_API_KEY not set")
    try:
        import voyageai
    except ImportError:
        raise RuntimeError("voyageai not installed")
    vo = voyageai.Client(api_key=api_key)
    result = vo.embed([text[:8000]], model="voyage-3", input_type="document")
    vec = result.embeddings[0]
    # voyage-3 returns 1024-dim — pad with zeros to 1536 for compatibility
    if len(vec) < EMBEDDING_DIMENSIONS:
        vec = list(vec) + [0.0] * (EMBEDDING_DIMENSIONS - len(vec))
    return vec[:EMBEDDING_DIMENSIONS]


def get_embedding(text: str) -> list[float]:
    """Get embedding for text. Tries OpenAI → Voyage → stub."""
    text = (text or "").strip()
    if not text:
        return _stub_embedding("empty")

    provider = getattr(settings, "EMBEDDING_PROVIDER", "auto").lower()

    if provider in ("openai", "auto"):
        try:
            return _openai_embedding(text)
        except Exception as e:
            if provider == "openai":
                raise
            logger.debug(f"OpenAI embedding failed, trying Voyage: {e}")

    if provider in ("voyage", "auto"):
        try:
            return _voyage_embedding(text)
        except Exception as e:
            if provider == "voyage":
                raise
            logger.debug(f"Voyage embedding failed, falling back to stub: {e}")

    return _stub_embedding(text)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity for two vectors. Used as SQLite fallback."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _is_stub_mode() -> bool:
    """True if no real embedding provider is configured (using hash-based stub)."""
    if getattr(settings, "EMBEDDING_PROVIDER", "auto") == "stub":
        return True
    return not (
        getattr(settings, "OPENAI_API_KEY", "") or getattr(settings, "VOYAGE_API_KEY", "")
    )


def _keyword_score(query: str, chunk_text: str) -> float:
    """Simple BM25-ish keyword overlap. Used as fallback in stub mode."""
    if not query or not chunk_text:
        return 0.0
    q_words = set(w for w in query.lower().split() if len(w) > 2)
    if not q_words:
        return 0.0
    text_lower = chunk_text.lower()
    matches = sum(1 for w in q_words if w in text_lower)
    return matches / max(len(q_words), 1)


def search_similar_chunks(
    embedding: list[float],
    role: str,
    language: str = None,
    limit: int = 5,
    min_score: float = 0.7,
    query_text: str = "",
):
    """Search relevant chunks. Uses pgvector on Postgres, hybrid (embedding+keyword)
    in stub/SQLite mode. Returns chunks with .similarity_score annotation.
    """
    from .models import KnowledgeChunk

    # Filter by role + language
    qs = KnowledgeChunk.objects.filter(is_active=True)
    if language:
        qs = qs.filter(language=language)

    is_pg = "postgres" in settings.DATABASES["default"]["ENGINE"]
    if is_pg:
        qs = qs.filter(access_roles__contains=role)
    else:
        qs = qs.filter(access_roles__icontains=f'"{role}"')

    # Postgres: real pgvector cosine search
    if is_pg and not _is_stub_mode():
        try:
            from pgvector.django import CosineDistance
            qs = qs.annotate(distance=CosineDistance("embedding", embedding))
            qs = qs.filter(distance__lt=(1 - min_score)).order_by("distance")
            chunks = list(qs[:limit])
            for c in chunks:
                c.similarity_score = round(1 - float(c.distance), 3)
            return chunks
        except Exception as e:
            logger.warning(f"pgvector search failed: {e}")

    # Hybrid fallback: combine cosine + keyword score, lower threshold for stub
    use_stub = _is_stub_mode()
    eff_min_score = 0.05 if use_stub else min_score

    chunks = list(qs[:1000])
    scored = []
    for c in chunks:
        emb_score = 0.0
        if c.embedding:
            emb = c.embedding if isinstance(c.embedding, list) else list(c.embedding)
            emb_score = cosine_similarity(embedding, emb)
        kw_score = _keyword_score(query_text, c.title + " " + c.content) if query_text else 0
        # Combined: emb * 0.5 + kw * 0.5 (in stub mode), else mostly emb
        combined = (emb_score * 0.5 + kw_score * 0.5) if use_stub else (emb_score * 0.8 + kw_score * 0.2)
        if combined >= eff_min_score:
            c.similarity_score = round(combined, 3)
            scored.append(c)
    scored.sort(key=lambda c: -c.similarity_score)
    return scored[:limit]
