"""Health check endpoint — always public, no auth required."""

import time

from fastapi import APIRouter

router = APIRouter()
_started_at = time.time()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptime_seconds": round(time.time() - _started_at, 1),
    }
