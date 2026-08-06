"""Filtering, formatting, rate limiting, and retrying delivery orchestration."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

try:
    from .filter_chain import FilterChain, coerce_message, fingerprint_for
    from .formatter import MessageFormatter
    from .models import DeliveryItem, FilterDecision, ParsedMessage, RetryRecord
    from .persistence import DedupStore, RetryQueue
    from .rate_limiter import AsyncRateLimiter, rate_limiter_from_config
except ImportError:  # pragma: no cover - top-level module layout
    from filter_chain import FilterChain, coerce_message, fingerprint_for
    from formatter import MessageFormatter
    from models import DeliveryItem, FilterDecision, ParsedMessage, RetryRecord
    from persistence import DedupStore, RetryQueue
    from rate_limiter import AsyncRateLimiter, rate_limiter_from_config


LOGGER = logging.getLogger(__name__)
_MISSING = object()


def _value(source: Any, key: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(key, default)
    value = getattr(source, key, _MISSING)
    if value is not _MISSING:
        return value
    getter = getattr(source, "get", None)
    if getter is not None:
        try:
            return getter(key, default)
        except (AttributeError, KeyError, TypeError):
            pass
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeliveryError(RuntimeError):
    """Raised when a delivery attempt cannot be completed."""


class DeliveryResult:
    """A small result object that is truthy only for a sent message."""

    __slots__ = ("accepted", "sent", "queued", "filtered", "reason", "item", "error", "parts")

    def __init__(
        self,
        *,
        accepted: bool,
        sent: bool = False,
        queued: bool = False,
        filtered: bool = False,
        reason: str = "",
        item: DeliveryItem | None = None,
        error: BaseException | None = None,
        parts: tuple[str, ...] = (),
    ) -> None:
        self.accepted = accepted
        self.sent = sent
        self.queued = queued
        self.filtered = filtered
        self.reason = reason
        self.item = item
        self.error = error
        self.parts = parts

    def __bool__(self) -> bool:
        return self.sent

    def __repr__(self) -> str:
        return (
            "DeliveryResult("
            f"accepted={self.accepted!r}, sent={self.sent!r}, queued={self.queued!r}, "
            f"filtered={self.filtered!r}, reason={self.reason!r})"
        )

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return bool(self) == other
        if not isinstance(other, DeliveryResult):
            return NotImplemented
        return (
            self.accepted,
            self.sent,
            self.queued,
            self.filtered,
            self.reason,
            self.item,
        ) == (
            other.accepted,
            other.sent,
            other.queued,
            other.filtered,
            other.reason,
            other.item,
        )


class DeliveryPipeline:
    """Coordinate one incoming message through filter and delivery stages.

    ``sender`` may be an async callable ``(text, target_umo)`` or an object with
    an async/sync ``send`` method.  Every part is sent in order.  A failure
    queues the complete item, avoiding a partial retry that could reorder or
    duplicate message chunks.
    """

    def __init__(
        self,
        filter_chain: FilterChain,
        dedup: DedupStore | None = None,
        retry_queue: RetryQueue | None = None,
        sender: Any | None = None,
        target_umo: str = "",
        *,
        formatter: MessageFormatter | None = None,
        formatter_config: Any | None = None,
        security_config: Any | None = None,
        retry_config: Any | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        on_error: Callable[[BaseException], Any] | None = None,
    ) -> None:
        self.filter_chain = filter_chain
        self.dedup = dedup
        self.retry_queue = retry_queue
        self.sender = sender
        self.target_umo = str(target_umo)
        self.formatter = formatter or MessageFormatter(formatter_config or security_config)
        self.security_config = security_config
        self.retry_config = retry_config
        self.rate_limiter = rate_limiter or rate_limiter_from_config(security_config or {}, clock=None)
        self.on_error = on_error
        self._send_lock = asyncio.Lock()
        self._closed = False
        self.last_error: BaseException | None = None
        self.sent_count = 0
        self.failed_count = 0
        self.retry_count = 0

        retry = retry_config
        self.max_attempts = max(1, _integer(_value(retry, "max_attempts", 5), 5))
        self.initial_delay = max(0.0, _number(_value(retry, "initial_delay_seconds", 1), 1.0))
        self.max_delay = max(
            self.initial_delay,
            _number(_value(retry, "max_delay_seconds", 30), 30.0),
        )
        self.replay_failed_messages = _as_bool(
            _value(retry, "replay_failed_messages", True), True
        )

    @staticmethod
    def _item_from_message(message: ParsedMessage, payloads: tuple[str, ...], target_umo: str) -> DeliveryItem:
        return DeliveryItem(
            fingerprint=message.fingerprint or fingerprint_for(message),
            payloads=payloads,
            sender=message.sender,
            original_message=message.message,
            kind=message.kind.value,
            created_at=message.timestamp or _iso_now(),
            target_umo=target_umo,
            event_id=message.event_id,
        )

    async def _invoke_sender(self, text: str, target_umo: str) -> None:
        if self.sender is None:
            raise DeliveryError("sender is not configured")
        if hasattr(self.sender, "send"):
            operation = self.sender.send(text, target_umo)
        else:
            operation = self.sender(text, target_umo)
        if inspect.isawaitable(operation):
            await operation

    async def _send_item(self, item: DeliveryItem) -> None:
        # Serialize a complete item.  This avoids interleaving chunks from two
        # incoming messages when callbacks are invoked concurrently.
        async with self._send_lock:
            for payload in item.payloads:
                await self.rate_limiter.acquire()
                await self._invoke_sender(payload, item.target_umo)

    def _delay_for_attempt(self, attempts: int) -> float:
        # ``attempts`` is the number of attempts already made.  First retry is
        # initial_delay, then doubles, capped by max_delay.
        exponent = max(0, int(attempts) - 1)
        return min(self.max_delay, self.initial_delay * (2**exponent))

    async def _notify_error(self, error: BaseException) -> None:
        self.last_error = error
        if self.on_error is None:
            return
        try:
            value = self.on_error(error)
            if inspect.isawaitable(value):
                await value
        except Exception:
            LOGGER.exception("Delivery error callback failed")

    async def _queue_failure(
        self,
        item: DeliveryItem,
        error: BaseException,
        *,
        attempts: int = 1,
    ) -> RetryRecord | None:
        self.failed_count += 1
        await self._notify_error(error)
        if self.retry_queue is None or attempts >= self.max_attempts:
            return None
        delay = self._delay_for_attempt(attempts)
        # Keep the item attempt field in sync for queue consumers that do not
        # inspect RetryRecord.attempts.
        queued_item = DeliveryItem(
            fingerprint=item.fingerprint,
            payloads=item.payloads,
            sender=item.sender,
            original_message=item.original_message,
            kind=item.kind,
            created_at=item.created_at,
            target_umo=item.target_umo,
            attempt=attempts,
            event_id=item.event_id,
        )
        record = await self.retry_queue.enqueue(
            queued_item,
            attempts=attempts,
            delay_seconds=delay,
            last_error=str(error),
            record_id=f"{item.fingerprint}:{attempts}:{uuid.uuid4().hex}",
        )
        if record is not None:
            self.retry_count += 1
        return record

    async def handle(
        self,
        message: ParsedMessage | Mapping[str, Any],
        *,
        target_umo: str | None = None,
    ) -> DeliveryResult:
        """Filter, format, and deliver one parsed message."""

        if self._closed:
            raise RuntimeError("delivery pipeline is closed")
        decision: FilterDecision = await self.filter_chain.check(message)
        parsed = decision.message or coerce_message(message)
        if not decision.accepted:
            return DeliveryResult(
                accepted=False,
                filtered=True,
                reason=decision.reason,
                item=None,
            )
        payloads = self.formatter.format_parts(parsed)
        actual_target = self.target_umo if target_umo is None else str(target_umo)
        item = self._item_from_message(parsed, payloads, actual_target)
        try:
            await self._send_item(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._queue_failure(item, exc, attempts=1)
            return DeliveryResult(
                accepted=True,
                sent=False,
                queued=self.retry_queue is not None and self.max_attempts > 1,
                reason="delivery_failed",
                item=item,
                error=exc,
                parts=payloads,
            )
        self.sent_count += 1
        return DeliveryResult(
            accepted=True,
            sent=True,
            reason="sent",
            item=item,
            parts=payloads,
        )

    async def handle_raw(
        self,
        sender: str,
        message: str,
        raw: Mapping[str, Any] | None = None,
        *,
        kind: Any = None,
        timestamp: str | None = None,
        event_id: str | None = None,
    ) -> DeliveryResult:
        """Handle the callback shape used by ``MCPClient``."""

        source: dict[str, Any] = dict(raw or {})
        source.update({"sender": sender, "message": message})
        if kind is not None:
            source["kind"] = kind
        if timestamp is not None:
            source["timestamp"] = timestamp
        if event_id is not None:
            source["event_id"] = event_id
        return await self.handle(source)

    async def process_retry(self, record: RetryRecord) -> DeliveryResult:
        """Replay one due record and acknowledge/reschedule it atomically."""

        try:
            await self._send_item(record.item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._notify_error(exc)
            self.failed_count += 1
            next_attempt = record.attempts + 1
            if self.retry_queue is not None and next_attempt < self.max_attempts:
                delay = self._delay_for_attempt(next_attempt)
                await self.retry_queue.reschedule(
                    record.record_id,
                    attempts=next_attempt,
                    delay_seconds=delay,
                    last_error=str(exc),
                    item=DeliveryItem(
                        fingerprint=record.item.fingerprint,
                        payloads=record.item.payloads,
                        sender=record.item.sender,
                        original_message=record.item.original_message,
                        kind=record.item.kind,
                        created_at=record.item.created_at,
                        target_umo=record.item.target_umo,
                        attempt=next_attempt,
                        event_id=record.item.event_id,
                    ),
                )
                self.retry_count += 1
            elif self.retry_queue is not None:
                await self.retry_queue.ack(record.record_id)
            return DeliveryResult(
                accepted=True,
                sent=False,
                queued=self.retry_queue is not None and next_attempt < self.max_attempts,
                reason="retry_failed",
                item=record.item,
                error=exc,
                parts=record.item.payloads,
            )
        else:
            if self.retry_queue is not None:
                await self.retry_queue.ack(record.record_id)
            self.sent_count += 1
            return DeliveryResult(
                accepted=True,
                sent=True,
                reason="retried",
                item=record.item,
                parts=record.item.payloads,
            )

    async def process_due_retries(self, *, limit: int | None = None) -> tuple[DeliveryResult, ...]:
        if self.retry_queue is None or not self.replay_failed_messages:
            return ()
        records = await self.retry_queue.due(limit=limit)
        results: list[DeliveryResult] = []
        for record in records:
            results.append(await self.process_retry(record))
        return tuple(results)

    async def drain_retries(self, *, limit: int | None = None) -> tuple[DeliveryResult, ...]:
        return await self.process_due_retries(limit=limit)

    async def close(self) -> None:
        self._closed = True
        await self.rate_limiter.close()


Delivery = DeliveryPipeline

__all__ = ["Delivery", "DeliveryError", "DeliveryPipeline", "DeliveryResult"]
