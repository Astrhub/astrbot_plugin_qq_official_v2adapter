"""System network uploads retain TLS, endpoint and write-outcome boundaries."""
import asyncio
import base64
import socket
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from test_media_boundary import PNG
from test_media_upload import media as media
from test_transport_http import upstream

from v2.errors import V2Error
from v2.media.io import BlobPool, UploadTransfer
from v2.media.types import MediaInput
from v2.models import SessionRoute


async def test_official_put_accepts_fake_ip_without_local_resolution_gate(media, monkeypatch):
    m = media
    original = m.http.request
    async def request(spec, **kwargs):
        response = await original(spec, **kwargs)
        if spec.path.endswith("/upload_prepare"):
            for part in response.data["parts"]:
                part["presigned_url"] = "https://198.18.0.89/part/" + str(part["index"]) + "?sign=fixture%2Fprivate&x=a+b"
        return response
    monkeypatch.setattr(m.http, "request", request)
    factory = m.service.transfer.session_factory
    def mapped():
        session = factory()
        raw = session.request
        def put(method, url, **kwargs):
            assert method == "PUT" and url.startswith("https://198.18.0.89/part/")
            assert kwargs["allow_redirects"] is False
            # Assert the original destination before the test-only loopback remap.
            result = raw(method, url.replace("https://198.18.0.89/", "https://cos.test/", 1), **kwargs)
            m.transfers[-1] = (method, url)
            return result
        session.request = put
        return session
    monkeypatch.setattr(m.service.transfer, "session_factory", mapped)
    route = SessionRoute(m.identity.robot, "group", "target")
    prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    try:
        await m.service.upload(route, prepared, operation_id="fake-ip-upload")
        assert b"".join(m.puts) == PNG
        assert m.transfers == [("PUT", f"https://198.18.0.89/part/{i}?sign=fixture%2Fprivate&x=a+b") for i in (0, 1)]
        assert "fixture%2Fprivate" not in "\n".join(m.store.db.iterdump())
    finally:
        prepared.close()


@asynccontextmanager
async def local_tls(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.invalid")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName("cos.test")]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate, private_key = tmp_path / "test.pem", tmp_path / "test.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, private_key)
    calls = []
    async def handle(request):
        assert request.method == "PUT" and request.host == "cos.test:" + str(port)
        assert not {"authorization", "cookie", "x-union-appid"} & {k.lower() for k in request.headers}
        calls.append(await request.read())
        return web.Response()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield port, certificate, calls
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("trusted,hostname", [(False, "cos.test"), (True, "wrong.test"), (True, "cos.test")])
async def test_production_put_uses_system_resolver_and_verifies_tls(tmp_path, monkeypatch, trusted, hostname):
    async with local_tls(tmp_path) as (port, certificate, calls):
        loop = asyncio.get_running_loop()
        lookups = []
        async def system_dns(host, service, **kwargs):
            assert host == hostname and service == port
            lookups.append((host, service))
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
        monkeypatch.setattr(loop, "getaddrinfo", system_dns)
        pool = BlobPool(tmp_path / "spool")
        blob = await pool.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
        transfer = UploadTransfer(pool)
        session = transfer._session()
        assert session.connector._ssl is True and not session.trust_env
        # Trust only the generated fixture CA; hostname verification remains enabled.
        if trusted:
            context = ssl.create_default_context(cafile=str(certificate))
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            session.connector._ssl = context
        transfer.session_factory = lambda: session
        try:
            if trusted and hostname == "cos.test":
                await transfer.put(f"https://{hostname}:{port}/part?sign=private", blob=blob)
                assert calls == [PNG]
            else:
                with pytest.raises(V2Error) as error:
                    await transfer.put(f"https://{hostname}:{port}/part?sign=private", blob=blob)
                assert error.value.code == "media_network_failure" and error.value.phase == "not_sent"
                assert not calls and "private" not in str(error.value)
            assert lookups == [(hostname, port)] and session.closed and not transfer.tasks
        finally:
            await session.close()
            await transfer.close()
            pool.close()


async def test_upload_timeout_is_unknown_not_replayed_and_closes_session(tmp_path):
    from test_media_upload import AssetSession
    entered, release = asyncio.Event(), asyncio.Event()
    async def handle(request):
        await request.read()
        entered.set()
        await release.wait()
        return web.Response()
    pool = BlobPool(tmp_path / "spool")
    blob = await pool.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
    async with upstream(handle) as base:
        session = AssetSession(base, [])
        transfer = UploadTransfer(pool, session_factory=lambda: session)
        # Reschedule this task's outer deadline rather than waiting on a wall-clock timer.
        async def run():
            async with asyncio.timeout(None) as deadline:
                box.append(deadline)
                try:
                    await transfer.put("https://cos.test/part?sign=private", blob=blob)
                except asyncio.CancelledError as exc:
                    phases.append(exc.phase)
                    raise
        box, phases = [], []
        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            box[0].reschedule(asyncio.get_running_loop().time() - 1)
            with pytest.raises(TimeoutError):
                await task
            assert session.inner.closed and not transfer.tasks and len(session.calls) == 1
            assert phases == ["result_unknown"]
        finally:
            release.set()
            await transfer.close()
            pool.close()


async def test_upload_network_error_traceback_does_not_expose_signed_url(tmp_path):
    import traceback

    import aiohttp
    class BrokenSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        def request(self, method, url, **kwargs):
            raise aiohttp.ServerTimeoutError("timed out at " + url)
    pool = BlobPool(tmp_path / "spool")
    blob = await pool.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
    transfer = UploadTransfer(pool, session_factory=BrokenSession)
    url = "https://cos.test/part?sign=private"
    try:
        with pytest.raises(V2Error) as error:
            await transfer.put(url, blob=blob)
        assert error.value.phase == "result_unknown"
        assert "sign=private" not in "".join(traceback.format_exception(error.value))
    finally:
        await transfer.close()
        pool.close()
