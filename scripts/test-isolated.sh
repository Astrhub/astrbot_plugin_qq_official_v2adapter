#!/usr/bin/env bash
set -euo pipefail
# Only source/dependencies are mounted; host runtime data and credentials stay outside.
ROOT=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
ASTRBOT_SOURCE=${ASTRBOT_SOURCE:-/root/work/AstrBot}
PYTHON_ENV=${PYTHON_ENV:-$ASTRBOT_SOURCE/.venv}
NODE_BIN=${NODE_BIN:-$(command -v node || true)}
command -v bwrap >/dev/null
[[ -x "$PYTHON_ENV/bin/python" ]]
[[ -d "$ASTRBOT_SOURCE/astrbot" ]]
args=(--unshare-all --die-with-parent --new-session --cap-drop ALL --uid 65534 --gid 65534
      --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind /lib64 /lib64
      --proc /proc --dev /dev --size 268435456 --tmpfs /tmp --size 536870912 --tmpfs /work --dir /plugin --dir /opt
      --ro-bind "$ASTRBOT_SOURCE/astrbot" /opt/astrbot/astrbot
      --ro-bind "$PYTHON_ENV" /opt/venv
      --clearenv --setenv PATH /opt/venv/bin:/usr/bin:/bin --setenv HOME /work
      --setenv ASTRBOT_ROOT /work --setenv PYTHONPATH /opt/astrbot:/plugin:/work
      --setenv TESTING true --setenv ASTRBOT_TEST_MODE true --setenv PYTHONDONTWRITEBYTECODE 1
      --setenv V2_TEST_SANDBOX 1 --chdir /work)
for entry in main.py metadata.yaml _conf_schema.json LICENSE requirements.txt v2 pages tests; do
    args+=(--ro-bind "$ROOT/$entry" "/plugin/$entry")
done
if [[ -n "$NODE_BIN" ]]; then
    args+=(--ro-bind "$(readlink -f "$NODE_BIN")" /opt/node --setenv V2_NODE /opt/node)
fi
ulimit -c 0
ulimit -u 128
ulimit -f 131072
ulimit -t 180
ulimit -n 512
ulimit -v 8388608
exec timeout --signal=TERM --kill-after=10s 240s bwrap "${args[@]}" -- /opt/venv/bin/python -m pytest /plugin/tests -p no:cacheprovider -o asyncio_mode=auto -o 'markers=assembly: isolated real AstrBot lifecycle assembly' "$@"
