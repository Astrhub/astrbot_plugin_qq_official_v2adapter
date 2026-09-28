"""Explicit group and robot-level QQ mutations without implicit member lookups."""

from datetime import datetime

from ...errors import V2Error
from ..identifiers import text_id
from .messages import segment


def ids(values, limit=20, *, numbers=False):
    if not isinstance(values, list) or not 1 <= len(values) <= limit:
        raise V2Error("invalid_members", f"Supply 1..{limit} explicit identifiers.")
    normalized = [text_id(value) for value in values]
    if len(set(normalized)) != len(normalized) or numbers and any(not value.isascii() or not value.isdecimal() for value in normalized):
        raise V2Error("invalid_members", "Duplicate identities or non-decimal QQ numbers are not allowed.")
    return normalized


def timestamp(value):
    if not isinstance(value, str) or not value:
        raise V2Error("invalid_timestamp", "An RFC3339 timestamp is required.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise V2Error("invalid_timestamp", "An RFC3339 timestamp with timezone is required.")
    return value


def partial_remove(data, members):
    if (not isinstance(data, dict) or data.get("remove_members_result") != "success"
            or not isinstance(data.get("add_to_member_blacklist_fail_openids"), list)):
        raise V2Error("invalid_management_response", "QQ did not confirm group removal.", status=502, phase="result_unknown")
    failed = data["add_to_member_blacklist_fail_openids"]
    if any(not isinstance(member, str) or member not in members for member in failed):
        raise V2Error("invalid_management_response", "QQ returned unrelated removal identities.", status=502, phase="result_unknown")
    if failed:
        raise V2Error("partial_failure", "Members were removed, but blacklist changes failed; do not repeat removal.",
                      status=502, phase="partial", details={"removed": members, "blacklist_failed": failed})


def partial_blacklist(data, members):
    if not isinstance(data, dict) or not isinstance(data.get("fail_openids"), list):
        raise V2Error("invalid_management_response", "QQ blacklist outcome is incomplete.", status=502, phase="result_unknown")
    failed = data["fail_openids"]
    if any(not isinstance(member, str) or member not in members for member in failed):
        raise V2Error("invalid_management_response", "QQ returned unrelated blacklist identities.", status=502, phase="result_unknown")
    if failed:
        raise V2Error("partial_failure", "QQ confirmed only some blacklist changes.", status=502, phase="partial",
                      details={"blacklist_succeeded": [m for m in members if m not in failed], "blacklist_failed": failed})


def strategy_groups(openids, group_ids):
    if (openids is None) == (group_ids is None):
        raise V2Error("invalid_groups", "Supply group OpenIDs or QQ group numbers, never both.")
    if openids is not None:
        return {"group_openids": ids(openids, 100)}
    if (not isinstance(group_ids, list) or not 1 <= len(group_ids) <= 100
            or any(type(value) is not int or not 0 < value < 2**64 for value in group_ids)
            or len(set(group_ids)) != len(group_ids)):
        raise V2Error("invalid_groups", "Supply at most 100 explicit uint64 QQ group numbers.")
    return {"group_ids": group_ids}


class NativeGroupAdminMixin:
    async def generate_url_link(self, callback_data: str = "", *, operation_id=None) -> dict:
        """Create a QQ share URL without persisting the returned URL."""
        if not isinstance(callback_data, str) or len(callback_data) > 32:
            raise V2Error("invalid_callback_data", "Share callback data is at most 32 characters.")
        return await self._native_write("POST", "/v2/generate_url_link", {"callback_data": callback_data},
                                        kind="share_link", operation_id=operation_id)

    async def set_group_restrict_chat_setting(self, group_openid: str, members: list[dict], *, operation_id=None) -> dict | None:
        """Apply at most twenty explicit mute changes, without requiring member GET permission."""
        if not isinstance(members, list) or not 1 <= len(members) <= 20:
            raise V2Error("invalid_members", "Mute at most 20 explicit members.")
        normalized = []
        for row in members:
            if not isinstance(row, dict) or row.get("op") not in ("add", "update", "del"):
                raise V2Error("invalid_members", "Each mute needs add, update or del.")
            member = text_id(row.get("member_openid"))
            if row.get("op") != "del":
                timestamp(row.get("mute_expire_at"))
            elif "mute_expire_at" in row and row["mute_expire_at"] != "":
                timestamp(row["mute_expire_at"])
            normalized.append({**row, "member_openid": member})
        if len({row["member_openid"] for row in normalized}) != len(normalized):
            raise V2Error("invalid_members", "A member may only occur once per batch.")
        return await self._native_write("POST", f"/v2/groups/{segment(group_openid)}/restrict_chat_setting",
                                        {"members": normalized}, kind="group_restrict_chat", operation_id=operation_id,
                                        scene="group", target=group_openid)

    async def batch_remove_group_members(self, group_openid: str, member_openids: list[str],
                                         add_to_member_blacklist: bool = False, *, operation_id=None) -> dict:
        """Remove at most twenty members in one QQ call and expose partial blacklist failure."""
        members = ids(member_openids)
        if type(add_to_member_blacklist) is not bool:
            raise V2Error("invalid_members", "Blacklist choice must be boolean.")
        return await self._native_write("POST", f"/v2/groups/{segment(group_openid)}/batch_remove_members",
            {"member_openids": members, "add_to_member_blacklist": add_to_member_blacklist},
            kind="batch_remove_members", scene="group", target=group_openid, operation_id=operation_id,
            response_check=lambda data: partial_remove(data, members))

    async def set_group_member_blacklist(self, group_openid: str, op: str, member_openids: list[str],
                                         *, operation_id=None) -> dict:
        """Change an explicit group blacklist without kicking members first."""
        if op not in ("add", "del"):
            raise V2Error("invalid_operation", "Blacklist action must be add or del.")
        members = ids(member_openids)
        return await self._native_write("POST", f"/v2/groups/{segment(group_openid)}/member_blacklist",
            {"op": op, "member_openids": members}, kind="group_member_blacklist", scene="group",
            target=group_openid, operation_id=operation_id, response_check=lambda data: partial_blacklist(data, members))

    async def approve_group_join_request(self, group_openid: str, member_openid: str, op: str,
                                         join_request_id: str | None = None, reject_reason: str | None = None,
                                         add_to_member_blacklist: bool | None = None, *, operation_id=None) -> dict | None:
        """Submit the official approval fields without inventing an application flag."""
        if op not in ("approve", "decline") or (op == "approve" and (reject_reason is not None or add_to_member_blacklist is not None)):
            raise V2Error("invalid_approval", "Only decline accepts a rejection reason or blacklist flag.")
        if join_request_id is not None:
            text_id(join_request_id)
        if reject_reason is not None and (not isinstance(reject_reason, str) or len(reject_reason) > 200):
            raise V2Error("invalid_approval", "Rejection reason exceeds its bounded length.")
        if add_to_member_blacklist is not None and type(add_to_member_blacklist) is not bool:
            raise V2Error("invalid_approval", "Blacklist flag must be boolean.")
        body = {"op": op, **({"join_request_id": join_request_id} if join_request_id is not None else {}),
                **({"reject_reason": reject_reason} if reject_reason is not None else {}),
                **({"add_to_member_blacklist": add_to_member_blacklist} if add_to_member_blacklist is not None else {})}
        return await self._native_write("POST", f"/v2/groups/{segment(group_openid)}/approval_join_request/{segment(member_openid)}",
            body, kind="approve_group_join_request", scene="group", target=group_openid, operation_id=operation_id)

    async def create_join_approval_strategy(self, *, group_openids=None, group_ids=None, is_enable="on",
                                            expire_at=None, remark=None, operation_id=None) -> dict:
        """Create a robot-level strategy with explicitly supplied group identifiers."""
        body = strategy_groups(group_openids, group_ids)
        if is_enable not in ("on", "off") or remark is not None and (not isinstance(remark, str) or len(remark) > 255):
            raise V2Error("invalid_strategy", "Strategy enable state or remark is invalid.")
        body["is_enable"] = is_enable
        if expire_at is not None:
            body["expire_at"] = timestamp(expire_at) if expire_at else ""
        if remark is not None:
            body["remark"] = remark
        return await self._native_write("POST", "/v2/groups/join_approval_strategy", body,
                                        kind="create_join_strategy", operation_id=operation_id)

    async def update_join_approval_strategy(self, strategy_id: str, *, is_enable=None, expire_at=None,
                                            group_action=None, remark=None, operation_id=None) -> dict | None:
        """Update a strategy with explicit, single-form group actions."""
        body = {}
        if is_enable is not None:
            if is_enable not in ("on", "off"):
                raise V2Error("invalid_strategy", "Use on or off.")
            body["is_enable"] = is_enable
        if expire_at is not None:
            body["expire_at"] = timestamp(expire_at) if expire_at else ""
        if remark is not None:
            if not isinstance(remark, str) or len(remark) > 255:
                raise V2Error("invalid_strategy", "Remark exceeds 255 characters.")
            body["remark"] = remark
        if group_action is not None:
            if not isinstance(group_action, dict) or group_action.get("op") not in ("add", "del"):
                raise V2Error("invalid_strategy", "Group action must be add or del.")
            groups = strategy_groups(group_action.get("group_openids"), group_action.get("group_ids"))
            body["group_action"] = {"op": group_action["op"], **groups}
        if not body:
            raise V2Error("invalid_strategy", "At least one strategy field must be supplied.")
        return await self._native_write("PATCH", f"/v2/groups/join_approval_strategy/{segment(strategy_id)}",
                                        body, kind="update_join_strategy", operation_id=operation_id)

    async def delete_join_approval_strategy(self, strategy_id: str, *, operation_id=None) -> dict | None:
        """Delete an explicit robot-level strategy."""
        return await self._native_write("DELETE", f"/v2/groups/join_approval_strategy/{segment(strategy_id)}",
                                        None, kind="delete_join_strategy", operation_id=operation_id)

    async def execute_join_approval_strategy(self, strategy_id: str, *, operation_id=None) -> dict | None:
        """Trigger one strategy evaluation through the write ledger."""
        return await self._native_write("POST", f"/v2/groups/join_approval_strategy/{segment(strategy_id)}/execute",
                                        {}, kind="execute_join_strategy", operation_id=operation_id)

    async def set_join_approval_whitelist(self, strategy_id: str, op: str, whitelist_users: list[str],
                                          *, operation_id=None) -> dict:
        """Change up to ten thousand caller-supplied QQ numbers without OpenID conversion."""
        if op not in ("add", "del"):
            raise V2Error("invalid_strategy", "Whitelist operation must be add or del.")
        numbers = ids(whitelist_users, 10000, numbers=True)
        return await self._native_write("POST", f"/v2/groups/join_approval_strategy/{segment(strategy_id)}/whitelist_users",
                                        {"op": op, "whitelist_users": numbers}, kind="set_join_whitelist", operation_id=operation_id)
