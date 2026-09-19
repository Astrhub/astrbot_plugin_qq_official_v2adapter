"""AstrBot 4.28.1 compatibility boundary for authenticated management requests."""

import jwt
from astrbot.api.web import request
from astrbot.dashboard.plugin_page_auth import PluginPageAuth
from astrbot.dashboard.services.auth_service import DASHBOARD_JWT_COOKIE_NAME

from . import PLUGIN_NAME
from .errors import V2Error


def require_admin(context):
    try:
        public = request._get_current()
        raw = public._request
        if public.plugin_name != PLUGIN_NAME or raw.app.state.core_lifecycle.star_context is not context:
            raise ValueError
        auth = raw.headers.get("authorization", "")
        if auth and not auth.startswith("Bearer "):
            raise ValueError
        if raw.headers.get("x-api-key") or "api_key" in raw.query_params or "key" in raw.query_params:
            raise ValueError
        token = auth[7:] if auth.startswith("Bearer ") else raw.cookies.get(DASHBOARD_JWT_COOKIE_NAME, "")
        payload = jwt.decode(token, raw.app.state.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "username"]})
        username = payload["username"]
        if (PluginPageAuth.is_asset_token(payload) or not isinstance(username, str)
                or username != context.get_config().get("dashboard", {}).get("username")
                or username != public.username):
            raise ValueError
        origin = raw.headers.get("origin")
        if origin is not None and origin.rstrip("/") != str(raw.base_url).rstrip("/"):
            raise ValueError
        if raw.headers.get("sec-fetch-site") == "cross-site":
            raise ValueError
        return username
    except (AttributeError, RuntimeError, ValueError, KeyError, jwt.PyJWTError) as exc:
        raise V2Error("management_auth_required", "A verified Dashboard administrator session is required.", status=403) from exc
