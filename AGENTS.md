# Repository Guidelines

This file defines the shared contribution and review contract. Base requirements on tracked source, documentation, and CI. Keep workstation paths, private tooling, and personal agent settings outside this document.

## Project Structure & Module Organization

- `main.py` registers the AstrBot plugin; `v2/` implements the shared WebSocket/Webhook core.
- `v2/transport/`, `v2/messaging/`, `v2/media/`, and `v2/extensions/` separate connectivity, delivery, uploads, and interactions.
- `pages/control/` contains plain HTML, CSS, and JavaScript; `assets/` includes the platform icon and its license.
- `tests/` holds regression tests and host assembly helpers; `scripts/` contains the isolated runner.
- `_conf_schema.json` defines settings, `metadata.yaml` declares compatibility, and `docs/ONEBOT.md` documents network integration.

## Build, Test, and Development Commands

Use Python 3.12+ and AstrBot 4.28.1+. Load the plugin from AstrBot's `data/plugins/` through WebUI. No separate build step is required.

Follow `.github/workflows/tests.yml` for the pinned AstrBot revision and dependency setup. Tests require Linux, bubblewrap, and Node 24. With the workflow's host checkout at `.host-source`, run from the repository root:

```bash
ASTRBOT_SOURCE="$PWD/.host-source" \
PYTHON_ENV="$PWD/.host-source/.venv" \
bash scripts/test-isolated.sh -q
```

Append `-k reply` to select reply-related tests. Plugin runtime dependencies are listed in `requirements.txt`.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions/modules, `PascalCase` classes, and uppercase constants. Preserve async I/O patterns and relative imports. JavaScript uses two-space indentation and camelCase. No repository formatter or lint configuration is checked in; follow surrounding formatting.

## Testing Guidelines

Use pytest/pytest-asyncio, `test_*.py` files, and `test_<behavior>` functions. Page tests invoke Node. Always use the isolated runner; tests reject execution outside its sandbox. Cover changed behavior with temporary stores and local upstream fixtures. No numeric coverage threshold is configured.

## Review Requirements

Report actionable defects with a trigger, impact, and file/line evidence. Check host compatibility, instance isolation, string IDs, and delivery persistence. Unknown send outcomes must not be automatically replayed. Assess existing contracts and error boundaries before requesting defensive code; distinguish verified defects from assumptions.

## Commit & Pull Request Guidelines

Use concise messages; history includes `fix:` and `style:` prefixes. Describe final behavior, link relevant issues, and report validation commands/results or untested gaps. Include screenshots for page changes. Update configuration and protocol documentation when affected.

## Configuration & Data

Keep credentials and runtime databases out of version control. Preserve configuration compatibility and existing delivery ledgers. Retain asset attribution and licenses.
