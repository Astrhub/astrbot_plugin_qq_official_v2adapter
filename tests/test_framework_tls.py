"""Use the real host CA builder without sharing authenticated sessions."""
import asyncio
import socket
import ssl

import aiohttp
import certifi
import pytest
from astrbot.core.utils import http_ssl as host_tls
from test_media_put_network import local_tls

from v2 import onboarding
from v2.media import io as media_io
from v2.models import InstanceKey
from v2.transport import http


async def test_host_tls_source_ca_loading_and_session_isolation(config, monkeypatch):
    assert http.build_ssl_context_with_certifi is host_tls.build_ssl_context_with_certifi
    assert onboarding.build_ssl_context_with_certifi is host_tls.build_ssl_context_with_certifi
    assert media_io.build_ssl_context_with_certifi is host_tls.build_ssl_context_with_certifi
    monkeypatch.setattr(host_tls, "_SHARED_TLS_CONTEXT", None)
    default_context, bundle = ssl.create_default_context, certifi.where
    calls = []
    def system_context(*args, **kwargs):
        calls.append("system")
        return default_context(*args, **kwargs)
    def ca_bundle():
        calls.append("certifi")
        return bundle()
    monkeypatch.setattr(ssl, "create_default_context", system_context)
    monkeypatch.setattr(certifi, "where", ca_bundle)
    transports = [http.HTTPTransport(InstanceKey.from_config({**config, "id": name}), "synthetic-secret", timeout=7)
                  for name in ("first", "second")]
    sessions = [transport._make_session() for transport in transports]
    sessions += [onboarding.Onboarding(None)._make_session(), media_io.UploadTransfer._session()]
    try:
        context = host_tls.build_ssl_context_with_certifi()
        assert calls == ["system", "certifi"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert context.cert_store_stats()["x509_ca"] > 0
        assert len({id(s) for s in sessions}) == len({id(s.connector) for s in sessions}) == 4
        assert [s.connector.limit for s in sessions] == [8, 8, 4, 1]
        assert [s.timeout.total for s in sessions] == [7, 7, 10, 30]
        assert sessions[-1].connector.force_close
        for session in sessions:
            assert session.connector._ssl is context
            assert not session.trust_env and not session.auto_decompress
            assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
            assert session._default_auth is None
            assert not {"Authorization", "Cookie", "X-Union-Appid"} & session.headers.keys()
        sessions[0].headers["Authorization"] = "QQBot synthetic-only"
        assert all("Authorization" not in s.headers for s in sessions[1:])
        await sessions[0].close()
        assert all(not s.closed for s in sessions[1:])
    finally:
        for session in sessions:
            await session.close()
        for transport in transports:
            await transport.close()


@pytest.mark.parametrize("purpose", ["http", "onboarding", "upload"])
@pytest.mark.parametrize("trusted,hostname", [(False, "cos.test"), (True, "wrong.test"), (True, "cos.test")])
async def test_all_real_session_connectors_enforce_tls(config, tmp_path, monkeypatch, purpose, trusted, hostname):
    async with local_tls(tmp_path) as (port, certificate, calls):
        monkeypatch.setattr(host_tls, "_SHARED_TLS_CONTEXT", None)
        if trusted:
            monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
        else:
            monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        lookups = []
        async def system_dns(host, service, **kwargs):
            assert host == hostname and service == port
            lookups.append((host, service))
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", system_dns)
        factories = {"http": http.HTTPTransport(InstanceKey.from_config(config), "synthetic")._make_session,
                     "onboarding": onboarding.Onboarding(None)._make_session,
                     "upload": media_io.UploadTransfer._session}
        session = factories[purpose]()
        connector = session.connector
        try:
            assert connector._ssl is host_tls.build_ssl_context_with_certifi()
            if trusted and hostname == "cos.test":
                async with session.put(f"https://{hostname}:{port}/part", data=b"unaltered", allow_redirects=False) as response:
                    assert response.status == 200
                    await response.read()
                assert calls == [b"unaltered"]
            else:
                with pytest.raises(aiohttp.ClientConnectorCertificateError):
                    async with session.put(f"https://{hostname}:{port}/part", data=b"unaltered", allow_redirects=False):
                        pytest.fail("Untrusted certificate or wrong hostname was accepted")
                assert not calls
            assert lookups == [(hostname, port)]
        finally:
            await session.close()
        assert session.closed and connector.closed
