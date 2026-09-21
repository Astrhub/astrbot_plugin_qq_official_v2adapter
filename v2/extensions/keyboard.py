"""Short-lived, actor-bound keyboard tickets; only approved host handlers may run."""
import copy
import hashlib
import json
import secrets
from dataclasses import dataclass

from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from ..commands import collect_catalog
from ..errors import V2Error, unsupported
from ..messaging.store import robot_key
from ..models import SessionRoute, text_id


class TicketStore:
    def __init__(self, adapter, *, catalog_provider=None):
        self.adapter, self.store = adapter, adapter.owner.messages
        self.catalog_provider = catalog_provider
        self.store.db.execute("CREATE TABLE IF NOT EXISTS keyboard_tickets(token TEXT PRIMARY KEY,robot TEXT,expires REAL,used INTEGER,body TEXT)")
        self.store.db.commit()

    def config(self, route):
        message_type = MessageType.GROUP_MESSAGE if route.scene == "group" else MessageType.FRIEND_MESSAGE
        return self.adapter.owner.context.get_config(str(MessageSession(self.adapter.identity.platform_id, message_type, route.encode())))

    def catalog(self, route):
        return self.catalog_provider(route) if self.catalog_provider else collect_catalog(self.config(route), route.scene)

    def settings(self):
        self.adapter.check_generation()
        value = self.adapter.owner.store.get(self.adapter.identity.settings_key)
        if not value["applied"].get("extensions", {}).get("keyboard_enabled", False):
            raise V2Error("keyboard_disabled", "Keyboard cards require the explicit advanced switch.", status=403)
        return value

    def issue(self, route, actor, node, command, intent, version):
        value = self.settings()
        if route.robot != self.adapter.identity.robot or route.scene not in {"group", "c2c"}:
            raise unsupported("Owned callback cards have a verified contract only for group/C2C.")
        text_id(actor)
        if not isinstance(command, str) or not 1 <= len(command) <= 100 or intent not in {"navigate", "confirm", "execute"}:
            raise V2Error("invalid_keyboard_command", "Keyboard commands must be complete bounded invocations.")
        if intent != "navigate" and (not value["applied"]["extensions"].get("keyboard_execute") or node["parameters"] or node["group"] or command != node["command"]):
            raise unsupported("Execution is opt-in and restricted to complete no-argument leaf commands.")
        if intent == "navigate" and (not node["menu_entry"] or not command.startswith(node["command"] + " kb ")):
            raise V2Error("invalid_keyboard_command", "Navigation only targets the real menu handler.")
        if not node.get("binding"):
            raise V2Error("command_unavailable", "The command source is unavailable.", status=409)
        expires = self.store.now() + value["applied"]["extensions"].get("ticket_ttl", 120)
        body = {"platform": self.adapter.identity.platform_id, "generation": self.adapter.identity.generation,
                "route": route.encode(), "actor": actor, "handler": node["id"], "binding": node["binding"],
                "command": command, "node_command": node["command"], "intent": intent,
                "revision": value["applied_revision"], "catalog_version": version}
        token = "qv2." + secrets.token_urlsafe(24)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM keyboard_tickets WHERE expires<=?", (self.store.now(),))
            if self.store.db.execute("SELECT count(*) FROM keyboard_tickets").fetchone()[0] >= 4096:
                raise V2Error("ticket_capacity", "Keyboard tickets reached their bounded capacity.", status=429)
            self.store.db.execute("INSERT INTO keyboard_tickets VALUES(?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), robot_key(route.robot), expires, 0, json.dumps(body)))
        return token

    def check_contract(self, body):
        value = self.settings()
        identity = self.adapter.identity
        if body["platform"] != identity.platform_id or body["generation"] != identity.generation or body["revision"] != value["applied_revision"]:
            raise V2Error("ticket_stale", "Keyboard generation or applied settings changed.", status=409)
        route = SessionRoute.decode(body["route"])
        if route.robot != identity.robot:
            raise V2Error("identity_mismatch", "Keyboard belongs to another robot.", status=403)
        catalog = self.catalog(route)
        nodes = [n for n in catalog["nodes"] if n["id"] == body["handler"]]
        from ..help import permitted
        admin = body["actor"] in self.config(route).get("admins_id", [])
        if (catalog["version"] != body["catalog_version"] or len(nodes) != 1 or not permitted(nodes[0], route.scene, admin)
                or nodes[0]["binding"] != body["binding"] or nodes[0]["command"] != body["node_command"]):
            raise V2Error("ticket_command_changed", "The command source, scope or permissions changed.", status=409)
        node = nodes[0]
        if body["intent"] != "navigate" and (not value["applied"]["extensions"].get("keyboard_execute") or node["parameters"] or node["group"] or body["command"] != node["command"]):
            raise V2Error("ticket_command_changed", "Execution is disabled or now requires explicit parameters.", status=409)
        return route

    def redeem(self, token, event, *, consume=True):
        if not isinstance(token, str) or not token.startswith("qv2.") or len(token) > 80:
            raise V2Error("unowned_callback", "This callback is not an owned command ticket.", status=404)
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self.store.transaction():
            row = self.store.db.execute("SELECT * FROM keyboard_tickets WHERE token=? AND robot=?", (hashed, robot_key(self.adapter.identity.robot))).fetchone()
            if not row or row["used"] or row["expires"] <= self.store.now():
                raise V2Error("ticket_unavailable", "Keyboard ticket expired, changed or was already consumed.", status=409)
            body = json.loads(row["body"])
            route = self.check_contract(body)
            if (route.scene, route.target, body["actor"]) != (event.scene, event.target, event.actor):
                raise V2Error("ticket_scope_mismatch", "Keyboard click does not match its actor and target.", status=403)
            if consume:
                self.store.db.execute("UPDATE keyboard_tickets SET used=1 WHERE token=?", (hashed,))
            return body

    def revoke(self, tokens):
        if self.store.closed:
            return
        with self.store.transaction():
            self.store.db.executemany("DELETE FROM keyboard_tickets WHERE token=? AND used=0", ((hashlib.sha256(token.encode()).hexdigest(),) for token in tokens))


@dataclass
class OwnedKeyboard:
    service: TicketStore
    route: SessionRoute
    revision: int
    generation: str
    body: dict
    tokens: list

    def validate(self, route):
        settings = self.service.settings()
        if route.robot != self.service.adapter.identity.robot or route != self.route or self.generation != self.service.adapter.identity.generation or self.revision != settings["applied_revision"]:
            raise V2Error("keyboard_scope_mismatch", "Keyboard cannot cross routes, generations or applied versions.", status=409)
        content = self.body.get("content") if isinstance(self.body, dict) else None
        rows = content.get("rows") if isinstance(content, dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
            raise V2Error("keyboard_limit", "Use 1..5 keyboard rows.")
        ids = set()
        for row in rows:
            buttons = row.get("buttons") if isinstance(row, dict) else None
            if not isinstance(buttons, list) or not 1 <= len(buttons) <= 5:
                raise V2Error("keyboard_limit", "Use 1..5 buttons in every row.")
            for button in buttons:
                if not isinstance(button, dict) or not isinstance(button.get("render_data"), dict) or not isinstance(button.get("action"), dict):
                    raise V2Error("invalid_keyboard", "Keyboard buttons require render and action objects.")
                render, action = button["render_data"], button["action"]
                if not isinstance(button.get("id"), str) or button["id"] in ids or not isinstance(render.get("label"), str) or not 1 <= len(render["label"]) <= 10 or type(render.get("style")) is not int or render["style"] not in {0, 1, 3, 4}:
                    raise V2Error("invalid_keyboard", "Keyboard IDs, labels or official styles are invalid.")
                ids.add(button["id"])
                permission = action.get("permission")
                if type(action.get("type")) is not int or action["type"] not in {1, 2} or action.get("enter", False) is not False or not isinstance(permission, dict) or type(permission.get("type")) is not int or permission["type"] != 0:
                    raise V2Error("invalid_keyboard", "Owned keyboards require explicit actors and cannot auto-enter commands.")
                if action["type"] == 1:
                    token = action.get("data")
                    if not isinstance(token, str) or token not in self.tokens:
                        raise V2Error("invalid_keyboard", "Callback data must be owned by this card.")
                    row = self.service.store.db.execute("SELECT * FROM keyboard_tickets WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
                    if not row or row["used"] or row["expires"] <= self.service.store.now():
                        raise V2Error("ticket_unavailable", "A keyboard ticket is no longer publishable.", status=409)
                    body = json.loads(row["body"])
                    if self.service.check_contract(body) != route or action["permission"].get("specify_user_ids") != [body["actor"]]:
                        raise V2Error("keyboard_scope_mismatch", "Keyboard permission does not match its ticket.", status=403)
        return copy.deepcopy(self.body)

    def revoke(self):
        self.service.revoke(self.tokens)


def make_keyboard(service, event, catalog, page, *, confirmation=None):
    settings = service.settings()
    route, actor = event.route, event.get_sender_id()
    nodes = {node["id"]: node for node in catalog["nodes"]}
    menu = next(node for node in catalog["nodes"] if node["menu_entry"] and node["enabled"])
    buttons, tokens = [], []
    def button(label, command, node, intent):
        if not command or len(command) > 100:
            return
        action = {"permission": {"type": 0, "specify_user_ids": [actor]}, "unsupport_tips": "请使用文本帮助"}
        if intent == "prefill":
            action.update({"type": 2, "data": command, "enter": False})
        else:
            token = service.issue(route, actor, node, command, intent, catalog["version"])
            tokens.append(token)
            action.update({"type": 1, "data": token})
        buttons.append({"id": str(len(buttons)), "render_data": {"label": label[:10], "visited_label": label[:10], "style": 1 if page["layout"]["style"] == "heading" else 0}, "action": action})
    try:
        if confirmation:
            node = nodes[confirmation["handler"]]
            button("确认执行", node["command"], node, "execute")
            button("返回首页", menu["command"] + " kb home 0", menu, "navigate")
        else:
            for item in page["items"]:
                command = item["command"]
                if not command:
                    continue
                node = nodes.get(item["id"])
                if node and command == node["command"]:
                    button("预填指令", command, node, "prefill")
                    if settings["applied"]["extensions"].get("keyboard_execute") and not node["parameters"] and not node["group"]:
                        button("请求执行", command, node, "confirm")
                else:
                    prefix = menu["command"] + (" md " if page["markdown"] else " ")
                    if not command.startswith(prefix):
                        raise V2Error("invalid_keyboard_command", "Navigation must come from the real menu.")
                    button(item["label"], menu["command"] + " kb " + command[len(prefix):], menu, "navigate")
            for label, command in page.get("navigation", []):
                prefix = menu["command"] + (" md " if page["markdown"] else " ")
                button(label, menu["command"] + " kb " + command[len(prefix):], menu, "navigate")
        columns = page["layout"]["columns"]
        if len(buttons) > columns * 5 or not buttons:
            raise V2Error("keyboard_limit", "Navigation and confirmation must fit the real button budget.")
        body = {"content": {"rows": [{"buttons": buttons[i:i + columns]} for i in range(0, len(buttons), columns)]}}
        return OwnedKeyboard(service, route, settings["applied_revision"], service.adapter.identity.generation, body, tokens)
    except BaseException:
        service.revoke(tokens)
        raise
