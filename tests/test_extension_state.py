import pytest

from v2.errors import V2Error
from v2.extensions.state import ExtensionStore, digest
from v2.messaging.store import MessageStore
from v2.models import RobotKey


def test_extension_recovery_fences_and_robot_isolation(tmp_path):
    path = tmp_path / "state"
    robot = RobotKey("a", "production")
    store = MessageStore(path, clock=lambda: 100)
    ext = ExtensionStore(store, capacity=2)
    assert ext.begin(robot, "one", "upload", digest([1])) == (True, None)
    ext.attempt(robot, "one")
    assert ext.begin(robot, "two", "delete", digest([2])) == (True, None)
    store.close()
    store = MessageStore(path, clock=lambda: 50)
    try:
        ext = ExtensionStore(store, capacity=2)
        assert ext.operation(robot, "one")["state"] == "unknown"
        assert ext.operation(robot, "two")["state"] == "not_sent"
        with pytest.raises(V2Error):
            ext.begin(robot, "one", "upload", digest([1]))
        with pytest.raises(V2Error) as error:
            ext.begin(robot, "one", "upload", digest([2]))
        assert error.value.code == "operation_conflict"
        assert ext.begin(RobotKey("b", "production"), "one", "upload", digest([1]))[0]
        assert ext.operation(robot, "one")["updated"] == 100
    finally:
        store.close()


def test_extension_history_compaction_retains_replay_fence(tmp_path):
    store = MessageStore(tmp_path / "state", clock=lambda: 100)
    robot = RobotKey("a", "production")
    try:
        ext = ExtensionStore(store, capacity=1)
        for op in ("a", "b"):
            ext.begin(robot, op, "delete", digest([op]))
            ext.attempt(robot, op)
            ext.finish(robot, op, "succeeded", result={"ok": True})
        assert ext.operation(robot, "a")["state"] == "history_evicted"
        with pytest.raises(V2Error):
            ext.begin(robot, "a", "delete", digest(["a"]))
        assert not ext.begin(robot, "b", "delete", digest(["b"]))[0]
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        store.close()
