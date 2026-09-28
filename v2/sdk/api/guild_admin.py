"""Named guild, channel, role, permission and content mutations."""

from ...errors import V2Error
from ..identifiers import text_id
from .group_admin import ids
from .messages import segment


def patch_fields(fields, allowed):
    if not fields or fields.keys() - allowed:
        raise V2Error("invalid_params", "Specify supported nonempty mutation fields.")
    return fields


def mute_fields(end, seconds):
    if end is None and seconds is None:
        raise V2Error("invalid_mute", "Supply a mute end or relative seconds.")
    if any(not isinstance(value, str) or not value.isascii() or not value.isdecimal()
           for value in (end, seconds) if value is not None):
        raise V2Error("invalid_mute", "Mute values are decimal strings in seconds.")
    return {**({"mute_end_timestamp": end} if end is not None else {}),
            **({"mute_seconds": seconds} if seconds is not None else {})}


def role_fields(fields):
    patch_fields(fields, {"name", "color", "hoist"})
    if "name" in fields and (not isinstance(fields["name"], str) or not fields["name"]):
        raise V2Error("invalid_role", "A nonempty role name is required.")
    if "color" in fields and (type(fields["color"]) is not int or not 0 <= fields["color"] <= 2**32 - 1):
        raise V2Error("invalid_role", "Role color must be uint32.")
    if "hoist" in fields and (type(fields["hoist"]) is not int or fields["hoist"] not in (0, 1)):
        raise V2Error("invalid_role", "Role hoist must be 0 or 1.")
    return fields


def permission_fields(add, remove):
    if add is None and remove is None:
        raise V2Error("invalid_permission", "Supply at least one permission mask.")
    result = {}
    for name, value in (("add", add), ("remove", remove)):
        if value is not None:
            if type(value) is int and value >= 0:
                value = str(value)
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                raise V2Error("invalid_permission", "Permission masks are decimal strings.")
            result[name] = value
    return result


def mute_batch_response(data, requested):
    if not isinstance(data, dict) or not isinstance(data.get("user_ids"), list):
        raise V2Error("invalid_management_response", "QQ did not provide batch mute outcomes.", phase="result_unknown", status=502)
    completed = data["user_ids"]
    if len(set(completed)) != len(completed) or any(type(value) is not str or value not in requested for value in completed):
        raise V2Error("invalid_management_response", "QQ batch mute included unrelated members.", phase="result_unknown", status=502)
    if len(completed) != len(requested):
        raise V2Error("partial_failure", "Only some guild members were changed; do not replay the batch.",
                      phase="partial", status=502, details={"succeeded": completed,
                                                           "failed": [value for value in requested if value not in completed]})


