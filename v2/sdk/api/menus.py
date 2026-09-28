"""Official menu and panel writes coordinated with managed panel synchronization."""

from copy import deepcopy
from uuid import uuid4

from ...errors import V2Error, not_ready
from ...extensions.state import digest
from ..identifiers import text_id
from .group_admin import ids
from .messages import segment


def returned_field(data, field, kind=str):
    if not isinstance(data, dict) or type(data.get(field)) is not kind or kind is str and not data[field]:
        raise V2Error("invalid_panel_response", "QQ did not confirm the requested panel or menu.", phase="result_unknown", status=502)


def panel_body(panel):
    if not isinstance(panel, dict) or not isinstance(panel.get("items", []), list) or len(panel.get("items", [])) > 20:
        raise V2Error("invalid_panel", "A panel needs at most 20 items.")
    return panel


class NativeMenuMixin:
    async def _panel_write(self, panel_id, method, path, body, *, kind, operation_id=None, response_check=None):
        self._check()
        state = self._client._state
        if state.panels is None or state.management is None:
            raise not_ready()
        state.management.check(write=True)
        body = deepcopy(body)
        op_id = text_id(self._operation_id(operation_id) or uuid4().hex)
        mutation = ({"op_id": op_id, "kind": kind, "binding": digest([method, path, None, body]),
                     "method": method, "path": path, "panel_id": panel_id,
                     "panel": body["panel"] if kind == "update_panel" else None} if panel_id is not None else None)
        async def write():
            return await self._native_write(method, path, body, kind=kind, operation_id=op_id,
                                            response_check=response_check)
        return await state.panels.manual_write(self._client, panel_id, write, mutation=mutation)

    async def put_menu(self, menu: dict, *, operation_id=None) -> dict:
        """Replace the full official menu under the robot's panel coordinator."""
        if not isinstance(menu, dict) or not isinstance(menu.get("items", []), list) or len(menu.get("items", [])) > 10:
            raise V2Error("invalid_menu", "Menu requires at most ten items.")
        for item in menu.get("items", []):
            if not isinstance(item, dict) or not isinstance(item.get("sub_menu_items", []), list) or len(item.get("sub_menu_items", [])) > 5:
                raise V2Error("invalid_menu", "A menu item has at most five subitems.")
        return await self._panel_write(None, "PUT", "/v2/menu", {"menu": menu},
            kind="put_menu", operation_id=operation_id, response_check=lambda data: returned_field(data, "version", int))

    async def create_panel(self, scope: str, panel: dict, *, target_type="all", user_openids=None,
                           group_openids=None, operation_id=None) -> dict:
        """Create an independent panel without taking ownership of existing panels."""
        if scope not in ("group", "c2c", "channel", "dm") or target_type not in ("all", "specific"):
            raise V2Error("invalid_panel", "Use a documented scope and target type.")
        body = {"scope": scope, "target_type": target_type, "panel": panel_body(panel)}
        if target_type == "specific":
            key = "group_openids" if scope == "group" else "user_openids" if scope == "c2c" else None
            if key is None or (user_openids is None) == (group_openids is None):
                raise V2Error("invalid_panel", "Only group or C2C may select matching targets.")
            body[key] = ids(group_openids if key == "group_openids" else user_openids)
        elif user_openids is not None or group_openids is not None:
            raise V2Error("invalid_panel", "An all-target panel cannot select identities.")
        return await self._panel_write(None, "POST", "/v2/panels", body, kind="create_panel",
            operation_id=operation_id, response_check=lambda data: returned_field(data, "panel_id"))

    async def update_panel(self, panel_id: str, panel: dict, *, operation_id=None) -> dict:
        """Pause managed synchronization before explicitly replacing a panel."""
        return await self._panel_write(text_id(panel_id), "PUT", f"/v2/panels/{segment(panel_id)}",
            {"panel": panel_body(panel)}, kind="update_panel", operation_id=operation_id,
            response_check=lambda data: returned_field(data, "version", int))

    async def delete_panel(self, panel_id: str, *, operation_id=None) -> dict | None:
        """Delete an explicit panel without searching or deleting other owners' objects."""
        return await self._panel_write(text_id(panel_id), "DELETE", f"/v2/panels/{segment(panel_id)}",
                                       None, kind="delete_panel", operation_id=operation_id)

    async def set_panel_target(self, panel_id: str, op: str, *, user_openids=None,
                               group_openids=None, operation_id=None) -> dict | None:
        """Change explicitly named panel associations in one bounded request."""
        if op not in ("add", "del") or (user_openids is None) == (group_openids is None):
            raise V2Error("invalid_panel", "Choose add or del and exactly one target kind.")
        body = {"op": op, "user_openids": ids(user_openids)} if user_openids is not None else {"op": op, "group_openids": ids(group_openids)}
        return await self._panel_write(text_id(panel_id), "PUT", f"/v2/panels/{segment(panel_id)}/target",
                                       body, kind="set_panel_target", operation_id=operation_id)
