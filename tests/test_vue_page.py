"""Static checks for the Vue ControlAPI candidate page."""

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_vue_control_page_has_a_self_contained_plugin_page_artifact():
    page = ROOT / "pages" / "test" / "index.html"
    metadata = ROOT / "pages" / "test" / "_page.json"
    html = page.read_text(encoding="utf-8")
    info = json.loads(metadata.read_text(encoding="utf-8"))

    assert "window.AstrBotPluginPage" in html
    assert "media_roots" not in html
    assert "shardText" not in html
    assert 'src="' not in html and 'href="' not in html
    assert info["title"] == "Vue SDK 控制页"
    assert "ControlAPI" in info["description"]


def test_vue_source_declares_the_sdk_control_contract():
    source = (ROOT / "dash" / "src" / "api.js").read_text(encoding="utf-8")
    app = (ROOT / "dash" / "src" / "App.vue").read_text(encoding="utf-8")

    for route in ("connection", "connection/save", "config/mutate", "panels/plan"):
        assert route in source
    assert 'apiGet("bootstrap")' in app
    for field in ("appid", "use_markdown", "shard_mode", "onebot"):
        assert field in source
    assert "AstrBotPluginPage" in app
    assert "media_roots" not in app
