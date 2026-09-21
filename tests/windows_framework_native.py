"""Native framework-reuse checks using only disposable local samples."""
import base64
import hashlib
import ssl
import sys
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from native_tests import CORE, NativeMedia, astrbot, io


def load_framework_modules():
    from astrbot.core.utils import http_ssl as host_tls
    from PIL import Image

    from v2.media.service import MediaService
    from v2.media.types import MediaInput
    from v2.models import InstanceKey, SessionRoute
    from v2.onboarding import Onboarding
    from v2.transport.http import HTTPTransport
    return host_tls, Image, MediaService, MediaInput, InstanceKey, SessionRoute, Onboarding, HTTPTransport


host_tls, Image, MediaService, MediaInput, InstanceKey, SessionRoute, Onboarding, HTTPTransport = load_framework_modules()


class NativeFramework(unittest.IsolatedAsyncioTestCase):
    setUp = NativeMedia.setUp

    async def test_large_jpeg_unchanged_file_uri_path_base64(self):
        with BytesIO() as output:
            Image.new("RGB", (16, 16)).save(output, format="JPEG")
            original = output.getvalue()
        segment = b"\xff\xef" + (60002).to_bytes(2, "big") + b"A" * 60000
        data = original[:2] + segment * 2 + original[2:]
        with Image.open(BytesIO(data)) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (16, 16))
        self.assertEqual(len(data), 120639)
        self.path.write_bytes(data)
        identity = InstanceKey.from_config({"id": "native-test", "appid": "synthetic", "secret": "synthetic-only"})
        service = MediaService(identity, SimpleNamespace(check=lambda: None), None, self.pool,
                               settings=lambda: {"media_roots": [str(self.allowed)]})
        try:
            with patch.object(Image, "open", side_effect=AssertionError("No decoding or reencoding during preparation")):
                for scene in ("group", "c2c", "channel"):
                    for value in (str(self.path).upper(), self.path.as_uri(), "base64://" + base64.b64encode(data).decode()):
                        with self.subTest(scene=scene, source=value[:20]):
                            prepared = await service.prepare(SessionRoute(identity.robot, scene, "target"), MediaInput("image", value, "metadata.jpg"))
                            self.assertEqual(b"".join([part async for part in prepared.blob.chunks()]), data)
                            self.assertEqual(prepared.blob.hashes()["sha256"], hashlib.sha256(data).hexdigest())
                            self.assertEqual(prepared.blob.size, len(data))
                            prepared.close()
            self.assertEqual(self.path.read_bytes(), data)
            self.assertFalse(self.pool.blobs)
            self.assertEqual(self.pool.used, 0)
        finally:
            await service.close()

    async def test_url_never_uses_local_reader_parser_or_network(self):
        identity = InstanceKey.from_config({"id": "native-test", "appid": "synthetic", "secret": "synthetic-only"})
        service = MediaService(identity, SimpleNamespace(check=lambda: None), None, self.pool)
        try:
            with patch.object(self.pool, "load", side_effect=AssertionError("URL must stay remote")), patch.object(Image, "open", side_effect=AssertionError("No URL decoding")):
                prepared = await service.prepare(SessionRoute(identity.robot, "group", "target"), MediaInput("image", "https://assets.invalid/image"))
                self.assertIsNone(prepared.blob)
                self.assertEqual(prepared.descriptor()["source"], "url")
                self.assertFalse(self.pool.blobs)
                prepared.close()
        finally:
            await service.close()

    async def test_actual_host_tls_context_is_shared_but_sessions_are_not(self):
        from v2 import onboarding
        from v2.transport import http
        self.assertIs(io.build_ssl_context_with_certifi, host_tls.build_ssl_context_with_certifi)
        self.assertIs(http.build_ssl_context_with_certifi, host_tls.build_ssl_context_with_certifi)
        self.assertIs(onboarding.build_ssl_context_with_certifi, host_tls.build_ssl_context_with_certifi)
        context = host_tls.build_ssl_context_with_certifi()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)
        identity = InstanceKey.from_config({"id": "native-test", "appid": "synthetic", "secret": "synthetic-only"})
        sessions = [HTTPTransport(identity, "synthetic-only")._make_session(), Onboarding(None)._make_session(), io.UploadTransfer._session()]
        try:
            self.assertEqual(len({id(s) for s in sessions}), 3)
            self.assertEqual([s.connector.limit for s in sessions], [8, 4, 1])
            for session in sessions:
                self.assertIs(session.connector._ssl, context)
                self.assertFalse(session.trust_env)
                self.assertNotIn("Authorization", session.headers)
            await sessions[0].close()
            self.assertTrue(all(not s.closed for s in sessions[1:]))
        finally:
            for session in sessions:
                await session.close()


if __name__ == "__main__":
    print("NATIVE", sys.version, "HOST", astrbot.__version__, flush=True)
    for name in ("astrbot/core/utils/http_ssl.py", "astrbot/utils/http_ssl_common.py"):
        print("HOST_SHA256", name, hashlib.sha256((CORE / name).read_bytes()).hexdigest(), flush=True)
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (NativeMedia, NativeFramework)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("RESULT", {"run": result.testsRun, "passed": result.testsRun - len(result.skipped) - len(result.errors) - len(result.failures),
                     "skipped": len(result.skipped), "errors": len(result.errors), "failures": len(result.failures)}, flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)
