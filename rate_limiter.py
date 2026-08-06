"""Async, deterministic rate limiting with no third-party dependencies."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any


class AsyncRateLimiter:
    """Token-bucket limiter suitable for one async delivery worker.

    ``rate`` tokens are replenished every second and ``capacity`` tokens may be
    held.  Acquiring one token waits asynchronously (never blocks the event
    loop), and FIFO waiter order is preserved by a lock-protected condition.
    """

    def __init__(
        self,
        rate: float | int = 5,
        *,
        capacity: float | int | None = None,
        rate_per_second: float | int | None = None,
        burst: float | int | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if rate_per_second is not None:
            rate = rate_per_second
        try:
            parsed_rate = float(rate)
        except (TypeError, ValueError):
            parsed_rate = 0.0
        self.rate = max(0.0, parsed_rate)
        selected_capacity = capacity if capacity is not None else burst
        if selected_capacity is None:
            selected_capacity = max(1.0, self.rate)
        try:
            parsed_capacity = float(selected_capacity)
        except (TypeError, ValueError):
            parsed_capacity = max(1.0, self.rate)
        self.capacity = max(1.0, parsed_capacity)
        self._clock = clock or time.monotonic
        self._tokens = self.capacity
        self._updated_at = self._clock()
        self._condition = asyncio.Condition()
        self._closed = False

    @property
    def tokens(self) -> float:
        self._refill(self._clock())
        return self._tokens

    @property
    def closed(self) -> bool:
        return self._closed

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        if self.rate > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def _wait_seconds(self) -> float | None:
        if self.rate <= 0:
            return None
        return max(0.0, (1.0 - self._tokens) / self.rate)

    async def acquire(self, tokens: float | int = 1) -> None:
        """Wait until ``tokens`` are available, then consume them."""

        try:
            requested = float(tokens)
        except (TypeError, ValueError):
            raise ValueError("tokens must be a positive number") from None
        if requested <= 0:
            return
        if requested > self.capacity:
            raise ValueError("requested tokens exceed limiter capacity")
        async with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError("rate limiter is closed")
                self._refill(self._clock())
                if self.rate <= 0:
                    # A zero rate means unlimited, rather than a permanently
                    # blocked limiter.  This is useful for disabled settings.
                    return
                if self._tokens >= requested:
                    self._tokens -= requested
                    return
                delay = max(0.001, (requested - self._tokens) / self.rate)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def wait(self, tokens: float | int = 1) -> None:
        await self.acquire(tokens)

    async def throttle(self, tokens: float | int = 1) -> None:
        await self.acquire(tokens)

    def try_acquire(self, tokens: float | int = 1) -> bool:
        try:
            requested = float(tokens)
        except (TypeError, ValueError):
            return False
        if requested <= 0:
            return True
        if requested > self.capacity or self._closed:
            return False
        self._refill(self._clock())
        if self.rate <= 0 or self._tokens >= requested:
            if self.rate > 0:
                self._tokens -= requested
            return True
        return False

    def reset(self) -> None:
        self._tokens = self.capacity
        self._updated_at = self._clock()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    async def reopen(self) -> None:
        async with self._condition:
            self._closed = False
            self._updated_at = self._clock()
            self._condition.notify_all()


RateLimiter = AsyncRateLimiter


def _value(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(key, default)
    value = getattr(source, key, None)
    if value is not None:
        return value
    getter = getattr(source, "get", None)
    if getter is not None:
        try:
            return getter(key, default)
        except (AttributeError, KeyError, TypeError):
            pass
    return default


def rate_limiter_from_config(config: Any, *, clock: Callable[[], float] | None = None) -> AsyncRateLimiter:
    """Build a limiter from the SPEC's ``security.rate_limit_per_second``."""

    security = _value(config, "security", None)
    source = security if security is not None else config
    rate = _value(source, "rate_limit_per_second", _value(config, "rate_limit_per_second", 5))
    return AsyncRateLimiter(rate, clock=clock)


__all__ = ["AsyncRateLimiter", "RateLimiter", "rate_limiter_from_config"]
