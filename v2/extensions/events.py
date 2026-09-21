"""Typed non-chat sources keep transport, interaction and message identifiers distinct."""
import copy
from dataclasses import dataclass

from ..errors import V2Error
from ..messaging.convert import bounded_structure
from ..models import SessionRoute, text_id
from .management import timestamp


@dataclass(frozen=True)
class EventReplySource:
    route: SessionRoute
    generation: str
    event_id: str
    interaction_id: str
    sent_at: float
    received_at: float
    message_id: None = None
    ref_idx: None = None

    @property
    def expires(self):
        return min(self.sent_at, self.received_at) + 300


@dataclass(frozen=True)
class ExtensionEvent:
    name: str
    event_id: str | None
    interaction_id: str | None
    message_id: str | None
    received_at: float
    payload: dict
    interaction_type: int | None = None
    scene: str | None = None
    target: str | None = None
    actor: str | None = None
    sent_at: float | None = None

    @classmethod
    def parse(cls, identity, payload, received):
        bounded_structure(payload)
        if payload.get("op") != 0 or not isinstance(payload.get("d"), dict):
            raise V2Error("invalid_extension_event", "Extension events require an original dispatch envelope.")
        name, data = payload.get("t"), payload["d"]
        outer = text_id(payload["id"]) if payload.get("id") is not None else None
        if name == "GROUP_JOIN_REQUEST":
            text_id(data.get("join_request_id"))
            if data.get("apply_source") not in ("self_apply", "invited"):
                raise V2Error("invalid_extension_event", "The application source is unknown.")
            return cls(name, outer, None, None, received, copy.deepcopy(payload), scene="group",
                       target=text_id(data.get("group_openid")), actor=text_id(data.get("member_openid")), sent_at=timestamp(data.get("apply_at")))
        if name != "INTERACTION_CREATE" or type(data.get("type")) is not int:
            raise V2Error("unsupported_extension_event", "This extension event is retained, not projected as chat.")
        if data.get("application_id", identity.robot.appid) != identity.robot.appid:
            raise V2Error("identity_mismatch", "Interaction belongs to another robot.", status=403)
        interaction = text_id(data.get("id"))
        if interaction.startswith("INTERACTION_CREATE:"):
            raise V2Error("invalid_interaction_id", "ACK requires d.id without a dispatch-type prefix.")
        nested = data.get("data", {})
        if not isinstance(nested, dict) or not isinstance(nested.get("resolved", {}), dict):
            raise V2Error("invalid_extension_event", "Interaction data is malformed.")
        resolved = nested.get("resolved", {})
        if nested.get("type", data["type"]) != data["type"]:
            raise V2Error("invalid_extension_event", "Interaction type fields disagree.")
        if not isinstance(data.get("scene"), str):
            raise V2Error("invalid_extension_event", "Interaction scene must be a string.")
        scene = {"c2c": "c2c", "group": "group", "guild": "channel"}.get(data.get("scene"))
        target = {"c2c": data.get("user_openid"), "group": data.get("group_openid"), "channel": data.get("channel_id")}.get(scene)
        actor = {"c2c": data.get("user_openid"), "group": data.get("group_member_openid"), "channel": resolved.get("user_id")}.get(scene)
        sent = timestamp(data.get("timestamp"))
        if not 0 <= sent <= received + 60:
            raise V2Error("invalid_extension_event", "Interaction timestamp is outside the accepted clock bound.")
        return cls(name, outer, interaction, text_id(resolved["message_id"]) if resolved.get("message_id") else None,
                   received, copy.deepcopy(payload), data["type"], scene,
                   text_id(target) if target is not None else None, text_id(actor) if actor is not None else None, sent)

    def route(self, identity, *, isolated=False):
        if self.scene not in {"group", "c2c"} or not self.target or not self.actor or not self.event_id:
            raise V2Error("interaction_projection_unsupported", "Only complete group/C2C callback routes have a verified passive reply contract.", status=501)
        return SessionRoute(identity.robot, self.scene, self.target, self.actor if isolated and self.scene == "group" else None)

    def metadata(self):
        result = {"event_type": self.name, "event_id": self.event_id, "interaction_id": self.interaction_id, "message_id": self.message_id,
                  "op": self.payload["op"], "sequence": self.payload.get("s"), "received_at": self.received_at, "sent_at": self.sent_at,
                  "interaction_type": self.interaction_type, "scene": self.scene, "target": self.target, "actor": self.actor}
        if self.name == "GROUP_JOIN_REQUEST":
            result.update({"join_request_id": self.payload["d"]["join_request_id"], "apply_source": self.payload["d"]["apply_source"],
                           "auto_approved": self.payload["d"].get("auto_approved") is not None})
        return result
