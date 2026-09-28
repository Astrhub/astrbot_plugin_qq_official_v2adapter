"""Merge structured chat authors without turning history into reply sources."""



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
