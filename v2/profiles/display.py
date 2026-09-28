"""Enrich host message display from this robot's scoped persistent profiles only."""

import sqlite3

from astrbot.core.message.components import At, Reply

from ..errors import V2Error

KIND = {"group": "member_openid", "c2c": "user_openid", "channel": "channel_user_id", "dm": "channel_user_id"}


def enrich_chat(chat, store):
    """Fill missing names without a QQ lookup or a change to raw_message."""
    route = chat.route
    names = {}
    def name_for(user):
        if not isinstance(user, str) or not user or user in {"0", "all"}:
            return None
        if user not in names:
            try:
                profile = store.get_member(route.robot, route.scene, route.target, user, kind=KIND[route.scene])
                entry = profile["fields"].get("nickname")
                names[user] = entry["value"] if entry and isinstance(entry["value"], str) and entry["value"] else None
            except (V2Error, sqlite3.Error):
                names[user] = None
        return names[user]
    if not chat.message.sender.nickname:
        name = name_for(chat.message.sender.user_id)
        if name:
            chat.message.sender.nickname = name
    for part in chat.message.message:
        if isinstance(part, Reply) and not part.sender_nickname and isinstance(part.sender_id, str):
            name = name_for(part.sender_id)
            if name:
                part.sender_nickname = name
        if type(part) is At and not part.name and isinstance(part.qq, str):
            name = name_for(part.qq)
            if name:
                part.name = name
