"""Official single-request group member reads and bounded iteration."""

from urllib.parse import quote

from ...errors import V2Error
from ..identifiers import text_id


def segment(value):
    return quote(text_id(value), safe="")


class GroupReads:
    def __init__(self, identity, http, request_spec):
        self.identity, self.http, self.request_spec = identity, http, request_spec

    async def get_group_member_info(self, group_openid: str, member_openid: str) -> dict:
        """Fetch one current member from QQ without substituting a cached record."""
        group, user = segment(group_openid), segment(member_openid)
        response = await self.http.request(self.request_spec(self.identity.robot.environment, "GET",
            f"/v2/groups/{group}/members/{user}"))
        data = response.data
        if not isinstance(data, dict) or data.get("member_openid") != member_openid:
            raise V2Error("invalid_management_response", "QQ member response does not match the requested member.", status=502)
        return data

    async def get_group_member_list(self, group_openid: str, cursor: str = "") -> dict:
        """Return one official page, preserving its next_cursor and native fields."""
        group = segment(group_openid)
        if not isinstance(cursor, str) or len(cursor) > 4096:
            raise V2Error("invalid_cursor", "Expected a bounded cursor string.")
        response = await self.http.request(self.request_spec(self.identity.robot.environment, "GET",
            f"/v2/groups/{group}/members", params={"cursor": cursor}))
        data = response.data
        if (not isinstance(data, dict) or not isinstance(data.get("members"), list)
                or len(data["members"]) > 30 or not isinstance(data.get("next_cursor"), str)):
            raise V2Error("invalid_management_response", "QQ returned an incomplete member page.", status=502)
        return data

    async def iter_group_members(self, group_openid: str):
        """Iterate official pages without claiming an atomic roster snapshot."""
        cursor, visited = "", set()
        for _ in range(200):
            page = await self.get_group_member_list(group_openid, cursor)
            for member in page["members"]:
                yield member
            cursor = page["next_cursor"]
            if not cursor:
                return
            if cursor in visited:
                raise V2Error("pagination_incomplete", "QQ repeated a member cursor.", status=502)
            visited.add(cursor)
        raise V2Error("pagination_incomplete", "Member list exceeded the page limit.", status=502)
