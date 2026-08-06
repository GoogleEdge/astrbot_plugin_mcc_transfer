from pathlib import Path

import pytest

from astrbot_adapter import NullSender, resolve_target
from delivery import DeliveryPipeline
from filter_chain import FilterChain
from persistence import DedupStore, RetryQueue


@pytest.mark.asyncio
async def test_dedup_persists_and_delivery_sends(tmp_path: Path):
    dedup = DedupStore(tmp_path / "dedup.json", cache_size=10, ttl_seconds=300)
    retry = RetryQueue(tmp_path / "retry.json", max_queue_size=10)
    await dedup.load()
    await retry.load()
    sender = NullSender()
    pipeline = DeliveryPipeline(
        filter_chain=FilterChain({"ignore_empty_messages": True}, dedup=dedup),
        dedup=dedup,
        retry_queue=retry,
        sender=sender,
        target_umo=resolve_target({"group_id": "123"}).umo,
        formatter_config={"message_template": "[{sender}] {message}"},
        security_config={"max_message_length": 20, "split_long_messages": True, "rate_limit_per_second": 0},
        retry_config={"max_attempts": 2},
    )
    result = await pipeline.handle_raw("Alex", "hello", {"event": "PlayerMessage", "type": "event"})
    assert result.sent
    assert sender.sent == [("qq_official:GroupMessage:123", "[Alex] hello")]
    duplicate = await pipeline.handle_raw("Alex", "hello", {"event": "PlayerMessage", "type": "event"})
    assert duplicate.filtered
    assert duplicate.reason == "duplicate_message"


@pytest.mark.asyncio
async def test_retry_queue_round_trip(tmp_path: Path):
    path = tmp_path / "retry.json"
    queue = RetryQueue(path, max_queue_size=10)
    await queue.load()
    item = {
        "fingerprint": "fp",
        "payloads": ["payload"],
        "sender": "Alex",
        "original_message": "payload",
        "kind": "chat",
        "created_at": "2026-01-01T00:00:00+00:00",
        "target_umo": "target",
    }
    record = await queue.enqueue(item, error="offline", delay_seconds=0)
    assert record is not None
    restored = RetryQueue(path, max_queue_size=10)
    await restored.load()
    assert len(restored) == 1
    due = await restored.claim_due()
    assert due is not None
    assert due.item.fingerprint == "fp"
