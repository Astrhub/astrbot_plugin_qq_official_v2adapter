"""Whitelisted, length-bounded send-failure summaries for host-facing boundaries."""

from ..errors import V2Error

_SAFE_TEXT = 128


def _clean(value):
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if 32 <= ord(ch) < 127)[:_SAFE_TEXT]
    return value or None


def failure_summary(error):
    """Build the safe diagnostic summary of one final send failure."""
    if not isinstance(error, V2Error):
        return None
    delivery = ((error.details or {}).get("delivery")) if isinstance(error.details, dict) else None
    summary = {
        "code": _clean(error.code),
        "business_code": error.business_code if type(error.business_code) is int else None,
        "http_status": error.http_status if type(error.http_status) is int else None,
        "phase": _clean(error.phase),
        "operation_id": _clean(error.operation_id),
        "trace_id": _clean(error.trace_id),
    }
    if isinstance(delivery, dict):
        summary["delivery_mode"] = _clean(delivery.get("mode"))
        attempts = delivery.get("attempts")
        summary["delivery_attempts"] = len(attempts) if isinstance(attempts, list) else None
        reason = delivery.get("reason")
        if isinstance(reason, dict):
            summary["mode_switch_reason"] = _clean(reason.get("code"))
        skipped = delivery.get("fallback_skipped")
        if isinstance(skipped, dict):
            summary["fallback_skipped"] = _clean(skipped.get("code"))
    return {key: value for key, value in summary.items() if value is not None}


def log_send_failure(logger, error, *, boundary):
    """Log one safe summary when a host-facing send finally failed."""
    summary = failure_summary(error)
    if summary is None:
        return
    logger.warning("QQ V2 send failed at %s: %s", boundary, summary)
