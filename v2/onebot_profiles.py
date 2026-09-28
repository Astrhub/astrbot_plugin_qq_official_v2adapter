"""Scoped historical profiles and current QQ members exposed as labeled OneBot views."""

from datetime import datetime

from .errors import V2Error
from .models import text_id

SCOPED_IDS = {"group": "member_openid", "c2c": "user_openid", "channel": "channel_user_id", "dm": "channel_user_id"}


def join_seconds(value):
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(when.timestamp()) if when.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def profile_member(group, member):
    fields = member["fields"]
    def value(name):
        return fields[name]["value"] if name in fields else None
    result = {"group_id": group, "user_id": member["user_id"], "id_kind": "member_openid", "partial": True}
    if value("nickname") is not None:
        result["nickname"] = value("nickname")
    if value("bot") is not None:
        result["bot"] = value("bot")
    role = value("last_known_role")
    if member["membership"] == "present" and not member["stale"] and role in ("member", "admin", "owner"):
        result["role"] = role
    stamp = join_seconds(value("joined_at"))
    if stamp is not None:
        result["join_time"] = stamp
    result["_qq"] = {"source": member["source"], "as_of": member["as_of"], "stale": member["stale"],
                     "membership": member["membership"], "current": member["membership"] == "present",
                     "missing_fields": member["missing_fields"], "field_sources": {
                         key: {"source": entry["source"], "as_of": entry["as_of"]} for key, entry in fields.items()}}
    return result


def official_member(group, row, *, source="official_query", current=True, at=None):
    if not isinstance(row, dict):
        raise V2Error("invalid_management_response", "QQ member record is not an object.", status=502)
    user = text_id(row.get("member_openid"))
    result = {"group_id": group, "user_id": user, "id_kind": "member_openid", "partial": True,
              "_qq": {"source": source, "as_of": at, "current": current, "membership": "present" if current else "unconfirmed",
                      "non_atomic": True}}
    if isinstance(row.get("username"), str):
        result["nickname"] = row["username"]
    if row.get("member_role") in ("member", "admin", "owner"):
        result["role"] = row["member_role"]
    if type(row.get("bot")) is bool:
        result["bot"] = row["bot"]
    stamp = join_seconds(row.get("joined_at"))
    if stamp is not None:
        result["join_time"] = stamp
    return result


class OneBotProfiles:
    def __init__(self, client):
        self.client = client
        self.profiles = client._state.profiles
        self.robot = client.identity.robot

    async def member(self, group, user, *, refresh=False):
        group, user = text_id(group), text_id(user)
        if refresh:
            row = await self.client.qq.get_group_member_info(group, user)
            return official_member(group, row, at=self.profiles.store.clock())
        data = await self.profiles.get_member(group, user, mode="prefer_cache")
        return profile_member(group, data)

    async def members(self, group, *, refresh=False):
        group = text_id(group)
        cached = None if refresh else self.profiles.cached_roster(group)
        if cached is not None:
            return [profile_member(group, member) for member in cached]
        snapshot = await self.profiles.refresh_roster(group, with_rows=True)
        if not snapshot["complete"] or len(snapshot["rows"]) != snapshot["count"]:
            raise V2Error("pagination_incomplete", "A concurrent change prevented a complete member list.", status=409)
        return [official_member(group, row, source="roster_query", current=snapshot["continuous"],
                                at=snapshot["finished"]) for row in snapshot["rows"]]

    def stranger(self, user, kind, scope):
        user = text_id(user)
        if not isinstance(kind, str) or not isinstance(scope, str) or ":" not in scope:
            raise V2Error("identity_scope_required", "Specify a scoped OpenID and identity kind.", status=400)
        scene, target = scope.split(":", 1)
        if SCOPED_IDS.get(scene) != kind:
            raise V2Error("identity_scope_required", "Scene and identity kind disagree.", status=400)
        text_id(target)
        record = None
        if self.client._state.cache is not None:
            try:
                record = self.client._state.cache.lookup(self.robot, kind, scope, user)
            except V2Error as exc:
                if exc.code != "identity_not_observed":
                    raise
        profile = None
        try:
            profile = self.profiles.store.get_member(self.robot, scene, target, user, kind=kind)
        except V2Error as exc:
            if exc.code != "identity_not_observed":
                raise
        if record is None and profile is None:
            raise V2Error("identity_not_observed", "No scoped chat or historical profile has been observed.", status=404)
        result = dict(record or {"user_id": user, "id_kind": kind, "scope": scope})
        added = {}
        if profile is not None:
            for field, alias in (("nickname", "nickname"), ("avatar_url", "avatar_url")):
                entry = profile["fields"].get(field)
                if entry is not None and (record is None or entry.get("as_of") is not None and entry["as_of"] > record.get("last_seen", 0)):
                    result[alias] = entry["value"]
                    added[alias] = {"source": entry["source"], "as_of": entry["as_of"]}
            result["_qq"] = {"membership": profile["membership"], "stale": profile["stale"],
                             "as_of": profile["as_of"], "profile_fields": added,
                             "partial": profile["partial"]}
        if record is None and profile is not None:
            result.update(as_of=profile["as_of"], stale=profile["stale"], membership=profile["membership"])
        result.update(source="chat_cache" if record is not None else "profile_cache", partial=True)
        return result
