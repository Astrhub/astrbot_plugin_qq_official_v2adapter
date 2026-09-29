"""Instance-scoped latest inbound chat sources for host session sends.

AstrBot ``send_by_session`` starts active. When QQ definitively rejects the active
message with the group proactive-permission code, one bounded active-to-passive
conversion may reuse the newest *real inbound chat* source observed by this
adapter generation. Bot send results, reference indexes and interaction-derived
events never enter this index, and candidates are never rebuilt from disk.
"""
from dataclasses import dataclass

from ..errors import V2Error
from .convert import ReplySource

# QQ business code: the group/C2C target rejected a proactive message.
ACTIVE_DENIED_CODE = 40034105


@dataclass(frozen=True)
class SessionSendPolicy:
    """One fixed candidate for a single logical session send."""

    source: object
    origin: str  # "event_session" or "session_index"

    def describe(self):
        return {"kind": self.origin, "message_id": self.source.message_id}


def _route_key(route):
    return route.scene, route.target, route.user


class SessionSourceIndex:
    """Latest inbound chat source per session key, bounded and generation-scoped."""

    def __init__(self, identity, *, capacity):
        if capacity < 1:
            raise ValueError("session source capacity must be positive")
        self.identity = identity
        self.capacity = capacity
        self._latest = {}

    def record(self, chat, now):
        """Retain one accepted real chat as the newest candidate for its session."""
        source = chat.source
        if source.generation != self.identity.generation:
            return
        key = _route_key(source.route)
        self._latest = {candidate_key: candidate for candidate_key, candidate in self._latest.items()
                        if candidate.expires > now and candidate_key != key}
        self._latest[key] = source
        while len(self._latest) > self.capacity:
            self._latest.pop(next(iter(self._latest)))

    def revoke(self):
        self._latest.clear()

    def policy_for_route(self, route):
        """Return a fixed policy for the newest unexpired candidate of this session."""
        source = self._latest.get(_route_key(route))
        if source is None:
            return None
        return self._validated(route, source, "session_index")

    def policy_for_bound_session(self, route, source):
        """Return a fixed policy for a bound event session's exact chat source."""
        if not isinstance(source, ReplySource):
            # Interaction-derived events keep their own exact passive replies, but they
            # are not inbound chat and never become a session fallback candidate.
            return None
        return self._validated(route, source, "event_session")

    def _validated(self, route, source, origin):
        # Only structural identity is checked here; window and blocked state are
        # revalidated inside the conversion transaction, and an unusable candidate
        # must never block the initial active attempt.
        if source.route.robot != self.identity.robot or source.generation != self.identity.generation:
            return None
        if _route_key(source.route) != _route_key(route):
            raise V2Error("identity_mismatch", "Session source belongs to another target.", status=409)
        return SessionSendPolicy(source, origin)
