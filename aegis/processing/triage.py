"""Triage layer — batch classify items as substantive/contextual/noise using Haiku."""

import json
import logging
from datetime import date

from anthropic import AsyncAnthropic
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import Email, ChatMessage, LLMUsage

logger = logging.getLogger(__name__)

TRIAGE_MODEL = "claude-haiku-4-5-20251001"


class TriageResult(BaseModel):
    item_id: int
    triage_class: str  # "substantive", "contextual", "noise"
    score: float  # 0.0 - 1.0
    reason: str


TRIAGE_PROMPT = """\
You are a triage classifier for an executive's communication stream.
Classify each item as one of:
- **substantive** (0.7-1.0): contains decisions, asks, deliverables, project updates, or new information
- **contextual** (0.3-0.7): provides context but no extractable intelligence on its own (e.g. "sounds good", brief acknowledgments)
- **noise** (0.0-0.3): zero intelligence value (auto-replies, out-of-office, system notifications)

Return a JSON array of objects with fields: item_id, triage_class, score, reason.

Items to classify:
{items_json}
"""


async def triage_batch(
    session: AsyncSession,
    items: list[dict],
) -> list[TriageResult]:
    """Classify a batch of items. Each item dict must have 'id', 'preview', 'source_type'.

    Meeting transcripts bypass triage (always substantive) — do not pass them here.
    """
    if not items:
        return []

    # Chunk into batches of 20 to avoid truncated LLM responses
    CHUNK_SIZE = 20
    all_results: list[TriageResult] = []

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    for i in range(0, len(items), CHUNK_SIZE):
        chunk = items[i : i + CHUNK_SIZE]
        items_for_prompt = [
            {"item_id": item["id"], "preview": item["preview"][:300], "source": item["source_type"]}
            for item in chunk
        ]

        prompt = TRIAGE_PROMPT.format(items_json=json.dumps(items_for_prompt, indent=2))

        try:
            response = await client.messages.create(
                model=TRIAGE_MODEL,
                max_tokens=4096,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.content[0].text
            # Parse JSON from response (may be wrapped in markdown code block)
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            results_raw = json.loads(content.strip())

            results = [TriageResult(**r) for r in results_raw]
            all_results.extend(results)

            # Track LLM usage
            await _track_usage(
                session,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

        except Exception:
            logger.exception("Triage batch chunk %d-%d failed", i, i + len(chunk))

    return all_results


async def apply_triage_results(
    session: AsyncSession,
    results: list[TriageResult],
    item_type: str,
) -> None:
    """Write triage results back to the source table."""
    from sqlalchemy import update

    model_map = {
        "email": Email,
        "chat_message": ChatMessage,
    }
    model = model_map.get(item_type)
    if not model:
        return

    for result in results:
        stmt = (
            update(model)
            .where(model.id == result.item_id)
            .values(triage_class=result.triage_class, triage_score=result.score)
        )
        await session.execute(stmt)

    await session.commit()


async def _track_usage(
    session: AsyncSession, input_tokens: int, output_tokens: int
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = date.today()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=TRIAGE_MODEL,
        task="triage",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calls=1,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_llm_usage_daily",
        set_={
            "input_tokens": LLMUsage.input_tokens + input_tokens,
            "output_tokens": LLMUsage.output_tokens + output_tokens,
            "calls": LLMUsage.calls + 1,
        },
    )
    await session.execute(stmt)
    await session.commit()
