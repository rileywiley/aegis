"""Voice profile learning and generation — learn user's writing style from sent emails."""

import logging
from datetime import datetime, timezone

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import get_settings
from aegis.db.models import LLMUsage, VoiceProfile

logger = logging.getLogger(__name__)

SONNET_MODEL = "claude-sonnet-4-6-20250514"


async def _track_llm_usage(
    session: AsyncSession,
    model: str,
    task: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Record LLM token usage for cost tracking."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    today = datetime.now(timezone.utc).date()
    stmt = pg_insert(LLMUsage).values(
        date=today,
        model=model,
        task=task,
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


async def _get_voice_profile(session: AsyncSession) -> VoiceProfile | None:
    """Fetch the single voice profile record (there is only ever one)."""
    stmt = select(VoiceProfile).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def learn_voice(session: AsyncSession) -> None:
    """Analyze sent emails to build the user's voice profile.

    Fetches 30-50 sent emails via Graph API, sends them to Sonnet for
    style analysis, and stores the resulting profile in the voice_profile table.
    """
    from aegis.ingestion.graph_client import GraphClient

    settings = get_settings()

    # Fetch sent emails
    graph = GraphClient()
    try:
        sent_emails = await graph.get_messages(folder="sentitems", top=50)
    finally:
        await graph.close()

    if not sent_emails:
        logger.warning("No sent emails found — cannot learn voice profile")
        return

    # Build sample text from sent emails (body preview or full body)
    samples = []
    for email in sent_emails[:50]:
        body = email.get("body", {}).get("content", "")
        preview = email.get("bodyPreview", "")
        subject = email.get("subject", "")
        text = preview or body[:500]
        if text:
            samples.append(f"Subject: {subject}\n{text}")

    if len(samples) < 5:
        logger.warning("Only %d sent email samples — profile may be inaccurate", len(samples))

    combined = "\n\n---\n\n".join(samples[:40])

    # Send to Sonnet for analysis
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    prompt = f"""Analyze the following sent emails from a single person and create a detailed voice profile.
Identify:
1. Overall tone (formal, semi-formal, casual, etc.)
2. Average message length tendency (brief/moderate/detailed)
3. Common greeting patterns (e.g., "Hi [Name],", "Hey,", no greeting)
4. Common closing patterns (e.g., "Best,", "Thanks,", "Cheers,", just name)
5. Writing style characteristics (direct vs diplomatic, use of bullet points, question style)
6. Common phrases or expressions they frequently use
7. Level of detail in responses
8. How they handle requests (direct ask vs softened language)
9. Use of pleasantries or small talk
10. Punctuation and formatting habits

Return a structured profile that could be used to generate emails in this person's voice.
Be specific with examples from the emails.

EMAILS:
{combined}"""

    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=2000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )

    profile_text = response.content[0].text

    # Track usage
    await _track_llm_usage(
        session,
        SONNET_MODEL,
        "voice_profile",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    # Store or update voice profile
    existing = await _get_voice_profile(session)
    now = datetime.now(timezone.utc)

    if existing:
        existing.auto_profile = profile_text
        existing.last_learned_at = now
        existing.updated = now
    else:
        profile = VoiceProfile(
            auto_profile=profile_text,
            custom_rules=[],
            edit_history={},
            last_learned_at=now,
            updated=now,
        )
        session.add(profile)

    await session.commit()
    logger.info("Voice profile learned from %d sent emails", len(samples))


async def generate_in_voice(
    session: AsyncSession,
    directive: str,
    context: str,
    channel: str,
) -> str:
    """Generate a message in the user's voice.

    Args:
        session: Database session.
        directive: User's plain-language instruction (e.g., "Approved, cap at $280K").
        context: Full context about the source item (ask description, thread, etc.).
        channel: 'email' or 'teams_chat' — affects formality and format.

    Returns:
        Generated message text ready for review.
    """
    settings = get_settings()

    # Load voice profile
    profile = await _get_voice_profile(session)
    voice_section = ""
    if profile and profile.auto_profile:
        voice_section = f"""
VOICE PROFILE (match this person's writing style):
{profile.auto_profile}
"""
        if profile.custom_rules:
            rules = "\n".join(f"- {r}" for r in profile.custom_rules)
            voice_section += f"""
CUSTOM STYLE RULES (override the auto-profile where they conflict):
{rules}
"""

    channel_guidance = ""
    if channel == "teams_chat":
        channel_guidance = (
            "This is a Teams chat message. Keep it conversational and relatively brief. "
            "No formal email structure needed."
        )
    else:
        channel_guidance = (
            "This is an email. Include appropriate greeting and closing. "
            "Follow standard email structure."
        )

    prompt = f"""Generate a message based on the user's directive, matching their voice profile.

{voice_section}

CONTEXT (the item being responded to):
{context}

USER'S DIRECTIVE:
{directive}

CHANNEL: {channel}
{channel_guidance}

Write ONLY the message body. Do not include explanations or meta-commentary.
Match the voice profile closely. If no voice profile is available, write in a
professional but approachable tone."""

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=2000,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )

    generated = response.content[0].text

    # Track usage
    await _track_llm_usage(
        session,
        SONNET_MODEL,
        "response_draft",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    return generated
