"""``/v1/ocr`` — time-window OCR frame retrieval.

Per HELIOS.md §7.3 / §9.4. Phase 2 returned an empty placeholder; Phase
5 / Track 5A lights up real frame retrieval against the ``ocr_frames``
table now that the OCR worker populates it.

Query params (all optional):

* ``start`` / ``end`` — UTC epoch seconds. When omitted, the window
  defaults to "everything" (start ⇒ 0, end ⇒ now); the typical caller
  always supplies both.
* ``min_confidence`` — float in [0.0, 1.0]. Drops rows below this
  ``avg_confidence`` (default 0.5, matching the worker's persistence
  threshold for parity).
* ``app_bundle`` — exact-match filter on the ``app_bundle`` column.

The endpoint never strips raw OCR text — Aegis is the only consumer
and needs the full text for extraction prompts. PII safety is upheld
by the bearer-token auth on the daemon's API (every Helios endpoint is
``Authorization: Bearer`` gated).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, Request, status

from helios.api.schemas import OcrFrameResponse, OcrListResponse
from helios.db import queries
from helios.log import get_logger

router = APIRouter()
log = get_logger("api.ocr")


def _err(code: int, error: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=code,
        detail={"error": error, "detail": detail, "code": code},
    )


@router.get("/ocr", response_model=OcrListResponse)
async def get_ocr(
    request: Request,
    start: float | None = Query(default=None),
    end: float | None = Query(default=None),
    min_confidence: float = Query(default=0.5),
    app_bundle: str | None = Query(default=None),
) -> OcrListResponse:
    """Frames whose ``ts`` falls in ``[start, end]``, oldest first.

    Per HELIOS.md §7.3 — the canonical query for Aegis when correlating
    a meeting's audio transcript with on-screen content (slide decks,
    shared docs, etc).
    """
    # Default the window to "everything up to now" when the caller omits
    # the bounds — keeps the test/dev experience simple while the
    # typical production caller always provides both.
    window_start = 0.0 if start is None else float(start)
    window_end = time.time() if end is None else float(end)
    if window_end < window_start:
        raise _err(
            status.HTTP_400_BAD_REQUEST,
            "invalid_range",
            f"end ({window_end}) must be >= start ({window_start})",
        )

    db = request.app.state.db_pool.writer
    rows = await queries.get_ocr_frames_in_range(
        db,
        start_ts=window_start,
        end_ts=window_end,
        min_confidence=min_confidence,
        app_bundle=app_bundle,
    )
    return OcrListResponse(
        frames=[
            OcrFrameResponse(
                ts=row.ts,
                app_bundle=row.app_bundle,
                display_id=row.display_id,
                text=row.text,
                confidence=row.avg_confidence,
                thumbnail_url=None,
            )
            for row in rows
        ]
    )
