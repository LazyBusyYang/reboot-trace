#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <1|4|5|9> <start|stop|restart|status|run>" >&2
  exit 2
fi

instance=$1
action=$2
case "$instance" in 1|4|5|9) ;; *) echo "unsupported instance: $instance" >&2; exit 2 ;; esac
case "$action" in start|stop|restart|status|run) ;; *) echo "unsupported action: $action" >&2; exit 2 ;; esac

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
runtime_root=${REBOOT_TRACE_RUNTIME_ROOT:-$project_root/runtime}
instance_root=$runtime_root/dev-$instance
data_dir=$instance_root/data
log_dir=$instance_root/logs
run_dir=$instance_root/run
pid_file=$run_dir/backend.pid
lock_dir=$run_dir/control.lock
log_file=$log_dir/backend.log
bound_hostname_file=$instance_root/bound-hostname
marker_dir=/tmp/reboot-trace-$UID
marker_path=$marker_dir/container-instance-id
python_bin=/mnt/aigc/gaoyang3/Code/github/tunnel-preview/.venv/bin/python3
uvicorn_bin=/mnt/aigc/gaoyang3/Code/github/tunnel-preview/.venv/bin/uvicorn
port=${REBOOT_TRACE_PORT:-31088}
frontend_origin=${REBOOT_TRACE_FRONTEND_ORIGIN:-https://maoshanwang-reboot-trace.zoedev.top}
storage_limit_mib=${RT_STORAGE_LIMIT_MIB:-50}
lock_held=false

umask 077
mkdir -p "$data_dir" "$log_dir" "$run_dir"
chmod 700 "$instance_root" "$data_dir" "$log_dir" "$run_dir"

acquire_lock() {
  if $lock_held; then return; fi
  if ! mkdir "$lock_dir" 2>/dev/null; then
    echo "instance dev-$instance is already being managed; lock: $lock_dir" >&2
    exit 1
  fi
  lock_held=true
  trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT
}

bind_hostname() {
  local current saved
  current=$(hostname)
  if [[ -f "$bound_hostname_file" ]]; then
    saved=$(<"$bound_hostname_file")
    if [[ "$saved" != "$current" ]]; then
      echo "dev-$instance data is bound to host '$saved', refusing host '$current'" >&2
      echo "verify the target instance; do not copy or reuse another host's data directory" >&2
      exit 1
    fi
  else
    printf '%s\n' "$current" > "$bound_hostname_file"
    chmod 600 "$bound_hostname_file"
  fi
}

process_control() {
  PYTHONPATH="$project_root/backend" "$python_bin" -m reboot_trace.process_control "$@"
}

process_args=(--pid-file "$pid_file" --data-dir "$data_dir" --marker-path "$marker_path" --port "$port")

legacy_pid() {
  [[ -s "$pid_file" ]] || return 1
  local value
  value=$(<"$pid_file")
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$value"
}

managed_pid() {
  process_control validate "${process_args[@]}"
}

prepare_marker_dir() {
  if [[ -L "$marker_dir" ]]; then
    echo "marker directory must not be a symlink: $marker_dir" >&2
    exit 1
  fi
  mkdir -p "$marker_dir"
  [[ -d "$marker_dir" && ! -L "$marker_dir" ]] || { echo "invalid marker directory: $marker_dir" >&2; exit 1; }
  [[ $(stat -c '%u' "$marker_dir") == "$UID" ]] || { echo "marker directory is not owned by uid $UID: $marker_dir" >&2; exit 1; }
  chmod 700 "$marker_dir"
  [[ $(stat -c '%a' "$marker_dir") == 700 ]] || { echo "marker directory permissions must be 0700: $marker_dir" >&2; exit 1; }
  [[ -w "$marker_dir" ]] || { echo "marker directory is not writable: $marker_dir" >&2; exit 1; }
}

check_prerequisites() {
  [[ -x "$python_bin" ]] || { echo "shared Python not found: $python_bin" >&2; exit 1; }
  [[ -x "$uvicorn_bin" ]] || { echo "shared uvicorn not found: $uvicorn_bin" >&2; exit 1; }
  "$python_bin" -c 'import fastapi, pydantic, uvicorn' >/dev/null
  [[ "$frontend_origin" =~ ^https?://[^/]+$ ]] || { echo "REBOOT_TRACE_FRONTEND_ORIGIN must be an Origin without path or trailing slash" >&2; exit 1; }
  [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) || { echo "invalid port: $port" >&2; exit 1; }
  [[ "$storage_limit_mib" =~ ^[0-9]+$ ]] && ((storage_limit_mib >= 3)) || { echo "RT_STORAGE_LIMIT_MIB must be at least 3" >&2; exit 1; }
  prepare_marker_dir
  PYTHONPATH="$project_root/backend" "$python_bin" - "$data_dir" "$marker_path" "$storage_limit_mib" <<'PY'
import os
import sys
from pathlib import Path
from reboot_trace.identity import _mount_identity, namespace_evidence

data = Path(sys.argv[1]).resolve()
marker = Path(sys.argv[2])
limit = int(sys.argv[3]) * 1024 * 1024
proc = Path("/proc")
if not data.is_absolute() or not data.is_dir() or not os.access(data, os.R_OK | os.W_OK | os.X_OK):
    raise SystemExit(f"RT_DATA_DIR is not a readable and writable absolute directory: {data}")
namespace = namespace_evidence(proc)
if not namespace.aligned:
    raise SystemExit(namespace.reason or "cannot verify local container namespaces")
data_mount = _mount_identity(proc, data)
marker_mount = _mount_identity(proc, marker)
if data_mount is None:
    raise SystemExit("RT_DATA_DIR is absent from PID 1 mountinfo")
if data_mount.mountpoint == "/" and data_mount.fs_type in {"overlay", "fuse-overlayfs"}:
    raise SystemExit("RT_DATA_DIR is on the container ephemeral root filesystem")
if marker_mount is None or marker_mount.mountpoint != "/" or marker_mount.fs_type not in {"overlay", "fuse-overlayfs"}:
    raise SystemExit("marker path is not on the PID 1 container root overlay")
if (data_mount.mountpoint, data_mount.fs_type, data_mount.source) == (marker_mount.mountpoint, marker_mount.fs_type, marker_mount.source):
    raise SystemExit("RT_DATA_DIR and marker path use the same mount")
managed = sum(entry.stat().st_size for entry in data.rglob("*") if entry.is_file())
if managed + 2 * 1024 * 1024 > limit:
    raise SystemExit(f"managed data leaves less than 2 MiB migration reserve: {managed} bytes")
PY
}

validate_status() {
  process_control validate-http "${process_args[@]}"
}

start_backend() {
  acquire_lock
  bind_hostname
  check_prerequisites
  if [[ -s "$pid_file" ]]; then
    local existing
    if existing=$(legacy_pid); then
      if process_control validate-legacy --pid "$existing" --data-dir "$data_dir" --port "$port" >/dev/null; then
        echo "dev-$instance legacy backend is still running (pid $existing); stop it before upgrading" >&2
        exit 1
      else
        local legacy_state=$?
        if [[ $legacy_state -eq 3 ]]; then
          rm -f "$pid_file"
        else
          echo "dev-$instance legacy PID identity is not trustworthy; refusing to overwrite $pid_file" >&2
          exit 1
        fi
      fi
    elif existing=$(managed_pid); then
      echo "dev-$instance backend is already running (pid $existing)"
      validate_status
      return
    else
      local state=$?
      if [[ $state -eq 3 ]]; then
        rm -f "$pid_file"
      else
        echo "dev-$instance process state is not trustworthy; refusing to overwrite $pid_file" >&2
        exit 1
      fi
    fi
  fi
  printf '\n[%s] starting dev-%s on %s:%s\n' "$(date --iso-8601=seconds)" "$instance" "$(hostname)" "$port" >> "$log_file"
  local service_token
  service_token=$("$python_bin" -c 'import secrets; print(secrets.token_urlsafe(32))')
  nohup env \
    PYTHONPATH="$project_root/backend" \
    RT_DATA_DIR="$data_dir" \
    RT_PROC_ROOT=/proc \
    RT_HOST_PASSWD=/etc/passwd \
    RT_STORAGE_LIMIT_MIB="$storage_limit_mib" \
    RT_SAMPLE_INTERVAL_SECONDS="${RT_SAMPLE_INTERVAL_SECONDS:-5}" \
    RT_PROCESS_TOP_N="${RT_PROCESS_TOP_N:-50}" \
    RT_CORS_ORIGINS="$frontend_origin" \
    RT_IDENTITY_SCOPE=local_container \
    RT_INSTANCE_MARKER_PATH="$marker_path" \
    RT_REQUIRE_CONTAINER_MARKER=true \
    RT_SERVICE_TOKEN="$service_token" \
    "$uvicorn_bin" reboot_trace.main:app --host 0.0.0.0 --port "$port" \
    >> "$log_file" 2>&1 &
  local pid=$!
  if ! process_control capture "${process_args[@]}" --pid "$pid" --service-token "$service_token" >/dev/null; then
    rm -f "$pid_file"
    echo "dev-$instance backend failed to establish process identity; inspect $log_file" >&2
    exit 1
  fi
  if ! validate_status; then
    if process_control terminate "${process_args[@]}" >/dev/null; then
      rm -f "$pid_file"
    else
      echo "backend validation failed and safe termination could not be confirmed; preserving $pid_file" >&2
    fi
    echo "dev-$instance backend failed post-start validation; inspect $log_file" >&2
    exit 1
  fi
  echo "dev-$instance backend started (pid $pid, port $port)"
  echo "data:   $data_dir"
  echo "marker: $marker_path"
  echo "log:    $log_file"
}

stop_backend() {
  acquire_lock
  [[ -x "$python_bin" ]] || { echo "shared Python not found: $python_bin" >&2; exit 1; }
  if [[ ! -s "$pid_file" ]]; then
    echo "dev-$instance backend is not running"
    return
  fi
  local pid
  if pid=$(legacy_pid); then
    if process_control terminate-legacy --pid "$pid" --data-dir "$data_dir" --port "$port" >/dev/null; then
      rm -f "$pid_file"
      echo "dev-$instance legacy backend stopped"
      return
    else
      local legacy_state=$?
      if [[ $legacy_state -eq 3 ]]; then
        rm -f "$pid_file"
        echo "dev-$instance legacy backend is not running"
        return
      fi
    fi
    echo "dev-$instance legacy PID identity is not trustworthy; refusing to send SIGTERM" >&2
    exit 1
  fi
  if pid=$(managed_pid); then
    :
  else
    local state=$?
    if [[ $state -eq 3 ]]; then
      rm -f "$pid_file"
      echo "dev-$instance backend is not running"
      return
    fi
    echo "dev-$instance process identity is not trustworthy; refusing to send SIGTERM and preserving $pid_file" >&2
    exit 1
  fi
  if process_control terminate "${process_args[@]}" >/dev/null; then
    rm -f "$pid_file"
    echo "dev-$instance backend stopped"
    return
  else
    local state=$?
    if [[ $state -eq 3 ]]; then
      rm -f "$pid_file"
      echo "dev-$instance backend stopped"
      return
    fi
  fi
  echo "dev-$instance backend could not be safely stopped; preserving $pid_file" >&2
  exit 1
}

status_backend() {
  [[ -x "$python_bin" ]] || { echo "shared Python not found: $python_bin" >&2; exit 1; }
  if [[ ! -s "$pid_file" ]]; then
    echo "dev-$instance backend is not running"
    exit 1
  fi
  local pid
  if pid=$(legacy_pid); then
    echo "dev-$instance uses a legacy PID file; stop and upgrade it before status validation" >&2
    exit 1
  fi
  if pid=$(managed_pid); then
    :
  else
    echo "dev-$instance process identity validation failed; preserving $pid_file" >&2
    exit 1
  fi
  echo "dev-$instance backend is running (pid $pid, port $port)"
  validate_status
}

run_foreground() {
  acquire_lock
  bind_hostname
  check_prerequisites
  if [[ -s "$pid_file" ]]; then echo "dev-$instance has an existing process state file; use stop before run" >&2; exit 1; fi
  export PYTHONPATH="$project_root/backend"
  export RT_DATA_DIR="$data_dir" RT_PROC_ROOT=/proc RT_HOST_PASSWD=/etc/passwd
  export RT_STORAGE_LIMIT_MIB="$storage_limit_mib"
  export RT_SAMPLE_INTERVAL_SECONDS="${RT_SAMPLE_INTERVAL_SECONDS:-5}"
  export RT_PROCESS_TOP_N="${RT_PROCESS_TOP_N:-50}"
  export RT_CORS_ORIGINS="$frontend_origin"
  export RT_IDENTITY_SCOPE=local_container
  export RT_INSTANCE_MARKER_PATH="$marker_path"
  export RT_REQUIRE_CONTAINER_MARKER=true
  export RT_SERVICE_TOKEN
  RT_SERVICE_TOKEN=$("$python_bin" -c 'import secrets; print(secrets.token_urlsafe(32))')
  rmdir "$lock_dir"
  lock_held=false
  trap - EXIT
  exec "$uvicorn_bin" reboot_trace.main:app --host 0.0.0.0 --port "$port"
}

case "$action" in
  start) start_backend ;;
  stop) stop_backend ;;
  restart) stop_backend; start_backend ;;
  status) status_backend ;;
  run) run_foreground ;;
esac
