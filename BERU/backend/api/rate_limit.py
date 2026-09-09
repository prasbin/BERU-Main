"""In-memory sliding-window rate limiter.

Provides a lightweight, dependency-free rate limiter suitable for a single-user
personal AI system. Tracks requests per client IP using a sliding window counter.

Usage as a FastAPI dependency::

    from backend.api.security import rate_limit_chat

    @router.post("/chat", dependencies=[Depends(rate_limit_chat)])
    async def chat(...):
        ...

The limiter is process-local (in-memory). For multi-process deployments a
shared backend (Redis) would be needed, but that is out of scope for the
foundation stage.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import Depends, Request

from backend.core.config import Settings, get_settings
from backend.core.errors import RateLimitError
from backend.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _Window:
    """Sliding window counter for a single client."""

    timestamps: list[float] = field(default_factory=list)

    def prune(self, now: float, window_sec: float) -> None:
        """Remove timestamps older than the window."""
        cutoff = now - window_sec
        self.timestamps = [t for t in self.timestamps if t > cutoff]

    def count(self, now: float, window_sec: float) -> int:
        self.prune(now, window_sec)
        return len(self.timestamps)

    def record(self, now: float) -> None:
        self.timestamps.append(now)


class RateLimiter:
    """Simple per-IP sliding-window rate limiter.

    Args:
        rpm: Maximum requests per minute (per window).
        burst: Maximum consecutive requests before the limiter activates.
        window_sec: Sliding window duration in seconds (default 60).
    """

    def __init__(self, rpm: int = 30, burst: int = 5, window_sec: float = 60.0) -> None:
        self.rpm = rpm
        self.burst = burst
        self.window_sec = window_sec
        self._clients: dict[str, _Window] = defaultdict(_Window)
        self._last_cleanup = time.monotonic()

    def _cleanup(self) -> None:
        """Periodically evict stale entries to bound memory."""
        now = time.monotonic()
        if now - self._last_cleanup < self.window_sec * 2:
            return
        self._last_cleanup = now
        stale = [
            ip
            for ip, w in self._clients.items()
            if not w.timestamps or (now - w.timestamps[-1]) > self.window_sec * 2
        ]
        for ip in stale:
            del self._clients[ip]

    def check(self, client_ip: str) -> None:
        """Check rate limit; raises RateLimitError if exceeded."""
        if self.rpm <= 0:
            return

        now = time.monotonic()
        window = self._clients[client_ip]
        window.prune(now, self.window_sec)
        count = len(window.timestamps)

        if count >= self.rpm:
            retry_after = int(window.timestamps[0] + self.window_sec - now) + 1
            logger.warning(
                "Rate limit exceeded for %s: %d requests in %ds (limit %d rpm)",
                client_ip,
                count,
                int(self.window_sec),
                self.rpm,
            )
            raise RateLimitError(
                f"Rate limit exceeded. Try again in {retry_after}s.",
                detail={
                    "retry_after_seconds": retry_after,
                    "limit": self.rpm,
                    "window_seconds": int(self.window_sec),
                },
            )

        window.record(now)
        self._cleanup()


_global_limiter: RateLimiter | None = None


def get_rate_limiter(settings: Settings = Depends(get_settings)) -> RateLimiter:
    """Return (and cache) the process-wide rate limiter instance."""
    global _global_limiter
    if _global_limiter is None or _global_limiter.rpm != settings.rate_limit_chat_rpm:
        _global_limiter = RateLimiter(
            rpm=settings.rate_limit_chat_rpm,
            burst=settings.rate_limit_burst,
        )
    return _global_limiter


def _client_ip(request: Request) -> str:
    """Extract the client IP.

    ``X-Forwarded-For`` is only honored when ``BERU_TRUST_PROXY_HEADERS`` is set
    (i.e. BERU runs behind a trusted reverse proxy). Otherwise the header is
    attacker-controlled and must not influence rate limiting.
    """
    settings = get_settings()
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


async def rate_limit_chat(
    request: Request,
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """Enforce rate limiting on chat endpoints.

    Keys by the scoped user's name (so each user has their own budget on shared
    IPs behind NAT) or by client IP for owner/system traffic and guest clients.
    """
    from backend.core.context import get_current_principal, is_scoped

    principal = get_current_principal()
    if is_scoped(principal) and principal.username:
        limiter.check(f"user:{principal.username}")
        return
    ip = _client_ip(request)
    limiter.check(ip)


_auth_limiter: RateLimiter | None = None


def get_auth_limiter(settings: Settings = Depends(get_settings)) -> RateLimiter:
    """Return (and cache) the process-wide login rate limiter instance."""
    global _auth_limiter
    if _auth_limiter is None or _auth_limiter.rpm != settings.rate_limit_auth_rpm:
        _auth_limiter = RateLimiter(
            rpm=settings.rate_limit_auth_rpm,
            burst=settings.rate_limit_auth_burst,
        )
    return _auth_limiter


async def rate_limit_auth(
    request: Request,
    limiter: RateLimiter = Depends(get_auth_limiter),
) -> None:
    """FastAPI dependency that rate-limits authentication attempts per IP."""
    ip = _client_ip(request)
    limiter.check(ip)
