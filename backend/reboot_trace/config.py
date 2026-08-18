from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    value = int(os.getenv(name, default))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _project_root() -> Path:
    configured = os.getenv("RT_PROJECT_DIR")
    if configured:
        return Path(configured).resolve()
    candidates = (Path.cwd(), Path(__file__).resolve())
    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists():
                return parent
    return Path.cwd().resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    proc_root: Path
    host_passwd: Path | None
    storage_limit_bytes: int
    sample_interval_ms: int
    process_top_n: int
    cmdline_max_bytes: int
    cors_origins: tuple[str, ...]
    host_id_override: str | None = None
    instance_marker_path: Path = Path("/run/reboot-trace/container-instance-id")
    identity_scope: str = "local_container"
    require_container_marker: bool = False
    service_token: str | None = None
    project_dir: Path | None = None
    segment_target_bytes: int | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        passwd = os.getenv("RT_HOST_PASSWD", "/etc/passwd")
        identity_scope = os.getenv("RT_IDENTITY_SCOPE", "local_container")
        if identity_scope != "local_container":
            raise ValueError("RT_IDENTITY_SCOPE must be local_container")
        if os.getenv("RT_INSTANCE_MARKER_TARGET_PATH"):
            raise ValueError("RT_INSTANCE_MARKER_TARGET_PATH was removed; mount validation uses RT_INSTANCE_MARKER_PATH")
        project_dir = _project_root()
        data_dir = Path(os.getenv("RT_DATA_DIR", str(project_dir / "var" / "reboot-trace"))).resolve()
        marker_text = os.getenv("RT_INSTANCE_MARKER_PATH", "/run/reboot-trace/container-instance-id")
        marker_path = Path(marker_text)
        if not marker_text.startswith("/"):
            raise ValueError("RT_INSTANCE_MARKER_PATH must be absolute")
        service_token = os.getenv("RT_SERVICE_TOKEN")
        if service_token is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", service_token):
            raise ValueError("RT_SERVICE_TOKEN must contain 16-128 URL-safe characters")
        max_cmd = _int("RT_CMDLINE_MAX_BYTES", 4096)
        if max_cmd > 4096:
            raise ValueError("RT_CMDLINE_MAX_BYTES cannot exceed 4096")
        origins = tuple(x.strip() for x in os.getenv("RT_CORS_ORIGINS", "").split(",") if x.strip())
        if "*" in origins:
            raise ValueError("wildcard CORS origin is forbidden")
        for origin in origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
                raise ValueError(f"RT_CORS_ORIGINS contains an invalid exact origin: {origin}")
        storage_limit_bytes = _int("RT_STORAGE_LIMIT_MIB", 50) * 1024 * 1024
        configured_segment_mib = os.getenv("RT_SEGMENT_TARGET_MIB")
        segment_target_bytes = (
            _int("RT_SEGMENT_TARGET_MIB", 12) * 1024 * 1024
            if configured_segment_mib is not None else None
        )
        reserve = 2 * 1024 * 1024
        maximum_segment = max(1024 * 1024, (storage_limit_bytes - reserve) // 3)
        if segment_target_bytes is not None and segment_target_bytes > maximum_segment:
            raise ValueError("RT_SEGMENT_TARGET_MIB must leave room for two rotation copies and 2 MiB reserve")
        return cls(
            data_dir=data_dir,
            proc_root=Path(os.getenv("RT_PROC_ROOT", "/proc")),
            host_passwd=Path(passwd) if passwd else None,
            storage_limit_bytes=storage_limit_bytes,
            sample_interval_ms=_int("RT_SAMPLE_INTERVAL_SECONDS", 5) * 1000,
            process_top_n=_int("RT_PROCESS_TOP_N", 50, maximum=500),
            cmdline_max_bytes=max_cmd,
            cors_origins=origins,
            host_id_override=os.getenv("RT_HOST_ID"),
            instance_marker_path=marker_path,
            identity_scope=identity_scope,
            require_container_marker=_bool("RT_REQUIRE_CONTAINER_MARKER"),
            service_token=service_token,
            project_dir=project_dir,
            segment_target_bytes=segment_target_bytes,
        )

    def effective_segment_target_bytes(self) -> int:
        if self.segment_target_bytes is not None:
            return self.segment_target_bytes
        available = max(1024 * 1024, self.storage_limit_bytes - 2 * 1024 * 1024)
        return max(1024 * 1024, min(12 * 1024 * 1024, available // 3))
