"""Validate opaque string identifiers without importing AstrBot models."""

import hashlib

from ..errors import V2Error


def text_id(value):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise V2Error("invalid_id", "Expected a nonempty string identifier.")
    return value


def child_operation_id(parent, suffix):
    """Derive a bounded deterministic child without changing existing short IDs."""
    parent = text_id(parent)
    child = parent + ":" + suffix
    if len(child) <= 512:
        return child
    payload = b"qq-v2-child\0" + parent.encode() + b"\0" + suffix.encode()
    return "qq-v2-child-" + hashlib.sha256(payload).hexdigest()
