"""Injection boundaries for transport, ledger and profile services."""

from typing import Any, Callable, Protocol


class HTTPPort(Protocol):
    async def request(self, spec: Any, *, before_send: Callable | None = None) -> Any: ...


class ProfilePort(Protocol):
    def get_member(self, robot: Any, scene: str, scope: str, user_id: str) -> dict: ...
    def merge(self, robot: Any, scene: str, scope: str, user_id: str,
              fields: dict, *, source: str, as_of: float | None, received: float) -> bool: ...


class LifecyclePort(Protocol):
    def check(self) -> None: ...
    async def close(self) -> None: ...
