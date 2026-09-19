import json
import os
import subprocess
from pathlib import Path

from v2.settings import DEFAULTS


def test_page_runtime():
    node = os.environ.get("V2_NODE")
    assert node, "Node is required for the Plugin Pages runtime fixture test."
    result = subprocess.run([node, "--disable-wasm-trap-handler", "/plugin/tests/page_runtime.mjs"],
                            input=json.dumps(DEFAULTS), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PAGE:" in result.stdout


def test_page_has_no_external_resources_or_fixture_fallback():
    root = Path("/plugin/pages/control")
    html, js = (root / "index.html").read_text(), (root / "app.js").read_text()
    assert 'src="./app.js"' in html and 'href="./style.css"' in html
    assert "window.AstrBotPluginPage" in js and "bridge.ready()" in js
    assert "localStorage" not in js and "fetch(" not in js and "innerHTML" not in js
    assert "fixture" not in js and "https://" not in html
