"""AstrBot integration boundary.

The core pipeline accepts an async callable instead of importing AstrBot.  This
module is the only place that knows how to build a proactive AstrBot message.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

try:
    from .models import TargetRef
except ImportError:  # pragma: no cover - standalone fallback
    from models import TargetRef

LOGGER = logging.getLogger(__name__)


class TargetResolutionError(RuntimeError):
    """Raised when a configured AstrBot message target cannot be resolved."""


class AstrBotDeliveryError(RuntimeError):
    """Raised when AstrBot explicitly rejects a proactive message."""


def build_umo(
    *,
    platform_name: str,
    platform_instance: str,
    message_type: str,
    group_id: str,
    template: str | None = None,
) -> str:
    """Build AstrBot's native UMO, or a configured custom template.

    AstrBot v4.26.8 uses ``platform:type:session_id`` for MessageSession.  The
    instance value remains available to custom/legacy templates, but native
    QQ Official and OneBot targets must not include it.
    """

    values = {
        "platform_name": platform_name,
        "platform_instance": platform_instance,
        "message_type": message_type,
        "group_id": str(group_id),
    }
    pattern = template or "{platform_name}:{message_type}:{group_id}"
    try:
        return pattern.format(**values)
    except KeyError as exc:
        raise TargetResolutionError(f"unknown UMO template field: {exc.args[0]}") from exc


def resolve_target(
    target: Any,
    *,
    available_platforms: Mapping[str, Any] | None = None,
) -> TargetRef:
    """Normalize target configuration and optionally resolve a platform instance.

    ``default`` is accepted only when exactly one instance can be found.  If
    AstrBot does not expose its platform registry to a caller, the configured
    default is retained and the adapter lets ``send_message`` report a useful
    error at runtime.
    """

    get = target.get if hasattr(target, "get") else lambda key, default=None: getattr(target, key, default)
    group_id = str(get("group_id", "") or "").strip()
    # AstrBot can provide a complete UMO without exposing the underlying group
    # openid. In that case the UMO is the authoritative target identifier.
    umo_override = get("umo_override", get("umo", None))
    umo_override = str(umo_override).strip() if umo_override else None
    if not group_id and not umo_override:
        raise TargetResolutionError("target.group_id or target.umo_override is required")

    platform_name = str(get("platform_name", "qq_official") or "qq_official").strip()
    instance = str(get("platform_instance", "default") or "default").strip()
    if instance == "default" and available_platforms is not None:
        candidates = available_platforms.get(platform_name, ())
        if isinstance(candidates, str):
            candidates = (candidates,)
        else:
            candidates = tuple(candidates or ())
        if len(candidates) == 1:
            instance = str(candidates[0])
        elif len(candidates) > 1:
            raise TargetResolutionError(
                f"multiple {platform_name} instances are available; configure platform_instance"
            )

    message_type = str(get("message_type", "GroupMessage") or "GroupMessage").strip()
    template = get("umo_template", None)
    template = str(template).strip() if template else None
    if not umo_override:
        umo_override = build_umo(
            platform_name=platform_name,
            platform_instance=instance,
            message_type=message_type,
            group_id=group_id,
            template=template,
        )
    return TargetRef(
        group_id=group_id,
        platform_name=platform_name,
        platform_instance=instance,
        message_type=message_type,
        umo_override=umo_override,
    )


class NullSender:
    """A sender for CLI dry runs and tests."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, text: str, target_umo: str) -> None:
        self.sent.append((target_umo, text))


class AstrBotSender:
    """Adapt ``Context.send_message`` to the core sender protocol."""

    def __init__(self, context: Any, *, message_chain_factory: Callable[[], Any] | None = None) -> None:
        self.context = context
        self._message_chain_factory = message_chain_factory

    def _make_chain(self, text: str) -> Any:
        if self._message_chain_factory is not None:
            chain = self._message_chain_factory()
            return chain.message(text) if hasattr(chain, "message") else chain
        try:
            from astrbot.api.message import MessageChain  # type: ignore

            return MessageChain().message(text)
        except (ImportError, AttributeError):
            # A small compatibility fallback is useful for fake contexts and
            # for AstrBot versions whose message-chain class is elsewhere.
            return text

    async def send(self, text: str, target_umo: str) -> None:
        result = self.context.send_message(target_umo, self._make_chain(text))
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise AstrBotDeliveryError(
                f"AstrBot rejected proactive message for UMO {target_umo!r}; "
                "verify the exact platform instance ID from /sid"
            )


async def discover_platform_instances(context: Any, platform_name: str) -> dict[str, tuple[str, ...]]:
    """Best-effort discovery across AstrBot versions.

    Public registries have changed names between AstrBot releases.  Discovery
    is deliberately conservative: unknown context shapes return no candidates
    rather than selecting an arbitrary adapter.
    """

    candidates: dict[str, tuple[str, ...]] = {}
    registries = []
    for attr in ("platform_manager", "platforms", "platform_registry"):
        value = getattr(context, attr, None)
        if value is not None:
            registries.append(value)
    for registry in registries:
        try:
            value = registry.get(platform_name) if hasattr(registry, "get") else None
            if value is None:
                continue
            if isinstance(value, Mapping):
                names = value.keys()
            elif isinstance(value, (list, tuple, set)):
                names = [getattr(item, "id", getattr(item, "instance_id", item)) for item in value]
            else:
                names = [getattr(value, "id", getattr(value, "instance_id", value))]
            candidates[platform_name] = tuple(str(name) for name in names if name)
            break
        except Exception:  # pragma: no cover - third-party compatibility path
            LOGGER.debug("Unable to inspect AstrBot platform registry", exc_info=True)
    return candidates


Sender = Callable[[str, str], Awaitable[None]]
