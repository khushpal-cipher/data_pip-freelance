import time
from collections import defaultdict

from fastapi import HTTPException, Request

from app.config import settings

# ponytail: in-memory per-process rate limiter, swap for Redis if scaled to >1 worker
_hits: dict[str, list[float]] = defaultdict(list)
_WINDOW_SECONDS = 60


def enforce_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _hits[ip] if now - t < _WINDOW_SECONDS]
    if len(recent) >= settings.rate_limit_per_minute:
        raise HTTPException(status_code=429, detail="Too many submissions, try again shortly")
    recent.append(now)
    _hits[ip] = recent