class NativeGuildAdminMixin:
    async def create_channel(self, guild_id: str, name: str, type: int, sub_type: int,
                             *, operation_id=None, **fields) -> dict:
        """Create one channel with every documented optional field, including zero."""
        if (not isinstance(name, str) or not name or type is None or not isinstance(type, int) or isinstance(type, bool) or type < 0
                or not isinstance(sub_type, int) or isinstance(sub_type, bool) or sub_type < 0):
            raise V2Error("invalid_channel", "Channel name, type and subtype are required.")
        if fields.keys() - {"position", "parent_id", "private_type", "private_user_ids", "speak_permission", "application_id"}:
            raise V2Error("invalid_channel", "Unknown channel creation field.")
        return await self._native_write("POST", f"/guilds/{segment(guild_id)}/channels",
            {"name": name, "type": type, "sub_type": sub_type, **fields}, kind="create_channel", operation_id=operation_id,
            scene="channel", target=guild_id)

    async def update_channel(self, channel_id: str, *, operation_id=None, **fields) -> dict:
        """Update only the official channel fields without truthy filtering."""
        body = patch_fields(fields, {"name", "position", "parent_id", "private_type", "speak_permission"})
        return await self._native_write("PATCH", f"/channels/{segment(channel_id)}", body,
            kind="update_channel", operation_id=operation_id, scene="channel", target=channel_id)

    async def delete_channel(self, channel_id: str, *, operation_id=None) -> dict | None:
        """Delete one explicit channel via the write ledger."""
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}", None,
            kind="delete_channel", operation_id=operation_id, scene="channel", target=channel_id)

    async def get_delete_member(self, guild_id: str, user_id: str, add_blacklist: bool = False,
                                delete_history_msg_days: int = 0, *, operation_id=None) -> dict | None:
        """Remove a guild member; the SDK's historical get name is a real DELETE."""
        if type(add_blacklist) is not bool or type(delete_history_msg_days) is not int or delete_history_msg_days not in (0, -1, 3, 7, 15, 30):
            raise V2Error("invalid_member_removal", "Supply a boolean and a documented history day range.")
        return await self._native_write("DELETE", f"/guilds/{segment(guild_id)}/members/{segment(user_id)}",
            {"add_blacklist": add_blacklist, "delete_history_msg_days": delete_history_msg_days},
            kind="delete_guild_member", operation_id=operation_id, scene="guild", target=guild_id)

    async def delete_guild_member(self, guild_id: str, user_id: str, add_blacklist=False,
                                  delete_history_msg_days=0, *, operation_id=None):
        """Alias the historic SDK removal name without a second implementation."""
        return await self.get_delete_member(guild_id, user_id, add_blacklist, delete_history_msg_days,
                                            operation_id=operation_id)

    async def mute_all(self, guild_id: str, mute_end_timestamp: str | None = None,
                       mute_seconds: str | None = None, *, operation_id=None) -> dict | None:
        """Mute a guild with explicit absolute or relative seconds."""
        return await self._native_write("PATCH", f"/guilds/{segment(guild_id)}/mute",
            mute_fields(mute_end_timestamp, mute_seconds), kind="mute_all", operation_id=operation_id,
            scene="guild", target=guild_id)

    async def cancel_mute_all(self, guild_id: str, *, operation_id=None) -> dict | None:
        """Cancel guild-wide mute without touching another guild."""
        return await self._native_write("PATCH", f"/guilds/{segment(guild_id)}/mute",
            {"mute_end_timestamp": "0", "mute_seconds": "0"}, kind="cancel_mute_all",
            operation_id=operation_id, scene="guild", target=guild_id)

    async def mute_member(self, guild_id: str, user_id: str, mute_end_timestamp: str | None = None,
                          mute_seconds: str | None = None, *, operation_id=None) -> dict | None:
        """Mute one guild member with a documented time value."""
        return await self._native_write("PATCH", f"/guilds/{segment(guild_id)}/members/{segment(user_id)}/mute",
            mute_fields(mute_end_timestamp, mute_seconds), kind="mute_guild_member",
            operation_id=operation_id, scene="guild", target=guild_id)

    async def mute_multi_member(self, guild_id: str, user_ids: list[str], mute_end_timestamp: str | None = None,
                                mute_seconds: str | None = None, *, operation_id=None) -> dict | None:
        """Mute an explicit bounded guild member list in one request."""
        body = {**mute_fields(mute_end_timestamp, mute_seconds), "user_ids": ids(user_ids, 5000)}
        return await self._native_write("PATCH", f"/guilds/{segment(guild_id)}/mute", body,
            kind="mute_multi_member", operation_id=operation_id, scene="guild", target=guild_id,
            response_check=lambda data: mute_batch_response(data, body["user_ids"]))

    async def cancel_mute_multi_member(self, guild_id: str, user_ids: list[str], *, operation_id=None) -> dict | None:
        """Unmute explicit guild members without silently splitting the batch."""
        body = {"user_ids": ids(user_ids, 5000), "mute_end_timestamp": "0", "mute_seconds": "0"}
        return await self._native_write("PATCH", f"/guilds/{segment(guild_id)}/mute", body,
            kind="cancel_mute_multi_member", operation_id=operation_id, scene="guild", target=guild_id,
            response_check=lambda data: mute_batch_response(data, body["user_ids"]))

    async def create_guild_role(self, guild_id: str, *, operation_id=None, **fields) -> dict:
        """Create one guild role using official role fields."""
        return await self._native_write("POST", f"/guilds/{segment(guild_id)}/roles", role_fields(fields),
            kind="create_guild_role", operation_id=operation_id, scene="guild", target=guild_id)

    async def update_guild_role(self, guild_id: str, role_id: str, *, operation_id=None, **fields) -> dict:
        """Modify one guild role with a complete explicit patch."""
        return await self._native_write("PATCH", f"/guilds/{segment(guild_id)}/roles/{segment(role_id)}", role_fields(fields),
            kind="update_guild_role", operation_id=operation_id, scene="guild", target=guild_id)

    async def delete_guild_role(self, guild_id: str, role_id: str, *, operation_id=None) -> dict | None:
        """Delete one explicit guild role."""
        return await self._native_write("DELETE", f"/guilds/{segment(guild_id)}/roles/{segment(role_id)}", None,
            kind="delete_guild_role", operation_id=operation_id, scene="guild", target=guild_id)

    async def create_guild_role_member(self, guild_id: str, role_id: str, user_id: str,
                                       channel_id: str | None = None, *, operation_id=None) -> dict | None:
        """Bind a guild role, including an optional channel context."""
        body = {"channel": {"id": text_id(channel_id)} if channel_id is not None else None}
        return await self._native_write("PUT", f"/guilds/{segment(guild_id)}/members/{segment(user_id)}/roles/{segment(role_id)}",
            body, kind="create_guild_role_member", operation_id=operation_id, scene="guild", target=guild_id)

    async def delete_guild_role_member(self, guild_id: str, role_id: str, user_id: str,
                                       channel_id: str | None = None, *, operation_id=None) -> dict | None:
        """Unbind a guild role without inferring a channel."""
        body = {"channel": {"id": text_id(channel_id)} if channel_id is not None else None}
        return await self._native_write("DELETE", f"/guilds/{segment(guild_id)}/members/{segment(user_id)}/roles/{segment(role_id)}",
            body, kind="delete_guild_role_member", operation_id=operation_id, scene="guild", target=guild_id)

    async def update_channel_user_permissions(self, channel_id: str, user_id: str, add=None, remove=None,
                                              *, operation_id=None) -> dict | None:
        """Update an explicit user's channel permission bitmap."""
        return await self._native_write("PUT", f"/channels/{segment(channel_id)}/members/{segment(user_id)}/permissions",
            permission_fields(add, remove), kind="update_channel_user_permissions", operation_id=operation_id,
            scene="channel", target=channel_id)

    async def update_channel_role_permissions(self, channel_id: str, role_id: str, add=None, remove=None,
                                              *, operation_id=None) -> dict | None:
        """Update an explicit role's channel permission bitmap."""
        return await self._native_write("PUT", f"/channels/{segment(channel_id)}/roles/{segment(role_id)}/permissions",
            permission_fields(add, remove), kind="update_channel_role_permissions", operation_id=operation_id,
            scene="channel", target=channel_id)

    async def post_permission_demand(self, guild_id: str, channel_id: str, api_identify: dict, desc: str,
                                     *, operation_id=None) -> dict:
        """Request the exact guild API authorization description and channel."""
        if not isinstance(api_identify, dict) or not api_identify or not isinstance(desc, str) or not desc:
            raise V2Error("invalid_permission", "API identity and nonempty purpose are required.")
        return await self._native_write("POST", f"/guilds/{segment(guild_id)}/api_permission/demand",
            {"channel_id": text_id(channel_id), "api_identify": api_identify, "desc": desc},
            kind="post_permission_demand", operation_id=operation_id, scene="guild", target=guild_id)

    async def create_announce(self, guild_id: str, channel_id: str, message_id: str, *, operation_id=None) -> dict:
        """Set a message announcement in the specified guild."""
        return await self._native_write("POST", f"/guilds/{segment(guild_id)}/announces",
            {"channel_id": text_id(channel_id), "message_id": text_id(message_id)}, kind="create_announce",
            operation_id=operation_id, scene="guild", target=guild_id)

    async def create_recommend_announce(self, guild_id: str, announces_type: int,
                                        recommend_channels: list[dict], *, operation_id=None) -> dict:
        """Replace only the explicitly supplied recommended channel list."""
        if type(announces_type) is not int or announces_type not in (0, 1) or not isinstance(recommend_channels, list) or len(recommend_channels) > 3:
            raise V2Error("invalid_announce", "Use a documented announcement type and at most three channels.")
        return await self._native_write("POST", f"/guilds/{segment(guild_id)}/announces",
            {"announces_type": announces_type, "recommend_channels": recommend_channels},
            kind="create_recommend_announce", operation_id=operation_id, scene="guild", target=guild_id)

    async def delete_announce(self, guild_id: str, message_id: str = "all", *, operation_id=None) -> dict | None:
        """Delete an exact announcement ID or the official all marker."""
        return await self._native_write("DELETE", f"/guilds/{segment(guild_id)}/announces/{segment(message_id)}", None,
            kind="delete_announce", operation_id=operation_id, scene="guild", target=guild_id)

    async def put_pin(self, channel_id: str, message_id: str, *, operation_id=None) -> dict | None:
        """Pin a channel message."""
        return await self._native_write("PUT", f"/channels/{segment(channel_id)}/pins/{segment(message_id)}", None,
            kind="put_pin", operation_id=operation_id, scene="channel", target=channel_id)

    async def delete_pin(self, channel_id: str, message_id: str, *, operation_id=None) -> dict | None:
        """Unpin a channel message."""
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}/pins/{segment(message_id)}", None,
            kind="delete_pin", operation_id=operation_id, scene="channel", target=channel_id)

    async def put_reaction(self, channel_id: str, message_id: str, emoji_type: int,
                           emoji_id: str, *, operation_id=None) -> dict | None:
        """Add an official channel reaction."""
        if type(emoji_type) is not int or emoji_type not in (1, 2):
            raise V2Error("invalid_emoji", "Emoji type must be 1 or 2.")
        return await self._native_write("PUT", f"/channels/{segment(channel_id)}/messages/{segment(message_id)}/reactions/{emoji_type}/{segment(emoji_id)}",
            None, kind="put_reaction", operation_id=operation_id, scene="channel", target=channel_id)

    async def delete_reaction(self, channel_id: str, message_id: str, emoji_type: int,
                              emoji_id: str, *, operation_id=None) -> dict | None:
        """Remove an official channel reaction."""
        if type(emoji_type) is not int or emoji_type not in (1, 2):
            raise V2Error("invalid_emoji", "Emoji type must be 1 or 2.")
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}/messages/{segment(message_id)}/reactions/{emoji_type}/{segment(emoji_id)}",
            None, kind="delete_reaction", operation_id=operation_id, scene="channel", target=channel_id)

    async def create_schedule(self, channel_id: str, name: str, start_timestamp: str, end_timestamp: str,
                              jump_channel_id: str, remind_type: int, *, operation_id=None) -> dict:
        """Create a schedule with current official remind_type field."""
        return await self._native_write("POST", f"/channels/{segment(channel_id)}/schedules",
            {"schedule": self._schedule(name, start_timestamp, end_timestamp, jump_channel_id, remind_type)},
            kind="create_schedule", operation_id=operation_id, scene="channel", target=channel_id)

    async def update_schedule(self, channel_id: str, schedule_id: str, name: str, start_timestamp: str,
                              end_timestamp: str, jump_channel_id: str, remind_type: int,
                              *, operation_id=None) -> dict:
        """Update a schedule without using the old SDK reminder_id alias."""
        return await self._native_write("PATCH", f"/channels/{segment(channel_id)}/schedules/{segment(schedule_id)}",
            {"schedule": self._schedule(name, start_timestamp, end_timestamp, jump_channel_id, remind_type)},
            kind="update_schedule", operation_id=operation_id, scene="channel", target=channel_id)

    @staticmethod
    def _schedule(name, start, end, jump, remind):
        if not isinstance(name, str) or not name or type(remind) is not int or not 0 <= remind <= 7:
            raise V2Error("invalid_schedule", "Provide a name and reminder kind 0..7.")
        for value in (start, end):
            if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
                raise V2Error("invalid_schedule", "Use millisecond timestamps as decimal strings.")
        return {"name": name, "start_timestamp": start, "end_timestamp": end,
                "jump_channel_id": text_id(jump), "remind_type": str(remind)}

    async def delete_schedule(self, channel_id: str, schedule_id: str, *, operation_id=None) -> dict | None:
        """Delete one schedule in its channel."""
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}/schedules/{segment(schedule_id)}",
            None, kind="delete_schedule", operation_id=operation_id, scene="channel", target=channel_id)

    async def update_audio(self, channel_id: str, audio_control: dict, *, operation_id=None) -> dict:
        """Send one caller-supplied audio control payload."""
        if not isinstance(audio_control, dict) or audio_control.keys() - {"audio_url", "text", "status"} or not audio_control:
            raise V2Error("invalid_audio", "Use official audio_url, text and status fields.")
        return await self._native_write("POST", f"/channels/{segment(channel_id)}/audio", audio_control,
            kind="update_audio", operation_id=operation_id, scene="channel", target=channel_id)

    async def on_microphone(self, channel_id: str, *, operation_id=None) -> dict:
        """Put the bot on its audio channel microphone."""
        return await self._native_write("PUT", f"/channels/{segment(channel_id)}/mic", {},
            kind="on_microphone", operation_id=operation_id, scene="channel", target=channel_id)

    async def off_microphone(self, channel_id: str, *, operation_id=None) -> dict:
        """Remove the bot from its audio channel microphone."""
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}/mic", {},
            kind="off_microphone", operation_id=operation_id, scene="channel", target=channel_id)

    async def post_thread(self, channel_id: str, title: str, content: str, format: int,
                          *, operation_id=None) -> dict:
        """Publish one thread preserving the official format value and task response."""
        if not isinstance(title, str) or not title or not isinstance(content, str) or type(format) is not int or format not in (1, 2, 3, 4):
            raise V2Error("invalid_thread", "Thread title, content and format 1..4 are required.")
        return await self._native_write("PUT", f"/channels/{segment(channel_id)}/threads",
            {"title": title, "content": content, "format": format}, kind="post_thread", operation_id=operation_id,
            scene="channel", target=channel_id)

    async def delete_thread(self, channel_id: str, thread_id: str, *, operation_id=None) -> dict | None:
        """Delete a thread explicitly in its channel."""
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}/threads/{segment(thread_id)}",
            None, kind="delete_thread", operation_id=operation_id, scene="channel", target=channel_id)
