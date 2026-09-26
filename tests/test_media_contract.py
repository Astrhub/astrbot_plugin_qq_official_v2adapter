"""Server upload contracts stay separate from local caching and resource limits."""
import asyncio
import base64

import pytest
from test_media_boundary import PNG
from test_media_upload import media as media
from test_media_upload import sending_core

from v2.errors import V2Error
from v2.media.types import MediaInput
from v2.messaging.outbound import parse_message
from v2.models import SessionRoute


def override_upload_reply(m, monkeypatch, *, ttl=300, schedule=None, finish_failures=0):
    request = m.http.request
    remaining = [finish_failures]
    delays = []

    async def reply(spec, **kwargs):
        response = await request(spec, **kwargs)
        if spec.path.endswith("upload_prepare") and schedule is not None:
            response.data["upload_config"] = dict(schedule)
        if spec.path.endswith("upload_part_finish") and remaining[0]:
            remaining[0] -= 1
            raise V2Error("qq_api_error", "Fixture upload retry.", business_code=40093001,
                          phase="rejected", status=400, http_status=400)
        if spec.path.endswith("/files"):
            response.data["ttl"] = ttl
        return response

    async def advance(seconds):
        delays.append(seconds)
        m.clock[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(m.http, "request", reply)
    monkeypatch.setattr(m.service, "sleep", advance)
    monkeypatch.setattr(m.service, "clock", lambda: m.clock[0], raising=False)
    return delays


@pytest.mark.parametrize("ttl", [0, 172800, 31536001])
async def test_receipt_keeps_server_lifetime_without_a_one_year_ceiling(media, monkeypatch, ttl):
    m = media
    override_upload_reply(m, monkeypatch, ttl=ttl)
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("image", "https://assets.test/image"))
    try:
        receipt = await m.service.upload(route, prepared, operation_id="server-lifetime")
        assert receipt["ttl"] == ttl and receipt["file_info"] == "actual-file-receipt"
        assert receipt["expires_at"] == (None if ttl == 0 else m.clock[0] + ttl)
    finally:
        prepared.close()


async def test_zero_ttl_send_and_transient_raw_url_never_persist_signed_links(media, monkeypatch):
    m = media
    override_upload_reply(m, monkeypatch, ttl=0)
    core, chat = sending_core(m)
    image = MediaInput("image", "base64://" + base64.b64encode(PNG).decode())
    try:
        result = await core.send(chat.route, [image], source=chat.source, operation_id="fresh-link")
        assert result["media"]["raw_url"] == "https://cos.test/private?sign=must-not-persist"
        assert result["message_id"] == "actual-media-message"
        cached = await core.send(chat.route, [image], source=chat.source, operation_id="cached-link")
        assert cached["media"]["raw_url"] == result["media"]["raw_url"]
        assert sum(path.endswith("/files") for path, _ in m.calls) == 1
        assert "must-not-persist" not in "\n".join(m.store.db.iterdump())
        assert "raw_url" not in m.store.operation(m.identity.robot, "fresh-link")["result"]["media"]
    finally:
        await core.close()


async def test_local_soft_limit_defaults_to_documented_file_fallback(media, tmp_path):
    path = tmp_path / "large-image.bin"
    with path.open("wb") as stream:
        stream.truncate(20_000_001)
    route = SessionRoute(media.identity.robot, "group", "g")
    prepared = await media.service.prepare(route, MediaInput("image", str(path)))
    try:
        assert prepared.kind == "file" and prepared.input.kind == "image"
        assert prepared.blob.size == 20_000_001
        atoms, _ = parse_message([{"type": "image", "data": {"file": str(path)}}])
        assert atoms[0][1].allow_file_fallback is True
    finally:
        prepared.close()
    with pytest.raises(V2Error) as exc:
        await media.service.prepare(route, MediaInput("image", str(path), allow_file_fallback=False))
    assert exc.value.code == "media_soft_limit" and media.pool.used == 0


