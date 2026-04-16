"""Embedding generation via OpenAI text-embedding-3-small."""

import logging
from datetime import date

from openai import AsyncOpenAI
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import LLMUsage

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
MAX_BATCH_SIZE = 100


async def embed_text(text: str) -> list[float]:
    """Generate a single embedding vector."""
    results = await embed_batch([text])
    return results[0]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts. Handles chunking for large batches."""
    if not texts:
        return []

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    all_embeddings: list[list[float]] = []
    total_tokens = 0

    for i in range(0, len(texts), MAX_BATCH_SIZE):
        chunk = texts[i : i + MAX_BATCH_SIZE]
        # Truncate long texts to avoid token limits
        truncated = [t[:8000] if t else " " for t in chunk]

        try:
            response = await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=truncated,
            )
            for item in response.data:
                all_embeddings.append(item.embedding)
            total_tokens += response.usage.total_tokens
        except Exception:
            logger.exception("Embedding batch failed for chunk %d-%d", i, i + len(chunk))
            # Return zero vectors for failed chunks
            all_embeddings.extend([[0.0] * EMBEDDING_DIM] * len(chunk))

    return all_embeddings


async def track_embedding_usage(session: AsyncSession, token_count: int) -> None:
    """Record embedding usage in llm_usage table."""
    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=EMBEDDING_MODEL,
        task="embedding",
        input_tokens=token_count,
        output_tokens=0,
        calls=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_llm_usage_daily",
        set_={
            "input_tokens": LLMUsage.input_tokens + token_count,
            "calls": LLMUsage.calls + 1,
        },
    )
    await session.execute(stmt)
    await session.commit()
