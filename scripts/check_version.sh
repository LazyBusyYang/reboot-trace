#!/usr/bin/env bash
# Validate the repository VERSION and require Git tags to match v${VERSION}.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
version_file="${repo_root}/VERSION"

if [[ ! -f "${version_file}" ]]; then
  echo "VERSION file missing at ${version_file}" >&2
  exit 1
fi

line_count="$(tr -d '\r' < "${version_file}" | awk 'END { print NR }')"
if [[ "${line_count}" -ne 1 ]]; then
  echo "VERSION must contain exactly one line" >&2
  exit 1
fi
version="$(tr -d '\r' < "${version_file}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [[ -z "${version}" ]]; then
  echo "VERSION is empty" >&2
  exit 1
fi

identifier='(0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)'
number='(0|[1-9][0-9]*)'
pattern="^${number}\\.${number}\\.${number}(-${identifier}(\\.${identifier})*)?$"
if ! printf '%s\n' "${version}" | grep -Eq "${pattern}"; then
  echo "VERSION must be one SemVer line like 1.0.0 or 1.0.0-rc.1, got: ${version}" >&2
  exit 1
fi

tag=""
if [[ -n "${CI_COMMIT_TAG:-}" ]]; then
  tag="${CI_COMMIT_TAG}"
elif [[ "${GITHUB_REF_TYPE:-}" == "tag" && -n "${GITHUB_REF_NAME:-}" ]]; then
  tag="${GITHUB_REF_NAME}"
fi

if [[ -n "${tag}" ]]; then
  expected="v${version}"
  if [[ "${tag}" != "${expected}" ]]; then
    echo "Git tag must match VERSION: expected ${expected}, got ${tag}" >&2
    exit 1
  fi
fi

printf '%s\n' "${version}"
