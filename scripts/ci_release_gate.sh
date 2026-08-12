#!/usr/bin/env bash
# Set publish=true when a push is a matching release tag or changes VERSION on a release branch.
set -euo pipefail

publish=false

if [[ "${GITHUB_EVENT_NAME:-}" != "push" ]]; then
  echo "publish=false" >> "${GITHUB_OUTPUT}"
  exit 0
fi

case "${GITHUB_REF_TYPE:-}" in
  tag)
    name="${GITHUB_REF_NAME:-}"
    if [[ "${name}" == v* && "${name}" != "vdev" ]]; then
      publish=true
    fi
    ;;
  branch)
    ref="${GITHUB_REF:-}"
    if [[ "${ref}" == "refs/heads/main" || "${ref}" == refs/heads/release/* ]]; then
      before="${GITHUB_EVENT_BEFORE:-}"
      if [[ -z "${before}" || "${before}" == "0000000000000000000000000000000000000000" ]]; then
        if git rev-parse HEAD~1 >/dev/null 2>&1; then
          before="HEAD~1"
        else
          echo "publish=false" >> "${GITHUB_OUTPUT}"
          exit 0
        fi
      fi
      if git diff --name-only "${before}" "${GITHUB_SHA}" | grep -qx VERSION; then
        publish=true
      fi
    fi
    ;;
esac

echo "publish=${publish}" >> "${GITHUB_OUTPUT}"
