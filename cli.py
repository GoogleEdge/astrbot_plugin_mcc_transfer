"""Standalone management CLI for the MCC transfer core."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from astrbot_adapter import NullSender, resolve_target
from config import AppConfig, ConfigError
from control import read_json, request_reload
from delivery import DeliveryPipeline
from filter_chain import FilterChain
from persistence import DedupStore, RetryQueue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the AstrBot MCC transfer plugin")
    parser.add_argument("--config", default="config.ini", help="INI configuration path")
    parser.add_argument("--data-dir", default="data", help="Standalone state directory")
    parser.add_argument("--check-config", action="store_true", help="Validate configuration and exit")
    parser.add_argument("--test-mcp", action="store_true", help="Perform one MCP handshake")
    parser.add_argument("--dry-run", action="store_true", help="Process a message without sending it")
    parser.add_argument("--status", action="store_true", help="Show persisted status")
    parser.add_argument("--reload-config", action="store_true", help="Write a standalone reload request")
    parser.add_argument("--sender", default="brightmoon")
    parser.add_argument("--message", default="hello")
    return parser


def _load(path: str) -> AppConfig:
    return AppConfig.from_ini(Path(path))


async def _test_mcp(config: AppConfig) -> int:
    from mcp_client import MCPClient

    received: list[tuple[str, str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def callback(sender: str, message: str, raw: dict[str, Any]) -> None:
        received.append((sender, message, raw))
        done.set()

    client = MCPClient(config.mcp.host, config.mcp.port, config.mcp.password, callback)
    task = asyncio.create_task(client.connect())
    try:
        await asyncio.wait_for(done.wait(), timeout=max(config.mcp.connect_timeout, 1) * 2)
        print(json.dumps({"received": received[-1] if received else None}, ensure_ascii=False))
        return 0
    except asyncio.TimeoutError:
        print("MCP handshake succeeded but no event was received", file=sys.stderr)
        return 2
    finally:
        await client.disconnect()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _dry_run(config: AppConfig, sender_name: str, text: str, data_dir: str) -> int:
    null_sender = NullSender()
    dedup = DedupStore(Path(data_dir) / "dry-run-dedup.json", cache_size=1000, ttl_seconds=300)
    retry = RetryQueue(Path(data_dir) / "dry-run-retry.json", max_queue_size=100)
    await dedup.load()
    await retry.load()
    pipeline = DeliveryPipeline(
        filter_chain=FilterChain(config.filter, dedup=dedup),
        dedup=dedup,
        retry_queue=retry,
        sender=null_sender,
        target_umo=resolve_target(config.target).umo,
        formatter_config=config.target,
        security_config=config.security,
        retry_config=config.retry,
    )
    result = await pipeline.handle_raw(sender_name, text, {"type": "event", "event": "PlayerMessage"})
    print(json.dumps({"accepted": bool(result), "sent": null_sender.sent}, ensure_ascii=False))
    return 0 if result else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not any((args.check_config, args.test_mcp, args.dry_run, args.status, args.reload_config)):
        _parser().print_help()
        return 0
    if args.status:
        value = read_json(Path(args.data_dir) / "status.json", {})
        print(json.dumps(value or {}, ensure_ascii=False, indent=2))
        return 0
    if args.reload_config:
        try:
            _load(args.config)
        except (ConfigError, OSError, ValueError) as exc:
            print(f"invalid configuration: {exc}", file=sys.stderr)
            return 2
        request_reload(Path(args.data_dir) / "reload.request.json", args.config)
        print(f"reload request written for {args.config}")
        return 0
    try:
        config = _load(args.config)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print("configuration is valid")
        return 0
    if args.test_mcp:
        return asyncio.run(_test_mcp(config))
    if args.dry_run:
        return asyncio.run(_dry_run(config, args.sender, args.message, args.data_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
