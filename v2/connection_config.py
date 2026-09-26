"""Normalize owned platform configuration without writing the host configuration."""
import copy

from . import PLATFORM_TYPE, PLATFORM_TYPES, WEBHOOK_TYPE
from .errors import V2Error

DEFAULT_INTENTS = 1174409216
MAX_AUTO_SHARDS = 32


def normalize_connection(config):
    value = copy.deepcopy(config)
    kind = value.get("type", PLATFORM_TYPE)
    if kind not in PLATFORM_TYPES:
        raise V2Error("invalid_transport", "Not an owned QQ V2 platform type.")
    transport = value.get("transport", "webhook" if kind == WEBHOOK_TYPE else "websocket")
    if transport not in {"websocket", "webhook"} or kind == WEBHOOK_TYPE and transport != "webhook":
        raise V2Error("config_conflict", "Platform type and legacy transport disagree.")
    environment = value.get("environment", "sandbox" if value.get("is_sandbox") is True else "production")
    if environment not in {"production", "sandbox"}:
        raise V2Error("invalid_environment", "Use production or sandbox.")
    if "is_sandbox" in value and (type(value["is_sandbox"]) is not bool or value["is_sandbox"] != (environment == "sandbox")):
        raise V2Error("config_conflict", "Sandbox switch and legacy environment disagree.")
    mode = value.get("shard_mode", "manual" if "shard" in value else "auto")
    shard = value.get("shard", [0, 1])
    if mode not in {"auto", "manual"}:
        raise V2Error("invalid_shard", "Shard mode must be auto or manual.")
    if (not isinstance(shard, (list, tuple)) or len(shard) != 2
            or any(type(n) is not int for n in shard) or not 0 <= shard[0] < shard[1] <= 1024):
        raise V2Error("invalid_shard", "Expected [index, count], count at most 1024.")
    if (transport == "webhook" or mode == "auto") and list(shard) != [0, 1]:
        raise V2Error("invalid_shard", "Only manual WebSocket mode accepts an explicit shard pair.")
    intents = value.get("intents", DEFAULT_INTENTS)
    if type(intents) is not int or not 0 <= intents < 2**32:
        raise V2Error("invalid_intents", "Expected a uint32 intents mask.")
    value.update(type=WEBHOOK_TYPE if transport == "webhook" else PLATFORM_TYPE, transport=transport,
                 environment=environment, is_sandbox=environment == "sandbox", shard_mode=mode,
                 shard=list(shard), intents=intents)
    return value


def saved_connection(config):
    value = normalize_connection(config)
    for key in ("environment", "transport", "unified_webhook_mode", "logo_token"):
        value.pop(key, None)
    return value


def receiver_conflict(left, right):
    if left.platform_id == right.platform_id:
        return True
    return left.robot == right.robot and (
        "webhook" in (left.transport, right.transport) or "auto" in (left.shard_mode, right.shard_mode)
        or left.shard[1] != right.shard[1] or left.shard[0] == right.shard[0])
