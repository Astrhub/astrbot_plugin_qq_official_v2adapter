"""M4/M5 methods are importable with stable signatures and non-OneBot aliases."""

import inspect

from v2.client import ClientState, V2Client
from v2.models import InstanceKey, RobotKey
from v2.sdk import NativeUploadHandle


M4 = (
    "get_gateway", "get_gateway_bot", "get_ws_url", "me", "me_guilds", "get_message",
    "get_group_info", "get_group_bot_state", "get_group_member_info", "get_group_member_list",
    "iter_group_members", "get_group_restrict_chat_setting", "get_group_member_blacklist",
    "get_group_join_requests", "get_join_approval_strategies", "get_menu", "get_panels", "get_panel",
    "get_guild", "get_channels", "get_channel", "get_guild_members", "get_guild_member",
    "get_guild_role_members", "get_voice_members", "get_channel_online_nums",
    "get_guild_message_setting", "get_guild_roles", "get_channel_user_permissions",
    "get_channel_role_permissions", "get_permissions", "get_pins", "get_reaction_users",
    "get_schedules", "get_schedule", "get_threads", "get_thread_detail",
)
M5 = (
    "post_group_message", "post_c2c_message", "post_c2c_stream_message", "post_message",
    "post_keyboard_message", "post_dms", "create_dms", "recall_group_message",
    "recall_c2c_message", "recall_message", "recall_dms", "patch_guild_message",
    "post_group_file", "post_c2c_file", "post_group_upload_prepare",
    "post_c2c_upload_prepare", "post_group_upload_part_finish",
    "post_c2c_upload_part_finish", "begin_upload", "on_interaction_result",
)


def test_m4m5_public_methods_import_and_positions():
    identity = InstanceKey("instance", RobotKey("app"))
    client = V2Client(identity, state=ClientState(identity))
    assert NativeUploadHandle.__name__ == "NativeUploadHandle"
    for name in (*M4, *M5):
        method = getattr(client.qq, name)
        assert callable(method), name
        assert inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method), name
    for name in ("get_guild", "get_message", "post_group_message", "post_c2c_message", "post_dms", "create_dms", "post_group_file"):
        assert callable(getattr(client.api, name))
    assert inspect.signature(client.qq.post_group_message).parameters["msg_seq"].default is None
    assert inspect.signature(client.qq.post_c2c_message).parameters["msg_seq"].default is None
    assert list(inspect.signature(client.qq.create_dms).parameters)[:2] == ["guild_id", "user_id"]
    assert inspect.signature(client.qq.get_guild_members).parameters["after"].default == "0"
    assert inspect.signature(client.qq.recall_message).parameters["hidetip"].default is False
    assert "begin_upload" in client.capabilities()["sdk"]["native_implemented"]
    assert "post_group_message" in client.capabilities()["sdk"]["native_implemented"]
    assert "create_guild_role" not in client.capabilities()["sdk"]["native_implemented"]
