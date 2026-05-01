"""Embedding generation via OpenAI text-embedding-3-small."""

import logging
from datetime import date, datetime, timezone

from openai import AsyncOpenAI, RateLimitError, APIError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import LLMUsage

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
MAX_BATCH_SIZE = 100


async def embed_text(text_input: str) -> list[float]:
    """Generate a single embedding vector."""
    results = await embed_batch([text_input])
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
        except RateLimitError as e:
            logger.error("OpenAI rate limit / quota exceeded: %s", e)
            await _report_api_error("embeddings", f"OpenAI rate limit: {e}")
            all_embeddings.extend([[0.0] * EMBEDDING_DIM] * len(chunk))
        except APIError as e:
            logger.error("OpenAI API error: %s", e)
            await _report_api_error("embeddings", f"OpenAI API error: {e.message}")
            all_embeddings.extend([[0.0] * EMBEDDING_DIM] * len(chunk))
        except Exception:
            logger.exception("Embedding batch failed for chunk %d-%d", i, i + len(chunk))
            all_embeddings.extend([[0.0] * EMBEDDING_DIM] * len(chunk))

    return all_embeddings


async def _report_api_error(service: str, message: str) -> None:
    """Update system_health to surface API errors on the dashboard."""
    try:
        from aegis.db.engine import async_session_factory
        from aegis.db.repositories import upsert_system_health
        async with async_session_factory() as session:
            await upsert_system_health(
                session,
                service,
                status="degraded",
                last_error=datetime.now(timezone.utc),
                last_error_message=message[:200],
            )
    except Exception:
        logger.debug("Failed to update system_health for %s", service, exc_info=True)


async def regenerate_missing_embeddings(session: AsyncSession) -> int:
    """Find items with all-zero embeddings and re-attempt generation.

    Returns count of successfully regenerated embeddings.
    """
    from aegis.db.models import Email, Meeting, ChatMessage

    regenerated = 0

    # Find emails with zero-vector embeddings
    # pgvector stores zeros; check by looking for embedding = all zeros via norm
    zero_email_stmt = text("""
        SELECT id, COALESCE(summary, subject, '') as text_input
        FROM emails
        WHERE embedding IS NOT NULL
        AND vector_norm(embedding) < 0.001
        AND processing_status = 'completed'
        LIMIT 50
    """)
    try:
        result = await session.execute(zero_email_stmt)
        rows = result.all()
        if rows:
            texts = [r[1] or " " for r in rows]
            embeddings = await embed_batch(texts)
            for row, emb in zip(rows, embeddings):
                if any(v != 0.0 for v in emb[:10]):  # Not a zero vector
                    await session.execute(
                        text("UPDATE emails SET embedding = :emb WHERE id = :id"),
                        {"emb": str(emb), "id": row[0]},
                    )
                    regenerated += 1
            await session.commit()
            logger.info("Regenerated %d email embeddings", regenerated)
    except Exception:
        logger.debug("Failed to regenerate email embeddings", exc_info=True)
        await session.rollback()

    return regenerated


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
