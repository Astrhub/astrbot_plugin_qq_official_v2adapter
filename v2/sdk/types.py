"""Public SDK data shapes without host dependencies."""

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict


@dataclass(frozen=True)
class CallOptions:
    operation_id: str | None = None
    owner: object | None = None


class ProfileResult(TypedDict):
    robot: str
    scene: str
    scope: str
    id_kind: str
    user_id: str
    fields: dict[str, Any]
    membership: Literal["present", "left", "unknown"]
    revision: int
    stale: bool
    partial: bool
    missing_fields: list[str]
    source: str | None
    as_of: float | None
    refresh_error: NotRequired[str]
