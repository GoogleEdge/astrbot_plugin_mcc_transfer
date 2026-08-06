"""Small stdlib-only persistent stores used by the forwarding pipeline.

Both stores use an atomic replace when writing JSON.  Corrupt or missing state is
reported through ``load``'s return value and treated as an empty store, so a bad
state file cannot prevent the plugin from starting.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .models import DeliveryItem, RetryRecord
except ImportError:  # pragma: no cover - top-level module layout
    from models import DeliveryItem, RetryRecord


_MISSING = object()


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


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _now_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat()


def _parse_datetime(value: Any, *, default: datetime | None = None) -> datetime:
    fallback = default or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            return fallback
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(text), timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return fallback
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _timestamp(value: float | datetime | str | None = None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return _parse_datetime(value).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _parse_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return time.time()


def _atomic_dump(path: Path, value: Any) -> None:
    """Write JSON to ``path`` without exposing a partial file to readers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, separators=(",", ": "))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class DedupStore:
    """A bounded, TTL-based fingerprint store.

    ``check_and_add`` returns ``True`` when the fingerprint was already seen and
    ``False`` when it was newly inserted.  This convention makes it safe to use
    directly from a filter: a true value means "reject as duplicate".
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        cache_size: int = 1000,
        ttl_seconds: float = 300,
        state_file: str | Path | None = None,
        clock: Any | None = None,
    ) -> None:
        selected = state_file if state_file is not None else path
        self.path = Path(selected) if selected is not None else None
        self.cache_size = max(0, _integer(cache_size, 1000))
        self.ttl_seconds = max(0.0, _number(ttl_seconds, 300.0))
        self._clock = clock or time.time
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()
        self.loaded = False
        self.last_load_error: str | None = None

    @property
    def state_file(self) -> Path | None:
        return self.path

    @property
    def entries(self) -> dict[str, float]:
        with self._lock:
            return dict(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _is_expired(self, seen_at: float, now: float) -> bool:
        return self.ttl_seconds == 0 or now - seen_at >= self.ttl_seconds

    def _prune_locked(self, now: float) -> bool:
        changed = False
        for fingerprint, seen_at in tuple(self._entries.items()):
            if self._is_expired(seen_at, now):
                del self._entries[fingerprint]
                changed = True
        while len(self._entries) > self.cache_size:
            self._entries.popitem(last=False)
            changed = True
        return changed

    def _record_locked(self, fingerprint: str, now: float) -> bool:
        """Record and return whether it was already present and unexpired."""

        key = str(fingerprint)
        if not key:
            # Empty fingerprints should not make every empty event a duplicate.
            return False
        self._prune_locked(now)
        previous = self._entries.get(key)
        duplicate = previous is not None and not self._is_expired(previous, now)
        if previous is not None:
            del self._entries[key]
        if self.cache_size > 0:
            self._entries[key] = now
            while len(self._entries) > self.cache_size:
                self._entries.popitem(last=False)
        return duplicate

    def _contains_locked(self, fingerprint: str, now: float) -> bool:
        key = str(fingerprint)
        self._prune_locked(now)
        previous = self._entries.get(key)
        if previous is None:
            return False
        # Refresh LRU order while retaining the original timestamp for TTL.
        self._entries.move_to_end(key)
        return not self._is_expired(previous, now)

    def check_and_add_sync(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        with self._lock:
            duplicate = self._record_locked(str(fingerprint), _timestamp(now))
            if self.path is not None:
                self._save_locked()
            return duplicate

    def check_and_record_sync(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        return self.check_and_add_sync(fingerprint, now=now)

    async def check_and_add(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        async with self._async_lock:
            # The operation itself is synchronous and short; holding the lock
            # makes check+insert atomic among concurrent message callbacks.
            return self.check_and_add_sync(fingerprint, now=now)

    async def check_and_record(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        return await self.check_and_add(fingerprint, now=now)

    def contains(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        with self._lock:
            return self._contains_locked(str(fingerprint), _timestamp(now))

    def seen(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        return self.contains(fingerprint, now=now)

    def add_sync(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        """Insert a fingerprint and return whether it was a duplicate."""

        return self.check_and_add_sync(fingerprint, now=now)

    async def add(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        return await self.check_and_add(fingerprint, now=now)

    async def is_duplicate(self, fingerprint: str, *, now: float | datetime | None = None) -> bool:
        return await self.check_and_add(fingerprint, now=now)

    def _save_locked(self) -> None:
        if self.path is None:
            return
        _atomic_dump(
            self.path,
            {
                "version": 1,
                "entries": [
                    {"fingerprint": fingerprint, "seen_at": seen_at}
                    for fingerprint, seen_at in self._entries.items()
                ],
            },
        )

    def save_sync(self) -> None:
        with self._lock:
            self._prune_locked(_timestamp(self._clock()))
            self._save_locked()

    async def save(self) -> None:
        async with self._async_lock:
            self.save_sync()

    def _load_value_locked(self, value: Any) -> None:
        self._entries.clear()
        if isinstance(value, Mapping):
            raw_entries = value.get("entries", value.get("items", value.get("cache", ())))
            if isinstance(raw_entries, Mapping):
                iterable: Iterable[Any] = (
                    {"fingerprint": key, "seen_at": item} for key, item in raw_entries.items()
                )
            else:
                iterable = raw_entries or ()
        elif isinstance(value, list):
            iterable = value
        else:
            iterable = ()
        for item in iterable:
            if isinstance(item, Mapping):
                key = item.get("fingerprint", item.get("key", item.get("id", "")))
                timestamp = item.get("seen_at", item.get("timestamp", item.get("at", 0)))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                key, timestamp = item[0], item[1]
            else:
                continue
            text = str(key or "")
            if not text:
                continue
            self._entries[text] = _timestamp(timestamp)
        self._prune_locked(_timestamp(self._clock()))

    def load_sync(self) -> bool:
        with self._lock:
            if self.path is None:
                self.loaded = True
                return True
            try:
                value = _read_json(self.path)
            except FileNotFoundError:
                self.loaded = True
                self.last_load_error = None
                return False
            except (OSError, ValueError, TypeError) as exc:
                self._entries.clear()
                self.loaded = True
                self.last_load_error = str(exc)
                return False
            self._load_value_locked(value)
            self.loaded = True
            self.last_load_error = None
            # Rewrite old/canonicalized state only when useful; this also
            # removes expired entries from a state file after a restart.
            self._save_locked()
            return True

    async def load(self) -> bool:
        async with self._async_lock:
            return self.load_sync()

    async def clear(self) -> None:
        async with self._async_lock:
            with self._lock:
                self._entries.clear()
                self._save_locked()

    def clear_sync(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save_locked()


class RetryQueue:
    """Persistent FIFO-by-due-time queue of :class:`RetryRecord` objects."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_queue_size: int = 1000,
        expire_seconds: float | None = 3600,
        drop_expired: bool = False,
        queue_file: str | Path | None = None,
        clock: Any | None = None,
    ) -> None:
        selected = queue_file if queue_file is not None else path
        self.path = Path(selected) if selected is not None else None
        self.max_queue_size = max(0, _integer(max_queue_size, 1000))
        self.expire_seconds = None if expire_seconds is None else max(0.0, _number(expire_seconds, 3600.0))
        self.drop_expired = _bool(drop_expired, False)
        self._clock = clock or time.time
        self._records: OrderedDict[str, RetryRecord] = OrderedDict()
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()
        self.loaded = False
        self.last_load_error: str | None = None
        self.dropped_count = 0

    @property
    def state_file(self) -> Path | None:
        return self.path

    @property
    def records(self) -> tuple[RetryRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def _expired(self, record: RetryRecord, now: datetime) -> bool:
        if not record.expires_at:
            return False
        return _parse_datetime(record.expires_at) <= now

    def _prune_locked(self, now: datetime | None = None) -> int:
        current = now or _parse_datetime(self._clock())
        removed = 0
        if self.drop_expired:
            for record_id, record in tuple(self._records.items()):
                if self._expired(record, current):
                    del self._records[record_id]
                    removed += 1
        while len(self._records) > self.max_queue_size:
            self._records.popitem(last=False)
            self.dropped_count += 1
            removed += 1
        return removed

    @staticmethod
    def _sort_locked(records: OrderedDict[str, RetryRecord]) -> None:
        ordered = sorted(
            records.items(),
            key=lambda pair: (_parse_datetime(pair[1].next_attempt_at), pair[0]),
        )
        records.clear()
        records.update(ordered)

    def _save_locked(self) -> None:
        if self.path is None:
            return
        _atomic_dump(
            self.path,
            {
                "version": 1,
                "records": [record.to_dict() for record in self._records.values()],
            },
        )

    def save_sync(self) -> None:
        with self._lock:
            self._prune_locked(_parse_datetime(self._clock()))
            self._sort_locked(self._records)
            self._save_locked()

    async def save(self) -> None:
        async with self._async_lock:
            self.save_sync()

    def _new_record(
        self,
        item: DeliveryItem,
        *,
        attempts: int = 1,
        next_attempt_at: str | datetime | float | None = None,
        last_error: str = "",
        created_at: str | datetime | float | None = None,
        expires_at: str | datetime | float | None = None,
        record_id: str | None = None,
        delay_seconds: float = 0,
        now: float | datetime | None = None,
    ) -> RetryRecord:
        current = _parse_datetime(now if now is not None else self._clock())
        created = _parse_datetime(created_at) if created_at is not None else current
        if next_attempt_at is None:
            due = current + timedelta(seconds=max(0.0, float(delay_seconds)))
            next_value = _now_iso(due)
        elif isinstance(next_attempt_at, datetime):
            next_value = _now_iso(next_attempt_at)
        elif isinstance(next_attempt_at, (int, float)):
            next_value = _now_iso(datetime.fromtimestamp(float(next_attempt_at), timezone.utc))
        else:
            next_value = _now_iso(_parse_datetime(next_attempt_at, default=current))
        if expires_at is None and self.expire_seconds is not None:
            expires_value: str | None = _now_iso(created + timedelta(seconds=self.expire_seconds))
        elif isinstance(expires_at, datetime):
            expires_value = _now_iso(expires_at)
        elif isinstance(expires_at, (int, float)):
            expires_value = _now_iso(datetime.fromtimestamp(float(expires_at), timezone.utc))
        elif expires_at is None:
            expires_value = None
        else:
            expires_value = _now_iso(_parse_datetime(expires_at, default=created))
        identity = record_id or uuid.uuid4().hex
        return RetryRecord(
            record_id=str(identity),
            item=item,
            attempts=max(0, int(attempts)),
            next_attempt_at=next_value,
            last_error=str(last_error),
            created_at=_now_iso(created),
            expires_at=expires_value,
        )

    def enqueue_sync(
        self,
        item: DeliveryItem | RetryRecord | Mapping[str, Any],
        *,
        attempts: int = 1,
        next_attempt_at: str | datetime | float | None = None,
        last_error: str = "",
        created_at: str | datetime | float | None = None,
        expires_at: str | datetime | float | None = None,
        record_id: str | None = None,
        delay_seconds: float = 0,
        now: float | datetime | None = None,
    ) -> RetryRecord | None:
        with self._lock:
            if isinstance(item, RetryRecord):
                record = item
            else:
                if not isinstance(item, DeliveryItem):
                    item = DeliveryItem.from_dict(item)
                record = self._new_record(
                    item,
                    attempts=attempts,
                    next_attempt_at=next_attempt_at,
                    last_error=last_error,
                    created_at=created_at,
                    expires_at=expires_at,
                    record_id=record_id,
                    delay_seconds=delay_seconds,
                    now=now,
                )
            if self.max_queue_size <= 0:
                self.dropped_count += 1
                return None
            self._records[record.record_id] = record
            self._sort_locked(self._records)
            self._prune_locked(_parse_datetime(now if now is not None else self._clock()))
            self._save_locked()
            return record if record.record_id in self._records else None

    async def enqueue(self, item: DeliveryItem | RetryRecord | Mapping[str, Any], **kwargs: Any) -> RetryRecord | None:
        # ``error`` is retained as a compatibility spelling used by older
        # callers; the persisted field is named ``last_error``.
        if "error" in kwargs and "last_error" not in kwargs:
            kwargs["last_error"] = kwargs.pop("error")
        async with self._async_lock:
            return self.enqueue_sync(item, **kwargs)

    # Common queue naming aliases.
    async def put(self, item: DeliveryItem | RetryRecord | Mapping[str, Any], **kwargs: Any) -> RetryRecord | None:
        return await self.enqueue(item, **kwargs)

    def add_sync(self, item: DeliveryItem | RetryRecord | Mapping[str, Any], **kwargs: Any) -> RetryRecord | None:
        return self.enqueue_sync(item, **kwargs)

    async def add(self, item: DeliveryItem | RetryRecord | Mapping[str, Any], **kwargs: Any) -> RetryRecord | None:
        return await self.enqueue(item, **kwargs)

    def _due_locked(self, now: float | datetime | None = None) -> tuple[RetryRecord, ...]:
        current = _parse_datetime(now if now is not None else self._clock())
        self._prune_locked(current)
        self._sort_locked(self._records)
        return tuple(
            record
            for record in self._records.values()
            if _parse_datetime(record.next_attempt_at, default=current) <= current
            and (not self.drop_expired or not self._expired(record, current))
        )

    def due_sync(self, now: float | datetime | None = None, *, limit: int | None = None) -> tuple[RetryRecord, ...]:
        with self._lock:
            due = self._due_locked(now)
            return due if limit is None else due[: max(0, int(limit))]

    async def due(self, now: float | datetime | None = None, *, limit: int | None = None) -> tuple[RetryRecord, ...]:
        async with self._async_lock:
            return self.due_sync(now, limit=limit)

    async def get_due(self, now: float | datetime | None = None, *, limit: int | None = None) -> tuple[RetryRecord, ...]:
        return await self.due(now, limit=limit)

    async def claim_due(self, now: float | datetime | None = None) -> RetryRecord | None:
        return await self.pop_due(now)

    def get_due_sync(self, now: float | datetime | None = None, *, limit: int | None = None) -> tuple[RetryRecord, ...]:
        return self.due_sync(now, limit=limit)

    def claim_due_sync(self, now: float | datetime | None = None) -> RetryRecord | None:
        return self.pop_due_sync(now)

    def _remove_locked(self, record_id: str) -> RetryRecord | None:
        return self._records.pop(str(record_id), None)

    def ack_sync(self, record_id: str) -> bool:
        with self._lock:
            removed = self._remove_locked(record_id)
            if removed is not None:
                self._save_locked()
                return True
            return False

    async def ack(self, record_id: str) -> bool:
        async with self._async_lock:
            return self.ack_sync(record_id)

    async def remove(self, record_id: str) -> bool:
        return await self.ack(record_id)

    def remove_sync(self, record_id: str) -> bool:
        return self.ack_sync(record_id)

    async def pop_due(self, now: float | datetime | None = None) -> RetryRecord | None:
        """Remove and return the earliest due record."""
        async with self._async_lock:
            with self._lock:
                due = self._due_locked(now)
                if not due:
                    return None
                record = self._remove_locked(due[0].record_id)
                self._save_locked()
                return record

    def pop_due_sync(self, now: float | datetime | None = None) -> RetryRecord | None:
        with self._lock:
            due = self._due_locked(now)
            if not due:
                return None
            record = self._remove_locked(due[0].record_id)
            self._save_locked()
            return record

    def reschedule_sync(
        self,
        record_id: str,
        *,
        attempts: int | None = None,
        delay_seconds: float | None = None,
        next_attempt_at: str | datetime | float | None = None,
        last_error: str | None = None,
        item: DeliveryItem | None = None,
        now: float | datetime | None = None,
    ) -> RetryRecord | None:
        with self._lock:
            record = self._records.get(str(record_id))
            if record is None:
                return None
            current = _parse_datetime(now if now is not None else self._clock())
            if next_attempt_at is None:
                due = current + timedelta(seconds=max(0.0, float(delay_seconds or 0)))
                next_value = _now_iso(due)
            elif isinstance(next_attempt_at, datetime):
                next_value = _now_iso(next_attempt_at)
            elif isinstance(next_attempt_at, (int, float)):
                next_value = _now_iso(datetime.fromtimestamp(float(next_attempt_at), timezone.utc))
            else:
                next_value = _now_iso(_parse_datetime(next_attempt_at, default=current))
            updated = RetryRecord(
                record_id=record.record_id,
                item=item or record.item,
                attempts=record.attempts if attempts is None else max(0, int(attempts)),
                next_attempt_at=next_value,
                last_error=record.last_error if last_error is None else str(last_error),
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
            self._records[record.record_id] = updated
            self._sort_locked(self._records)
            self._prune_locked(current)
            self._save_locked()
            return updated

    async def reschedule(self, record_id: str, **kwargs: Any) -> RetryRecord | None:
        async with self._async_lock:
            return self.reschedule_sync(record_id, **kwargs)

    def _load_value_locked(self, value: Any) -> None:
        self._records.clear()
        if isinstance(value, Mapping):
            raw_records = value.get("records", value.get("queue", value.get("items", ())))
        elif isinstance(value, list):
            raw_records = value
        else:
            raw_records = ()
        if isinstance(raw_records, Mapping):
            raw_records = raw_records.values()
        for item in raw_records or ():
            if not isinstance(item, Mapping):
                continue
            try:
                record = RetryRecord.from_dict(item)
            except (TypeError, ValueError, KeyError):
                continue
            if not record.record_id:
                record.record_id = uuid.uuid4().hex
            # Missing due timestamps are immediately eligible rather than
            # getting stuck forever behind an invalid string.
            if not record.next_attempt_at:
                record.next_attempt_at = _now_iso()
            if not record.created_at:
                record.created_at = record.next_attempt_at
            self._records[record.record_id] = record
        self._prune_locked(_parse_datetime(self._clock()))
        self._sort_locked(self._records)

    def load_sync(self) -> bool:
        with self._lock:
            if self.path is None:
                self.loaded = True
                return True
            try:
                value = _read_json(self.path)
            except FileNotFoundError:
                self.loaded = True
                self.last_load_error = None
                return False
            except (OSError, ValueError, TypeError) as exc:
                self._records.clear()
                self.loaded = True
                self.last_load_error = str(exc)
                return False
            self._load_value_locked(value)
            self.loaded = True
            self.last_load_error = None
            self._save_locked()
            return True

    async def load(self) -> bool:
        async with self._async_lock:
            return self.load_sync()

    async def clear(self) -> None:
        async with self._async_lock:
            with self._lock:
                self._records.clear()
                self._save_locked()

    def clear_sync(self) -> None:
        with self._lock:
            self._records.clear()
            self._save_locked()


__all__ = ["DedupStore", "RetryQueue"]
