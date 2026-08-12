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

for required in backend/Dockerfile frontend/Dockerfile deploy/compose.example.yaml deploy/runtime-config.example.json; do
  test -s "$required" || { echo "FAIL: missing deployment artifact $required" >&2; exit 1; }
done
grep -q 'npm ci' frontend/Dockerfile || { echo "FAIL: frontend image must use lockfile install" >&2; exit 1; }
grep -q '^USER reboot-trace' backend/Dockerfile || { echo "FAIL: backend image must be non-root" >&2; exit 1; }
grep -q '^USER nginx' frontend/Dockerfile || { echo "FAIL: frontend image must be non-root" >&2; exit 1; }
grep -q 'pid: host' deploy/compose.example.yaml || { echo "FAIL: compose must expose host PID namespace" >&2; exit 1; }
grep -q '/proc:/host/proc:ro' deploy/compose.example.yaml || { echo "FAIL: compose must mount host procfs read-only" >&2; exit 1; }

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
