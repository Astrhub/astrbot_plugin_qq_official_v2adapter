import base64
import json

import pytest
from test_media_boundary import PNG
from test_media_upload import media as media

from v2.errors import V2Error
from v2.media.types import MediaInput
from v2.models import SessionRoute


@pytest.mark.parametrize("base", [0, 1])
async def test_media_part_indices_echo_server_values_without_shifting_bytes(media, base):
    m = media
    m.modes[:] = [{"part_indices": [base, base + 1]}]
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    try:
        result = await m.service.upload(route, prepared, operation_id=f"base-{base}")
        assert result["file_info"] == "actual-file-receipt"
        assert b"".join(m.puts) == PNG
        finishes = [body for path, body in m.calls if path.endswith("upload_part_finish")]
        assert [body["part_index"] for body in finishes] == [base, base + 1]
        assert [int(body["block_size"]) for body in finishes] == [40, len(PNG) - 40]
        contexts = [json.loads(row[0]) for row in m.store.db.execute("SELECT context FROM extension_ops WHERE kind='media_put' ORDER BY op_id")]
        assert [item["part_index"] for item in contexts] == [base, base + 1]
    finally:
        prepared.close()


@pytest.mark.parametrize("indices", [[True, 2], ["1", 2], [2, 3], [-1, 0], [1, 1], [1, 3], [0, 2], [2, 1], [None, 1]])
async def test_media_part_indices_reject_ambiguous_or_noncontiguous_values(media, indices):
    m = media
    m.modes[:] = [{"part_indices": indices}]
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    try:
        with pytest.raises(V2Error) as error:
            await m.service.upload(route, prepared, operation_id="invalid-indices")
        assert error.value.code == "invalid_upload_response"
        assert not m.puts
        assert not any(path.endswith("upload_part_finish") or path.endswith("files") for path, _ in m.calls)
    finally:
        prepared.close()
