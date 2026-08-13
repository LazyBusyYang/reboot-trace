#!/usr/bin/env bash
set -euo pipefail

skip_docker=false
if [[ "${1:-}" == "--skip-docker" ]]; then skip_docker=true; shift; fi
if [[ $# -ne 0 ]]; then echo "usage: $0 [--skip-docker]" >&2; exit 2; fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo_root"

PYTHONPATH=backend python3 -m compileall -q backend/reboot_trace backend/tests
PYTHONPATH=backend pytest -q
python3 docs/sync_styles.py --check
git diff --check

for required in backend/Dockerfile frontend/Dockerfile deploy/compose.example.yaml deploy/runtime-config.example.json scripts/instances/reboot-trace-instance.sh scripts/instances/start-dev-1.sh scripts/instances/start-dev-4.sh scripts/instances/start-dev-5.sh scripts/instances/start-dev-9.sh; do
  test -s "$required" || { echo "FAIL: missing deployment artifact $required" >&2; exit 1; }
done
grep -q 'npm ci' frontend/Dockerfile || { echo "FAIL: frontend image must use lockfile install" >&2; exit 1; }
grep -q '^USER reboot-trace' backend/Dockerfile || { echo "FAIL: backend image must be non-root" >&2; exit 1; }
grep -q '^USER nginx' frontend/Dockerfile || { echo "FAIL: frontend image must be non-root" >&2; exit 1; }
if grep -q 'pid: host' deploy/compose.example.yaml || grep -q '/proc:/host/proc' deploy/compose.example.yaml; then
  echo "FAIL: compose must not claim host PID/proc can identify a sibling target container" >&2; exit 1
fi
grep -q 'native process inside each target container' deploy/compose.example.yaml || { echo "FAIL: compose must state the native collector topology" >&2; exit 1; }
grep -q '^/var/reboot-trace/$' .gitignore || { echo "FAIL: runtime database directory must be ignored" >&2; exit 1; }
grep -q '^runtime/$' .gitignore || { echo "FAIL: local instance runtime directories must be ignored" >&2; exit 1; }
grep -q 'RT_REQUIRE_CONTAINER_MARKER=true' scripts/instances/reboot-trace-instance.sh || { echo "FAIL: local instances must require a supported container marker" >&2; exit 1; }
grep -q 'RT_SERVICE_TOKEN=' scripts/instances/reboot-trace-instance.sh || { echo "FAIL: local instances must bind HTTP status to the launched service process" >&2; exit 1; }
grep -q 'start_ticks' backend/reboot_trace/process_control.py || { echo "FAIL: process state must protect against stale PID reuse" >&2; exit 1; }
grep -q 'operator_verification_required' backend/reboot_trace/database.py || { echo "FAIL: storage status must preserve the operator verification boundary" >&2; exit 1; }
grep -q -- '--uid 10001' backend/Dockerfile || { echo "FAIL: backend image must use fixed uid 10001" >&2; exit 1; }

test -f frontend/package-lock.json || { echo "FAIL: frontend/package-lock.json is missing" >&2; exit 1; }
(
  cd frontend
  if [ ! -d node_modules ]; then npm ci; fi
  npm test
  npm run build
  npm audit --offline --audit-level=moderate
  test -z "$(find src -type f -name '*.js' -print -quit)" || { echo "FAIL: TypeScript emitted JavaScript into frontend/src" >&2; exit 1; }
)

if $skip_docker; then
  echo "Docker image gates skipped explicitly; source acceptance passed."
elif command -v docker >/dev/null 2>&1; then
  docker build -f backend/Dockerfile -t reboot-trace-backend:acceptance .
  docker build -f frontend/Dockerfile -t reboot-trace-frontend:acceptance .
  docker run --rm reboot-trace-frontend:acceptance nginx -t
  if docker compose version >/dev/null 2>&1; then
    docker compose -f deploy/compose.example.yaml config --quiet
  else
    echo "FAIL: docker compose plugin is required for final image acceptance" >&2
    exit 1
  fi
else
  echo "FAIL: docker CLI is required for final image acceptance" >&2
  exit 1
fi

echo "All requested acceptance gates passed."
