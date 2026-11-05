"""Keep production loggers imported from AstrBot; tests are exempt."""

import ast
from pathlib import Path

import pytest


TEST_LOGGER_WHITELIST = {"tests"}
NON_ASTRBOT_LOGGING_MODULES = {"logging", "loguru", "structlog"}


def _logger_import_violations(root: Path) -> list[str]:
    violations = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] in TEST_LOGGER_WHITELIST:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                forbidden = any(
                    alias.name.split(".")[0] in NON_ASTRBOT_LOGGING_MODULES
                    or alias.name.rsplit(".", 1)[-1] == "logger"
                    or alias.asname == "logger"
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                from_astrbot = node.level == 0 and (
                    module == "astrbot" or module.startswith("astrbot.")
                )
                forbidden = module.split(".")[0] in NON_ASTRBOT_LOGGING_MODULES or any(
                    (alias.name == "logger" or alias.asname == "logger")
                    and not (from_astrbot and alias.name == "logger")
                    for alias in node.names
                )
            else:
                continue

            if forbidden:
                violations.append(f"{relative.as_posix()}:{node.lineno}: {ast.unparse(node)}")

    return violations


def test_logger_imports_only_from_astrbot():
    root = Path(__file__).resolve().parents[1]
    violations = _logger_import_violations(root)
    assert not violations, (
        "Non-test Python code must import logger from AstrBot:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize("source", [
    "from astrbot import logger",
    "from astrbot.api import logger",
    "from astrbot.core import logger as bot_logger",
    "from astrbot.api.all import logger",
    "from astrbot.api import (\n    logger as log,\n    AstrBotConfig,\n)",
    "# import logging\ntext = 'from loguru import logger'\nfrom astrbot.api import logger",
    "import json",
])
def test_logger_imports_allow_astrbot_and_ignore_non_import_text(tmp_path, source):
    (tmp_path / "main.py").write_text(source, encoding="utf-8")
    assert _logger_import_violations(tmp_path) == []


@pytest.mark.parametrize("source,line", [
    ("import logging", 1),
    ("import logging as log", 1),
    ("import logging.handlers", 1),
    ("from logging import getLogger as make_logger", 1),
    ("import json, logging as log", 1),
    ("import loguru as log", 1),
    ("from loguru import logger as log", 1),
    ("from loguru import *", 1),
    ("import structlog as log", 1),
    ("import logger", 1),
    ("import external.logger as log", 1),
    ("import external as logger", 1),
    ("from external import logger as log", 1),
    ("from external import other as logger", 1),
    ("from astrbot.api import other as logger", 1),
    ("from astrbot_fake.api import logger", 1),
    ("from .astrbot.api import logger", 1),
    ("from . import logger", 1),
    ("from external import (\n    logger as log,\n)", 1),
    ("def callback():\n    import logging", 2),
    ("try:\n    from astrbot.api import logger\nexcept ImportError:\n    import logging", 4),
])
def test_logger_imports_reject_other_sources_with_file_and_line(tmp_path, source, line):
    path = tmp_path / "v2" / "module.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")

    violations = _logger_import_violations(tmp_path)
    assert len(violations) == 1
    assert violations[0].startswith(f"v2/module.py:{line}: ")


def test_logger_imports_only_tests_directory_is_whitelisted(tmp_path):
    sources = {
        "tests/conftest.py": "import logging",
        "tests/helpers.py": "from loguru import logger",
        "tests/unit/test_logger.py": "from external import logger",
        "main.py": "import logging",
        "api.py": "from loguru import logger",
        "v2/nested/test_logger.py": "from external import logger",
        "new_package/module.py": "import structlog",
    }
    for name, source in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    violations = _logger_import_violations(tmp_path)
    assert [item.split(":", 1)[0] for item in violations] == [
        "api.py", "main.py", "new_package/module.py", "v2/nested/test_logger.py",
    ]
