"""Pin the public botpy 1.2.1 and V2 native method surface without importing botpy at runtime."""

import ast
import importlib.util
import inspect
from pathlib import Path

from v2.client import ClientState, NativeView, V2Client
from v2.models import InstanceKey, RobotKey
from v2.sdk import NativeUploadHandle

TARGET_PUBLIC = """
get_guild get_guild_roles create_guild_role update_guild_role delete_guild_role
create_guild_role_member delete_guild_role_member get_guild_member get_delete_member
get_guild_members get_guild_role_members get_voice_members get_channel get_channels
create_channel update_channel delete_channel get_channel_user_permissions
update_channel_user_permissions get_channel_role_permissions update_channel_role_permissions
get_message post_message recall_message post_keyboard_message on_interaction_result
patch_guild_message create_dms post_dms update_audio on_microphone off_microphone me
me_guilds get_ws_url mute_all cancel_mute_all mute_member mute_multi_member
cancel_mute_multi_member create_announce create_recommend_announce delete_announce
get_permissions post_permission_demand get_schedules get_schedule create_schedule
update_schedule delete_schedule put_reaction delete_reaction get_reaction_users
put_pin delete_pin get_pins get_threads get_thread_detail post_thread delete_thread
post_group_message post_c2c_message post_group_file post_c2c_file
""".split()

V2_EXTENSIONS = (
    "get_gateway", "get_gateway_bot", "get_group_info", "get_group_bot_state",
    "get_group_member_info", "get_group_member_list", "iter_group_members",
    "get_group_restrict_chat_setting", "get_group_member_blacklist",
    "get_group_join_requests", "get_join_approval_strategies", "get_menu",
    "get_panels", "get_panel", "get_channel_online_nums",
    "get_guild_message_setting", "post_c2c_stream_message",
    "recall_group_message", "recall_c2c_message", "recall_dms",
    "post_group_upload_prepare", "post_c2c_upload_prepare",
    "post_group_upload_part_finish", "post_c2c_upload_part_finish", "begin_upload",
)


def test_public_botpy_and_v2_native_method_surface():
    spec = importlib.util.find_spec("botpy.api")
    assert spec and spec.origin
    tree = ast.parse(Path(spec.origin).read_text())
    api = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BotAPI")
    methods = {node.name: node for node in api.body if isinstance(node, ast.AsyncFunctionDef)
               and not node.name.startswith("_")}
    assert len(TARGET_PUBLIC) == len(set(TARGET_PUBLIC)) == len(methods) == 64
    assert set(TARGET_PUBLIC) == set(methods)
    assert NativeUploadHandle.__name__ == "NativeUploadHandle"

    identity = InstanceKey("platform", RobotKey("app"))
    client = V2Client(identity, state=ClientState(identity))
    for name in TARGET_PUBLIC:
        method = getattr(NativeView, name)
        assert inspect.iscoroutinefunction(method), name
        old = [arg.arg for arg in methods[name].args.posonlyargs + methods[name].args.args][1:]
        current = [arg.name for arg in list(inspect.signature(method).parameters.values())[1:]
                   if arg.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        assert current == old, name
        assert getattr(client.qq, name) and getattr(client.api, name), name

    for name in V2_EXTENSIONS:
        method = getattr(client.qq, name)
        assert inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method), name

    assert inspect.signature(NativeView.post_group_message).parameters["msg_seq"].default is None
    assert inspect.signature(NativeView.post_c2c_message).parameters["msg_seq"].default is None
    assert inspect.signature(NativeView.patch_guild_message).parameters["keyboard"].default is None
    assert list(inspect.signature(client.qq.create_dms).parameters)[:2] == ["guild_id", "user_id"]
    assert inspect.signature(client.qq.get_guild_members).parameters["after"].default == "0"
    assert inspect.signature(client.qq.recall_message).parameters["hidetip"].default is False
    for cap in ("begin_upload", "post_group_message", "create_guild_role"):
        assert cap in client.capabilities()["sdk"]["native_implemented"]
