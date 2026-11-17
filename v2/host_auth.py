"""Use AstrBot's authenticated Plugin Web API context for management requests."""

import jwt
from astrbot.api.web import request
from astrbot.dashboard.plugin_page_auth import PluginPageAuth

from . import PLUGIN_NAME
from .errors import V2Error


def _is_plugin_page_asset_token(raw):
    authorization = raw.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return False
    try:
        payload = jwt.decode(authorization[7:], options={"verify_signature": False, "verify_exp": False}, algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return PluginPageAuth.is_asset_token(payload)


def require_admin(context):
    try:
        public = request._get_current()
        raw = public._request
        if _is_plugin_page_asset_token(raw):
            raise ValueError
        if public.plugin_name != PLUGIN_NAME or raw.app.state.core_lifecycle.star_context is not context:
            raise ValueError
        username = public.username
        if not isinstance(username, str) or not username.strip():
            raise ValueError
        return username
    except (AttributeError, RuntimeError, ValueError) as exc:
        raise V2Error("management_auth_required", "A verified Dashboard administrator session is required.", status=403) from exc
