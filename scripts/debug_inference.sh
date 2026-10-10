#!/usr/bin/env bash
# Keep the service in a VS Code task terminal across GUI debug sessions.
set -euo pipefail
cd "$(dirname "$0")/.."
service_url=http://127.0.0.1:8765/v1/models
service_available() {
    curl --noproxy '*' --fail --silent --output /dev/null --max-time 2 "$service_url"
}
if service_available; then
    echo 'Imajev service ready (existing process)'
    exit 0
fi
bash scripts/launch_inference.sh &
service_pid=$!
trap 'kill "$service_pid" 2>/dev/null || true; wait "$service_pid" 2>/dev/null || true' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for ((attempt=0; attempt<300; attempt++)); do
    if ! kill -0 "$service_pid" 2>/dev/null; then
        wait "$service_pid"
        exit 1
    fi
    if service_available; then
        echo 'Imajev service ready'
        wait "$service_pid"
        exit $?
    fi
    sleep 1
done
echo 'Timed out waiting for Imajev service startup' >&2
exit 1
