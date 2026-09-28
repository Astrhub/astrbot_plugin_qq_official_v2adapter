"""Validate opaque string identifiers without importing AstrBot models."""

from ..errors import V2Error


def text_id(value):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise V2Error("invalid_id", "Expected a nonempty string identifier.")
    return value
