"""Merge structured chat authors without turning history into reply sources."""

from .store import observation_time


def merge_chat(store, chat, payload, received):
    robot, scene, scope = chat.identity.robot, chat.route.scene, chat.route.target
    for row in chat.observations:
        if row.get("scope") != f"{scene}:{scope}":
            continue
        store.merge(robot, scene, scope, row["user_id"],
                    {"nickname": row.get("nickname"), "avatar_url": row.get("avatar_url")},
                    source=row.get("source", "current_chat"), as_of=row.get("as_of"), received=received, kind=row["id_kind"])
    if scene == "group":
        store.member_event(robot, scene, scope, chat.message.sender.user_id, "present", int(chat.source.sent_at),
                           received=received)
    data = payload.get("d")
    if not isinstance(data, dict):
        return
    kind = {"group": "member_openid", "c2c": "user_openid", "channel": "channel_user_id", "dm": "channel_user_id"}[scene]
    field = "id" if scene in {"channel", "dm"} else kind
    stack = list(data.get("msg_elements", [])) if isinstance(data.get("msg_elements", []), list) else []
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or node.get("group_openid", scope) != scope:
            continue
        author = node.get("author")
        if isinstance(author, dict) and isinstance(author.get(field), str) and isinstance(author.get("username"), str):
            store.merge(robot, scene, scope, author[field], {"nickname": author["username"]},
                        source="chat_history", as_of=observation_time(node.get("timestamp")),
                        received=received, kind=kind)
        children = node.get("msg_elements", [])
        if isinstance(children, list):
            stack.extend(children)
