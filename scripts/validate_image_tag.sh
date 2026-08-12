#!/usr/bin/env bash
set -euo pipefail

tag="${1:-}"
if [[ "${tag}" == "dev" || "${tag}" =~ ^sha-[A-Za-z0-9._-]+$ ]]; then
  exit 0
fi

echo "image tag must be exactly 'dev' or match ^sha-[A-Za-z0-9._-]+$ (got: ${tag})" >&2
exit 1
