from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


EXIT_NOT_RUNNING = 3
EXIT_IDENTITY_MISMATCH = 4
EXIT_TIMEOUT = 5
MODULE = "reboot_trace.main:app"


class ProcessControlError(RuntimeError):
    exit_code = EXIT_IDENTITY_MISMATCH


class ProcessNotRunning(ProcessControlError):
    exit_code = EXIT_NOT_RUNNING


class ProcessIdentityMismatch(ProcessControlError):
    exit_code = EXIT_IDENTITY_MISMATCH


class ProcessStopTimeout(ProcessControlError):
    exit_code = EXIT_TIMEOUT


@dataclass(frozen=True, slots=True)
class ProcessState:
    version: int
    pid: int
    start_ticks: int
    service_token: str
    data_dir: str
    marker_path: str
    port: int


def _normalized(path: str | Path) -> str:
    return str(Path(path).resolve())


def _proc_file(proc_root: Path, pid: int, name: str) -> Path:
    return proc_root / str(pid) / name


def _start_ticks(proc_root: Path, pid: int) -> int:
    try:
        raw = _proc_file(proc_root, pid, "stat").read_text()
    except FileNotFoundError as exc:
        raise ProcessNotRunning(f"process {pid} is not running") from exc
    except OSError as exc:
        raise ProcessIdentityMismatch(f"cannot read process {pid} stat: {exc}") from exc
    close_paren = raw.rfind(")")
    fields = raw[close_paren + 2 :].split() if close_paren >= 0 else []
    if len(fields) <= 19:
        raise ProcessIdentityMismatch(f"process {pid} has an invalid stat record")
    try:
        return int(fields[19])
    except ValueError as exc:
        raise ProcessIdentityMismatch(f"process {pid} has an invalid start time") from exc


def _nul_values(path: Path) -> list[str]:
    try:
        return [value.decode("utf-8", "replace") for value in path.read_bytes().split(b"\0") if value]
    except FileNotFoundError as exc:
        raise ProcessNotRunning(f"process {path.parent.name} is not running") from exc
    except OSError as exc:
        raise ProcessIdentityMismatch(f"cannot read {path}: {exc}") from exc


def _environment(proc_root: Path, pid: int) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in _nul_values(_proc_file(proc_root, pid, "environ")):
        key, separator, value = item.partition("=")
        if separator:
            values[key] = value
    return values


def _argument_after(arguments: list[str], name: str) -> str | None:
    try:
        index = arguments.index(name)
    except ValueError:
        return None
    return arguments[index + 1] if index + 1 < len(arguments) else None


def _validate_live(state: ProcessState, proc_root: Path = Path("/proc")) -> None:
    observed_ticks = _start_ticks(proc_root, state.pid)
    if observed_ticks != state.start_ticks:
        raise ProcessIdentityMismatch(
            f"process {state.pid} start ticks changed: expected {state.start_ticks}, got {observed_ticks}"
        )
    arguments = _nul_values(_proc_file(proc_root, state.pid, "cmdline"))
    if MODULE not in arguments:
        raise ProcessIdentityMismatch(f"process {state.pid} does not run {MODULE}")
    if _argument_after(arguments, "--port") != str(state.port):
        raise ProcessIdentityMismatch(f"process {state.pid} does not use port {state.port}")
    environment = _environment(proc_root, state.pid)
    expected = {
        "RT_DATA_DIR": state.data_dir,
        "RT_INSTANCE_MARKER_PATH": state.marker_path,
        "RT_SERVICE_TOKEN": state.service_token,
        "RT_IDENTITY_SCOPE": "local_container",
        "RT_REQUIRE_CONTAINER_MARKER": "true",
    }
    for name, value in expected.items():
        observed = environment.get(name)
        if name in {"RT_DATA_DIR", "RT_INSTANCE_MARKER_PATH"} and observed is not None:
            observed = _normalized(observed)
        if observed != value:
            raise ProcessIdentityMismatch(f"process {state.pid} has unexpected {name}")