@pytest.mark.parametrize("concurrency,expected_peak", [(2, 2), (32, 8)])
async def test_server_concurrency_uses_bounded_parallel_parts(media, monkeypatch, concurrency, expected_peak):
    m = media
    override_upload_reply(m, monkeypatch, schedule={"concurrency": concurrency, "retry_timeout": 300, "retry_delay": 7})
    data = b"part" * 120
    prepared = await m.service.prepare(SessionRoute(m.identity.robot, "group", "g"),
                                      MediaInput("file", "base64://" + base64.b64encode(data).decode()))
    put = m.service.transfer.put
    reached, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    timeouts = []

    async def bounded(url, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        timeouts.append(kwargs.get("request_seconds"))
        if active == expected_peak:
            reached.set()
        try:
            await release.wait()
            return await put(url, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(m.service.transfer, "put", bounded)
    task = asyncio.create_task(m.service.upload(SessionRoute(m.identity.robot, "group", "g"), prepared))
    try:
        await asyncio.wait_for(reached.wait(), 1)
        release.set()
        await asyncio.wait_for(task, 5)
        assert peak == expected_peak
        assert all(value is not None and 290 <= value <= 300 for value in timeouts)
        assert len(m.puts) == 12 and m.calls[-1][0].endswith("/files")
        assert sorted(body["part_index"] for path, body in m.calls if path.endswith("upload_part_finish")) == list(range(12))
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        prepared.close()


@pytest.mark.parametrize("failures,success", [(1, True), (2, False)])
async def test_finish_retry_honors_server_delay_and_budget(media, monkeypatch, failures, success):
    m = media
    delays = override_upload_reply(m, monkeypatch, schedule={"concurrency": 1, "retry_timeout": 10, "retry_delay": 7}, finish_failures=failures)
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("file", "base64://" + base64.b64encode(b"part").decode()))
    try:
        if success:
            await m.service.upload(route, prepared)
            assert m.calls[-1][0].endswith("/files")
        else:
            with pytest.raises(V2Error) as exc:
                await m.service.upload(route, prepared)
            assert exc.value.business_code == 40093001
            assert not any(path.endswith("/files") for path, _ in m.calls)
        assert delays == [7] and len(m.puts) == 1
        assert sum(path.endswith("upload_part_finish") for path, _ in m.calls) == 2
    finally:
        prepared.close()


@pytest.mark.parametrize("phase,success", [("not_sent", True), ("result_unknown", False)])
async def test_part_retry_never_replays_unknown_bytes(media, monkeypatch, phase, success):
    m = media
    delays = override_upload_reply(m, monkeypatch, schedule={"concurrency": 1, "retry_timeout": 10, "retry_delay": 2})
    put = m.service.transfer.put
    attempts = 0

    async def fail_once(url, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise V2Error("media_network_failure", "Fixture transfer failure.", phase=phase, status=502)
        return await put(url, **kwargs)

    monkeypatch.setattr(m.service.transfer, "put", fail_once)
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("file", "base64://" + base64.b64encode(b"part").decode()))
    try:
        if success:
            await m.service.upload(route, prepared)
            assert attempts == 2 and delays == [2]
            assert not any(row["state"] == "unknown" for row in m.state.recent(route.robot))
        else:
            with pytest.raises(V2Error) as exc:
                await m.service.upload(route, prepared)
            assert exc.value.phase == "result_unknown" and attempts == 1 and not delays
            assert not any(path.endswith("upload_part_finish") or path.endswith("/files") for path, _ in m.calls)
            assert any(row["state"] == "unknown" for row in m.state.recent(route.robot))
    finally:
        prepared.close()


async def test_zero_delay_server_errors_still_have_bounded_attempts(media, monkeypatch):
    m = media
    override_upload_reply(m, monkeypatch, schedule={"concurrency": 1, "retry_timeout": 300, "retry_delay": 0}, finish_failures=100)
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("file", "base64://" + base64.b64encode(b"part").decode()))
    try:
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(m.service.upload(route, prepared), 3)
        assert exc.value.business_code == 40093001
        assert sum(path.endswith("upload_part_finish") for path, _ in m.calls) == 16
        assert not any(path.endswith("/files") for path, _ in m.calls)
    finally:
        prepared.close()


async def test_parallel_part_failure_cancels_peer_and_keeps_unknown_ledger(media, monkeypatch):
    m = media
    override_upload_reply(m, monkeypatch, schedule={"concurrency": 2, "retry_timeout": 300, "retry_delay": 1})
    entered = asyncio.Event()
    active = set()

    async def put(url, **kwargs):
        active.add(kwargs["offset"])
        if kwargs["offset"] == 0:
            entered.set()
            await asyncio.Event().wait()
        await entered.wait()
        raise V2Error("media_network_failure", "Fixture ambiguous PUT.", phase="result_unknown", status=502)

    monkeypatch.setattr(m.service.transfer, "put", put)
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("file", "base64://" + base64.b64encode(b"part" * 20).decode()))
    try:
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(m.service.upload(route, prepared, operation_id="parallel-unknown"), 3)
        assert exc.value.phase == "result_unknown" and active == {0, 40}
        assert not m.service.tasks
        assert not any(path.endswith("upload_part_finish") or path.endswith("/files") for path, _ in m.calls)
        assert sum(row["state"] == "unknown" for row in m.state.recent(route.robot)) == 2
    finally:
        prepared.close()
