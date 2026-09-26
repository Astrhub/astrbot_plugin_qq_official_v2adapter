import copy
import json
from dataclasses import replace

import pytest
from test_media_boundary import PNG
from test_media_upload import media as media

from v2.errors import V2Error
from v2.media.io import Blob
from v2.media.types import MediaInput
from v2.models import SessionRoute
from v2.settings import DEFAULTS, SettingsStore, merge_patch, validate_settings


@pytest.mark.parametrize("policy", [{}, {"media_roots": []}, {"media_roots": ["/missing", r"Z:\old\media"]}])
@pytest.mark.parametrize("kind", ["image", "file"])
async def test_local_media_needs_no_directory_setting(media, tmp_path, policy, kind):
    path = tmp_path / "本地 文件.png"
    path.write_bytes(PNG)
    media.service.settings = lambda: policy
    route = SessionRoute(media.identity.robot, "group", "target")
    prepared = await media.service.prepare(route, MediaInput(kind, str(path), path.name))
    try:
        assert b"".join([chunk async for chunk in prepared.blob.chunks()]) == PNG
        receipt = await media.service.upload(route, prepared)
        assert receipt["kind"] == kind and b"".join(media.puts) == PNG
        assert path.read_bytes() == PNG
    finally:
        prepared.close()
    assert not media.pool.blobs and media.pool.used == 0


@pytest.mark.parametrize("change", ["roots", "capacity"])
async def test_only_actual_capacity_change_revokes_read_in_progress(media, tmp_path, monkeypatch, change):
    path = tmp_path / "sample"
    path.write_bytes(PNG)
    policy = {"media_roots": [str(tmp_path)], "media_max_bytes": 1000}
    media.service.settings = lambda: policy
    route = SessionRoute(media.identity.robot, "group", "target")
    original = Blob.append
    def changed(blob, chunk):
        original(blob, chunk)
        policy.update({"media_roots": [r"Z:\missing"]} if change == "roots" else {"media_max_bytes": 500})
    monkeypatch.setattr(Blob, "append", changed)
    if change == "capacity":
        with pytest.raises(V2Error) as error:
            await media.service.prepare(route, MediaInput("file", str(path)))
        assert error.value.code == "media_policy_changed"
    else:
        prepared = await media.service.prepare(route, MediaInput("file", str(path)))
        try:
            policy["media_roots"] = None
            await media.service.upload(route, prepared)
            assert b"".join(media.puts) == PNG
        finally:
            prepared.close()
    assert not media.pool.blobs and media.pool.used == 0 and not media.service.tasks


@pytest.mark.parametrize("change", ["platform", "generation", "robot", "scene", "target", "capacity"])
async def test_prepared_media_still_cannot_cross_ownership_or_capacity(media, change):
    route = SessionRoute(media.identity.robot, "group", "target")
    prepared = await media.service.prepare(route, MediaInput("image", "https://assets.test/a.png"))
    try:
        if change in {"platform", "generation"}:
            field = "platform_id" if change == "platform" else "generation"
            media.service.identity = replace(media.identity, **{field: "other"})
        elif change == "robot":
            route = replace(route, robot=replace(route.robot, appid="other"))
        elif change == "scene":
            route = replace(route, scene="c2c")
        elif change == "target":
            route = replace(route, target="other")
        else:
            media.service.settings = lambda: {"media_max_bytes": 1000}
        with pytest.raises(V2Error) as error:
            await media.service.upload(route, prepared)
        assert error.value.code in {"media_scope_mismatch", "stale_generation"}
        assert not media.calls and not media.transfers
    finally:
        prepared.close()


@pytest.mark.parametrize("legacy", [[], ["/missing"], [r"Z:\old\media"], ["/", "../old", "\u0000"], None, "obsolete", {"invalid": True}])
def test_legacy_roots_normalize_without_mutating_caller(legacy):
    value = copy.deepcopy(DEFAULTS)
    value["extensions"]["media_roots"] = legacy
    before = copy.deepcopy(value)
    normalized = validate_settings(value)
    assert "media_roots" not in normalized["extensions"]
    assert normalized["extensions"]["media_max_bytes"] == 32_000_000
    assert value == before


def test_legacy_stored_draft_applied_history_and_client_submission(tmp_path):
    path = tmp_path / "settings.db"
    store = SettingsStore(path)
    old = copy.deepcopy(DEFAULTS)
    old["title"] = "retained"
    old["extensions"].update(media_roots=[r"Z:\missing", "/", "../obsolete"], media_max_bytes=12345, typing_enabled=True)
    encoded = json.dumps(old)
    try:
        store.db.execute("INSERT INTO settings VALUES(?,?,?,?,?,?)", ("key", 4, encoded, 3, encoded, "old-editor"))
        store.db.execute("INSERT INTO versions VALUES(?,?,?)", ("key", 2, encoded))
        store.db.commit()
        current = store.get("key")
        assert current["revision"] == 4 and current["applied_revision"] == 3 and current["editor"] == "old-editor"
        assert "media_roots" not in current["draft"]["extensions"] and "media_roots" not in current["applied"]["extensions"]
        assert store.db.execute("SELECT draft FROM settings").fetchone()[0] == encoded
        saved = store.mutate("key", 4, "new", operation="save", patch={"extensions": {"media_roots": {"obsolete": True}, "media_max_bytes": 23456}})
        assert saved["draft"]["extensions"]["media_max_bytes"] == 23456
        with pytest.raises(V2Error) as error:
            store.mutate("key", 4, "stale", operation="save", patch={"extensions": {"media_roots": []}})
        assert error.value.code == "config_conflict"
        applied = store.mutate("key", 5, "new", operation="apply")
        assert applied["applied"] == saved["draft"] and applied["applied_revision"] == 5
        restored = store.mutate("key", 5, "new", operation="restore", restore_revision=2)
        assert restored["draft"]["extensions"]["media_max_bytes"] == 12345
        assert restored["draft"]["extensions"]["typing_enabled"] is True
        assert restored["draft"]["title"] == "retained" and "media_roots" not in restored["draft"]["extensions"]
        assert store.db.execute("SELECT body FROM versions WHERE revision=2").fetchone()[0] == encoded
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        store.close()
        store = SettingsStore(path)
        assert store.get("key") == restored
    finally:
        store.close()


@pytest.mark.parametrize("patch", [
    {"extensions": {"media_roots": [], "unknown": True}},
    {"extensions": {"media_roots": [], "media_max_bytes": 0}},
    {"extensions": {"media_roots": [], "media_max_bytes": 200_000_001}},
    {"extensions": {"media_roots": "x" * (256 * 1024)}},
])
def test_legacy_compatibility_preserves_other_validation_and_input_size(patch):
    with pytest.raises(V2Error) as error:
        validate_settings(merge_patch(DEFAULTS, patch))
    assert error.value.code == "invalid_settings"