def _write_state(path: Path, state: ProcessState) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(asdict(state), handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_state(path: Path) -> ProcessState:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ProcessIdentityMismatch(f"process state is not a regular file: {path}")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ProcessIdentityMismatch(f"process state owner or permissions are unsafe: {path}")
        value = json.loads(path.read_text())
        state = ProcessState(**value)
    except FileNotFoundError as exc:
        raise ProcessNotRunning(f"process state file is absent: {path}") from exc
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ProcessIdentityMismatch(f"invalid process state file {path}: {exc}") from exc
    if (
        state.version != 1
        or state.pid <= 0
        or state.start_ticks <= 0
        or not isinstance(state.service_token, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", state.service_token)
    ):
        raise ProcessIdentityMismatch(f"unsupported process state in {path}")
    return state


def _expected_state(
    state: ProcessState,
    data_dir: str,
    marker_path: str,
    port: int,
) -> None:
    expected = (_normalized(data_dir), _normalized(marker_path), port)
    observed = (state.data_dir, state.marker_path, state.port)
    if observed != expected:
        raise ProcessIdentityMismatch("process state belongs to a different instance configuration")


def capture(
    path: Path,
    pid: int,
    service_token: str,
    data_dir: str,
    marker_path: str,
    port: int,
    proc_root: Path = Path("/proc"),
    timeout: float = 3.0,
) -> ProcessState:
    state = ProcessState(1, pid, _start_ticks(proc_root, pid), service_token, _normalized(data_dir), _normalized(marker_path), port)
    deadline = time.monotonic() + timeout
    last_error: ProcessControlError | None = None
    while time.monotonic() < deadline:
        try:
            _validate_live(state, proc_root)
            _write_state(path, state)
            return state
        except ProcessNotRunning:
            raise
        except ProcessIdentityMismatch as exc:
            last_error = exc
            time.sleep(0.05)
    try:
        if _start_ticks(proc_root, pid) == state.start_ticks:
            os.kill(pid, signal.SIGTERM)
    except (ProcessNotRunning, ProcessLookupError):
        pass
    raise ProcessIdentityMismatch(f"new process did not establish the expected identity: {last_error}")


def validate(
    path: Path,
    data_dir: str,
    marker_path: str,
    port: int,
    proc_root: Path = Path("/proc"),
) -> ProcessState:
    state = _load_state(path)
    _expected_state(state, data_dir, marker_path, port)
    _validate_live(state, proc_root)
    return state


def terminate(
    path: Path,
    data_dir: str,
    marker_path: str,
    port: int,
    proc_root: Path = Path("/proc"),
    timeout: float = 10.0,
) -> ProcessState:
    state = _load_state(path)
    _expected_state(state, data_dir, marker_path, port)
    _validate_live(state, proc_root)
    # Validate start ticks and the full instance identity immediately before
    # signalling. The deployment Python lacks pidfd APIs, so this is the
    # strongest portable fail-closed contract available in the target runtime.
    _validate_live(state, proc_root)
    try:
        os.kill(state.pid, signal.SIGTERM)
    except ProcessLookupError as exc:
        raise ProcessNotRunning(f"process {state.pid} is not running") from exc
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            observed=_start_ticks(proc_root,state.pid)
        except ProcessNotRunning:
            return state
        if observed != state.start_ticks:
            return state
        time.sleep(0.05)
    raise ProcessStopTimeout(f"process {state.pid} did not stop within {timeout:g} seconds")


def _validate_legacy_live(pid: int, data_dir: str, port: int, proc_root: Path = Path("/proc")) -> int:
    start_ticks = _start_ticks(proc_root, pid)
    arguments = _nul_values(_proc_file(proc_root, pid, "cmdline"))
    if MODULE not in arguments or _argument_after(arguments, "--port") != str(port):
        raise ProcessIdentityMismatch(f"legacy process {pid} command does not match this instance")
    environment = _environment(proc_root, pid)
    observed_data = environment.get("RT_DATA_DIR")
    if observed_data is None or _normalized(observed_data) != _normalized(data_dir):
        raise ProcessIdentityMismatch(f"legacy process {pid} data directory does not match this instance")
    return start_ticks


def validate_legacy(pid: int, data_dir: str, port: int, proc_root: Path = Path("/proc")) -> int:
    _validate_legacy_live(pid, data_dir, port, proc_root)
    return pid


def terminate_legacy(
    pid: int,
    data_dir: str,
    port: int,
    proc_root: Path = Path("/proc"),
    timeout: float = 10.0,
) -> int:
    start_ticks = _validate_legacy_live(pid, data_dir, port, proc_root)
    observed_ticks = _validate_legacy_live(pid, data_dir, port, proc_root)
    if observed_ticks != start_ticks:
        raise ProcessIdentityMismatch(f"legacy process {pid} start ticks changed before SIGTERM")
    try:
        os.kill(pid,signal.SIGTERM)
    except ProcessLookupError as exc:
        raise ProcessNotRunning(f"process {pid} is not running") from exc
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            observed=_start_ticks(proc_root,pid)
        except ProcessNotRunning:
            return pid
        if observed != start_ticks:
            return pid
        time.sleep(0.05)
    raise ProcessStopTimeout(f"legacy process {pid} did not stop within {timeout:g} seconds")


def validate_http(
    path: Path,
    data_dir: str,
    marker_path: str,
    port: int,
    proc_root: Path = Path("/proc"),
    attempts: int = 20,
) -> dict[str, Any]:
    state = validate(path, data_dir, marker_path, port, proc_root)
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/status", timeout=2) as response:
                response_token = response.headers.get("X-Reboot-Trace-Service-Token")
                value = json.load(response)
            if response_token != state.service_token:
                raise ProcessIdentityMismatch("status response belongs to a different service process")
            checks = {
                "schema_version": value.get("schema_version") == 3,
                "identity_scope": value.get("identity", {}).get("identity_scope") == "local_container",
                "namespace_state": value.get("identity", {}).get("namespace_state") == "supported",
                "marker_storage": value.get("identity", {}).get("marker_storage") == "container_ephemeral_rootfs",
                "container_identity": value.get("capabilities", {}).get("container_identity", {}).get("state") == "supported",
                "persistence_capability": value.get("storage", {}).get("persistence_capability") == "operator_verification_required",
                "data_path": value.get("storage", {}).get("data_path") == state.data_dir,
            }
            failed = [name for name, passed in checks.items() if not passed]
            if failed:
                raise RuntimeError("status validation failed: " + ", ".join(failed))
            validate(path, data_dir, marker_path, port, proc_root)
            return {
                "host_id": value["host_id"],
                "lifecycle_key": value.get("lifecycle_key"),
                "storage": value["storage"],
                "identity": value.get("identity"),
            }
        except ProcessControlError:
            raise
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise ProcessIdentityMismatch(f"backend status did not pass validation: {last_error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reboot Trace instance process ownership control")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "validate", "terminate", "validate-http"):
        command = subparsers.add_parser(name)
        command.add_argument("--pid-file", required=True, type=Path)
        command.add_argument("--data-dir", required=True)
        command.add_argument("--marker-path", required=True)
        command.add_argument("--port", required=True, type=int)
        command.add_argument("--proc-root", type=Path, default=Path("/proc"))
        if name == "capture":
            command.add_argument("--pid", required=True, type=int)
            command.add_argument("--service-token", required=True)
        if name == "terminate":
            command.add_argument("--timeout", type=float, default=10.0)
    for name in ("validate-legacy", "terminate-legacy"):
        command = subparsers.add_parser(name)
        command.add_argument("--pid", required=True, type=int)
        command.add_argument("--data-dir", required=True)
        command.add_argument("--port", required=True, type=int)
        command.add_argument("--proc-root", type=Path, default=Path("/proc"))
        if name == "terminate-legacy":
            command.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "capture":
            state = capture(
                arguments.pid_file,
                arguments.pid,
                arguments.service_token,
                arguments.data_dir,
                arguments.marker_path,
                arguments.port,
                arguments.proc_root,
            )
            print(state.pid)
        elif arguments.command == "validate":
            print(validate(arguments.pid_file, arguments.data_dir, arguments.marker_path, arguments.port, arguments.proc_root).pid)
        elif arguments.command == "terminate":
            print(terminate(arguments.pid_file, arguments.data_dir, arguments.marker_path, arguments.port, arguments.proc_root, arguments.timeout).pid)
        elif arguments.command == "validate-http":
            value = validate_http(arguments.pid_file, arguments.data_dir, arguments.marker_path, arguments.port, arguments.proc_root)
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        elif arguments.command == "validate-legacy":
            print(validate_legacy(arguments.pid, arguments.data_dir, arguments.port, arguments.proc_root))
        else:
            print(terminate_legacy(arguments.pid, arguments.data_dir, arguments.port, arguments.proc_root, arguments.timeout))
        return 0
    except ProcessControlError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
