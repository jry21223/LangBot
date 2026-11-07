"""Additive contracts for replayable native sources and Host-owned sends."""

from __future__ import annotations

from ..api.http.authz import AuthorizationError


class NativeAccessDenied(AuthorizationError, PermissionError):
    """A precise native capability denial, rendered as HTTP 403."""


from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class SourceBinding:
    source: str
    account: str
    scope: str
    revision: str
    read_only: bool = True


@dataclass(frozen=True)
class SourceEnvelope:
    record: str
    version: str
    reference: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class SourceUnresolved:
    event_id: str
    source_sequence: int
    event_cursor: str
    source: str
    scope: str
    reason: str = 'native_unresolved'


@dataclass(frozen=True)
class SourcePage:
    previous_cursor: str | None
    next_cursor: str
    events: tuple[SourceEnvelope, ...]
    complete: bool = True
    gaps: tuple[str, ...] = ()
    unresolved: tuple[SourceUnresolved, ...] = ()


@dataclass(frozen=True)
class AcceptanceReceipt:
    page_id: str
    cursor: str
    event_ids: tuple[str, ...]


Authorize = Callable[[], Awaitable[None]]


class SendProvider(Protocol):
    async def send(
        self, operation_key: str, target_type: str, target_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def lookup(self, operation_key: str) -> dict[str, Any] | None: ...
