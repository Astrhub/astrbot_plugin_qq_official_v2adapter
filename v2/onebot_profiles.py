"""Scoped historical profiles and current QQ members exposed as labeled OneBot views."""

from datetime import datetime

from .errors import V2Error, unsupported
from .models import text_id

SCOPED_IDS = {"group": "member_openid", "c2c": "user_openid", "channel": "channel_user_id", "dm": "channel_user_id"}


def proven_text(value):
    return value if isinstance(value, str) and value else None


def join_seconds(value):
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(when.timestamp()) if when.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def profile_member(group, member, *, role_stale_at):
    fields = member["fields"]
    def value(name):
        return fields[name]["value"] if name in fields else None
    result = {"group_id": group, "user_id": member["user_id"], "id_kind": "member_openid", "partial": True}
    if value("nickname") is not None:
        result["nickname"] = value("nickname")
    if value("bot") is not None:
        result["bot"] = value("bot")
    role = fields.get("last_known_role", {})
    as_of = role.get("as_of")
    if (member["membership"] == "present" and type(as_of) in (int, float) and
            as_of > role_stale_at and role.get("value") in ("member", "admin", "owner")):
        result["role"] = role["value"]
    stamp = join_seconds(value("joined_at"))
    if stamp is not None:
        result["join_time"] = stamp
    result["_qq"] = {"source": member["source"], "as_of": member["as_of"], "stale": member["stale"],
                     "membership": member["membership"], "current": member["membership"] == "present",
                     "missing_fields": member["missing_fields"], "field_sources": {
                         key: {"source": entry["source"], "as_of": entry["as_of"]} for key, entry in fields.items()}}
    if value("union_openid"):
        result["_qq"]["union_openid"] = value("union_openid")
    if value("avatar_url"):
        result["_qq"]["avatar"] = value("avatar_url")
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
    if union := proven_text(row.get("union_openid")):
        result["_qq"]["union_openid"] = union
    if avatar := proven_text(row.get("avatar")):
        result["_qq"]["avatar"] = avatar
    return result


def project_group_info(data):
    result = {"group_id": data["group_openid"], "group_name": data["group_name"],
              "member_count": data["group_member_num"], "partial": True, "permission": "unknown",
              "_qq": {"source": "official_query"}}
    if isinstance(data.get("union_group_openid"), str) and data["union_group_openid"]:
        result["_qq"]["union_group_openid"] = data["union_group_openid"]
    return result


def project_login_info(data):
    if (not isinstance(data, dict) or data.get("bot") is not True
            or not isinstance(data.get("username"), str) or not data["username"]):
        raise V2Error("invalid_management_response", "A complete real bot ID and username are required for login_info.",
                      status=502)
    return {"user_id": text_id(data.get("id")), "nickname": data["username"],
            "id_kind": "channel_user_id", "source": "official_profile"}



class OneBotProfiles:
    def __init__(self, client):
        self.client = client
        self.profiles = client._state.profiles
        self.robot = client.identity.robot

    async def member(self, group, user, *, refresh=False):
        group, user = text_id(group), text_id(user)
        if refresh:
            row = await self.client.qq.get_group_member_info(group, user)
            at = self.profiles.store.clock() if self.profiles is not None else None
            return official_member(group, row, at=at)
        data = await self.profiles.get_member(group, user, mode="prefer_cache")
        return profile_member(group, data, role_stale_at=self.profiles.store.clock() - self.profiles.store.stale_seconds)

    async def members(self, group, *, refresh=False):
        group = text_id(group)
        cached = None if refresh else self.profiles.cached_roster(group)
        if cached is not None:
            cutoff = self.profiles.store.clock() - self.profiles.store.stale_seconds
            return [profile_member(group, member, role_stale_at=cutoff) for member in cached]
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
            union = profile["fields"].get("union_openid")
            if union is not None:
                added["union_openid"] = {"source": union["source"], "as_of": union["as_of"]}
            result["_qq"] = {"membership": profile["membership"], "stale": profile["stale"],
                             "as_of": profile["as_of"], "profile_fields": added,
                             "partial": profile["partial"]}
            if union is not None and proven_text(union.get("value")):
                result["_qq"]["union_openid"] = union["value"]
        if record is None and profile is not None:
            result.update(as_of=profile["as_of"], stale=profile["stale"], membership=profile["membership"])
        result.update(source="chat_cache" if record is not None else "profile_cache", partial=True)
        qq = result.setdefault("_qq", {})
        if avatar := proven_text(result.get("avatar_url")):
            qq.setdefault("avatar", avatar)
        return result

    async def stranger_live(self, user, kind, scope):
        user = text_id(user)
        if not isinstance(kind, str) or not isinstance(scope, str) or ":" not in scope:
            raise V2Error("identity_scope_required", "Specify a scoped OpenID and identity kind.", status=400)
        scene, target = scope.split(":", 1)
        if SCOPED_IDS.get(scene) != kind:
            raise V2Error("identity_scope_required", "Scene and identity kind disagree.", status=400)
        text_id(target)
        if scene == "c2c":
            if user != target:
                raise V2Error("identity_mismatch", "C2C user info scope must match the user OpenID.", status=409)
            data = await self.client.qq.get_user_info(user)
            result = {"user_id": user, "id_kind": kind, "scope": scope, "nickname": data["username"],
                      "source": "official_query", "partial": True,
                      "_qq": {"source": "official_query", "allow_proactive_msg": data["allow_proactive_msg"]}}
            if avatar := proven_text(data.get("avatar")):
                result["_qq"]["avatar"] = avatar
            if union := proven_text(data.get("union_openid")):
                result["_qq"]["union_openid"] = union
            return result
        if scene == "group":
            row = await self.client.qq.get_group_member_info(target, user)
            at = self.profiles.store.clock() if self.profiles is not None else None
            result = official_member(target, row, at=at)
            result["scope"] = scope
            result["source"] = "official_query"
            return result
        raise unsupported("A live OpenID profile lookup is not available.")
