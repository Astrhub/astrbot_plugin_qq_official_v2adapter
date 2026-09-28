"""Auditable official HTTP targets; presence here never enables arbitrary writes."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    identifier: str
    method: str
    path: str
    stage: str
    effect: str
    pagination: str | None
    support: str
    permission: str = "qq_response_required"


# One method+path per line, independent of OneBot network action allowlists.
_TARGETS = """
A001 GET /gateway M4
A002 GET /gateway/bot M4
A003 GET /users/@me M4
A004 GET /users/@me/guilds M4
A005 POST /v2/generate_url_link M6
A006 POST /v2/groups/{group_openid}/messages M5
A007 DELETE /v2/groups/{group_openid}/messages/{message_id} M5
A008 POST /v2/users/{user_openid}/messages M5
A009 DELETE /v2/users/{user_openid}/messages/{message_id} M5
A010 POST /v2/users/{user_openid}/stream_messages M5
A011 GET /channels/{channel_id}/messages/{message_id} M4
A012 POST /channels/{channel_id}/messages M5
A013 PATCH /channels/{channel_id}/messages/{message_id} M5
A014 DELETE /channels/{channel_id}/messages/{message_id} M5
A015 POST /users/@me/dms M5
A016 POST /dms/{guild_id}/messages M5
A017 DELETE /dms/{guild_id}/messages/{message_id} M5
A018 POST /v2/groups/{group_openid}/files M5
A019 POST /v2/users/{user_openid}/files M5
A020 POST /v2/groups/{group_id}/upload_prepare M5
A021 POST /v2/groups/{group_id}/upload_part_finish M5
A022 POST /v2/users/{user_id}/upload_prepare M5
A023 POST /v2/users/{user_id}/upload_part_finish M5
A024 GET /v2/groups/{group_openid}/info M4
A025 GET /v2/groups/{group_openid}/bot_state M4
A026 GET /v2/groups/{group_openid}/members/{member_openid} M3
A027 GET /v2/groups/{group_openid}/members M3
A028 GET /v2/groups/{group_openid}/restrict_chat_setting M4
A029 POST /v2/groups/{group_openid}/restrict_chat_setting M6
A030 POST /v2/groups/{group_openid}/batch_remove_members M6
A031 GET /v2/groups/{group_openid}/member_blacklist M4
A032 POST /v2/groups/{group_openid}/member_blacklist M6
A033 GET /v2/groups/{group_openid}/join_request_list M4
A034 POST /v2/groups/{group_openid}/approval_join_request/{member_openid} M6
A035 GET /v2/groups/join_approval_strategy M4
A036 POST /v2/groups/join_approval_strategy M6
A037 PATCH /v2/groups/join_approval_strategy/{strategy_id} M6
A038 DELETE /v2/groups/join_approval_strategy/{strategy_id} M6
A039 POST /v2/groups/join_approval_strategy/{strategy_id}/execute M6
A040 POST /v2/groups/join_approval_strategy/{strategy_id}/whitelist_users M6
A041 GET /v2/menu M4
A042 PUT /v2/menu M6
A043 GET /v2/panels M4
A044 POST /v2/panels M6
A045 GET /v2/panels/{panel_id} M4
A046 PUT /v2/panels/{panel_id} M6
A047 DELETE /v2/panels/{panel_id} M6
A048 PUT /v2/panels/{panel_id}/target M6
A049 PUT /interactions/{interaction_id} M5
A050 GET /guilds/{guild_id} M4
A051 GET /guilds/{guild_id}/channels M4
A052 POST /guilds/{guild_id}/channels M6
A053 GET /channels/{channel_id} M4
A054 PATCH /channels/{channel_id} M6
A055 DELETE /channels/{channel_id} M6
A056 GET /guilds/{guild_id}/members M4
A057 GET /guilds/{guild_id}/members/{user_id} M4
A058 DELETE /guilds/{guild_id}/members/{user_id} M6
A059 GET /guilds/{guild_id}/roles/{role_id}/members M4
A060 GET /channels/{channel_id}/voice/members M4
A061 GET /channels/{channel_id}/online_nums M4
A062 PATCH /guilds/{guild_id}/mute M6
A063 PATCH /guilds/{guild_id}/members/{user_id}/mute M6
A064 GET /guilds/{guild_id}/message/setting M4
A065 GET /guilds/{guild_id}/roles M4
A066 POST /guilds/{guild_id}/roles M6
A067 PATCH /guilds/{guild_id}/roles/{role_id} M6
A068 DELETE /guilds/{guild_id}/roles/{role_id} M6
A069 PUT /guilds/{guild_id}/members/{user_id}/roles/{role_id} M6
A070 DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id} M6
A071 GET /channels/{channel_id}/members/{user_id}/permissions M4
A072 PUT /channels/{channel_id}/members/{user_id}/permissions M6
A073 GET /channels/{channel_id}/roles/{role_id}/permissions M4
A074 PUT /channels/{channel_id}/roles/{role_id}/permissions M6
A075 GET /guilds/{guild_id}/api_permission M4
A076 POST /guilds/{guild_id}/api_permission/demand M6
A077 POST /guilds/{guild_id}/announces M6
A078 DELETE /guilds/{guild_id}/announces/{message_id} M6
A079 POST /channels/{channel_id}/announces X
A080 DELETE /channels/{channel_id}/announces/{message_id} X
A081 GET /channels/{channel_id}/pins M4
A082 PUT /channels/{channel_id}/pins/{message_id} M6
A083 DELETE /channels/{channel_id}/pins/{message_id} M6
A084 PUT /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} M6
A085 DELETE /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} M6
A086 GET /channels/{channel_id}/messages/{message_id}/reactions/{type}/{id} M4
A087 GET /channels/{channel_id}/schedules M4
A088 GET /channels/{channel_id}/schedules/{schedule_id} M4
A089 POST /channels/{channel_id}/schedules M6
A090 PATCH /channels/{channel_id}/schedules/{schedule_id} M6
A091 DELETE /channels/{channel_id}/schedules/{schedule_id} M6
A092 POST /channels/{channel_id}/audio M6
A093 PUT /channels/{channel_id}/mic M6
A094 DELETE /channels/{channel_id}/mic M6
A095 GET /channels/{channel_id}/threads M4
A096 GET /channels/{channel_id}/threads/{thread_id} M4
A097 PUT /channels/{channel_id}/threads M6
A098 DELETE /channels/{channel_id}/threads/{thread_id} M6
"""

_LEGACY_CORE = frozenset("""A002 A003 A005 A006 A007 A008 A009 A010 A012 A014 A016 A017 A018 A019
A020 A021 A022 A023 A025 A028 A029 A030 A033 A034 A043 A044 A045 A046 A049 A050 A051 A052
A053 A054 A055 A056 A057 A058 A062 A063""".split())
_PAGES = {"A004": "limit", "A027": "cursor", "A031": "cursor", "A033": "cursor", "A035": "cursor",
          "A043": "cursor", "A056": "after", "A059": "start_index", "A086": "cookie"}
_INVITED = frozenset("A026 A027 A029 A030 A031 A032 A033 A034 A035 A036 A037 A038 A039 A040".split())


def _build():
    result = {}
    for identifier, method, path, stage in (line.split() for line in _TARGETS.strip().splitlines()):
        support = ("excluded" if stage == "X" else "native" if method == "GET" or stage == "M5"
                   else "internal" if identifier in _LEGACY_CORE else "pending")
        result[identifier] = Endpoint(identifier, method, path, stage,
                                      "read" if method == "GET" else "write", _PAGES.get(identifier), support,
                                      "invitation_only_unverified" if identifier in _INVITED else "qq_response_required")
    return result


HTTP_TARGETS = _build()
