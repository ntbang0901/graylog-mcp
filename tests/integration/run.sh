#!/usr/bin/env bash
# Run the integration suite against one Graylog version: tests/integration/run.sh v52
# Profiles: v43 v50 v52 v61 v70. Set KEEP=1 to leave the containers running.
set -euo pipefail
profile="${1:?usage: run.sh <v43|v50|v52|v61|v70>}"
port="90${profile#v}"
here="$(cd "$(dirname "$0")" && pwd)"
compose=(docker compose -f "$here/docker-compose.yml" -p "graylog-mcp-$profile" --profile "$profile")
python="${PYTHON:-python}"

"${compose[@]}" up -d
trap '[[ "${KEEP:-0}" == 1 ]] || "${compose[@]}" down -v' EXIT
export GRAYLOG_IT_URL="http://127.0.0.1:$port"
export GRAYLOG_IT_SEEDED_AT_FILE="/tmp/graylog-it-seeded-at-$profile"
"$python" "$here/seed.py"
"$python" -m pytest -m integration -q -s "$here" "${@:2}"
