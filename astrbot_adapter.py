"""AstrBot integration boundary.

The core pipeline accepts an async callable instead of importing AstrBot.  This
module is the only place that knows how to build a proactive AstrBot message.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit

try:
    from .models import TargetRef
except ImportError:  # pragma: no cover - standalone fallback
    from models import TargetRef

LOGGER = logging.getLogger(__name__)


class TargetResolutionError(RuntimeError):
    """Raised when a configured AstrBot message target cannot be resolved."""


class AstrBotDeliveryError(RuntimeError):
    """Raised when AstrBot explicitly rejects a proactive message."""


class AstrBotOpenAPIError(RuntimeError):
    """Raised when AstrBot's OpenAPI IM endpoint rejects a message."""


class AstrBotOpenAPISender:
    """Send proactive messages through AstrBot's local OpenAPI IM endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        api_key_env: str,
        auth_header: str = "bearer",
        timeout: float = 10.0,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urlsplit(str(endpoint).strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OpenAPI endpoint must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OpenAPI endpoint must not contain credentials, query, or fragment")
        if auth_header not in {"bearer", "x-api-key"}:
            raise ValueError("OpenAPI auth_header must be bearer or x-api-key")
        self.api_key_env = str(api_key_env).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("OpenAPI api_key_env must be a valid environment variable name")
        self.endpoint = endpoint.rstrip("/")
        self.auth_header = auth_header
        self.timeout = max(0.1, float(timeout))
        self._environ = environ

    @property
    def key_configured(self) -> bool:
        environment = self._environ if self._environ is not None else os.environ
        return bool(str(environment.get(self.api_key_env, "")).strip())

    def diagnostics(self) -> dict[str, Any]:
        parsed = urlsplit(self.endpoint)
        return {
            "mode": "openapi",
            "endpoint": f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}",
            "auth_header": self.auth_header,
            "key_configured": self.key_configured,
        }

    def _key(self) -> str:
        environment = self._environ if self._environ is not None else os.environ
        key = str(environment.get(self.api_key_env, "")).strip()
        if not key:
            raise AstrBotOpenAPIError(
                f"OpenAPI API key is missing from environment variable {self.api_key_env}"
            )
        return key

    async def send(self, text: str, target_umo: str) -> None:
        key = self._key()
        payload = json.dumps({"umo": str(target_umo), "message": str(text)}, ensure_ascii=False).encode("utf-8")
        if self.auth_header == "bearer":
            auth_value = f"Bearer {key}"
            headers = {"Authorization": auth_value}
        else:
            headers = {"X-API-Key": key}
        headers.update({"Content-Type": "application/json", "Accept": "application/json"})

        def post() -> int:
            class _NoRedirect(request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise AstrBotOpenAPIError("OpenAPI endpoint returned a redirect")

            opener = request.build_opener(_NoRedirect)
            req = request.Request(self.endpoint, data=payload, headers=headers, method="POST")
            try:
                with opener.open(req, timeout=self.timeout) as response:
                    response.read()
                    return int(response.status)
            except error.HTTPError as exc:
                raise AstrBotOpenAPIError(f"OpenAPI IM request returned HTTP {exc.code}") from None
            except (error.URLError, TimeoutError, OSError, socket.timeout) as exc:
                raise AstrBotOpenAPIError(f"OpenAPI IM request failed: {type(exc).__name__}") from None

        status = await asyncio.to_thread(post)
        if status < 200 or status >= 300:
            raise AstrBotOpenAPIError(f"OpenAPI IM request returned HTTP {status}")


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


__all__ = [
    "AstrBotDeliveryError",
    "AstrBotOpenAPIError",
    "AstrBotOpenAPISender",
    "AstrBotSender",
    "NullSender",
    "Sender",
    "TargetResolutionError",
    "build_umo",
    "discover_platform_instances",
    "resolve_target",
]
