from __future__ import annotations

import os
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

    @classmethod
    def from_env(cls) -> "Settings":
        passwd = os.getenv("RT_HOST_PASSWD", "/host/etc/passwd")
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
        return cls(
            data_dir=Path(os.getenv("RT_DATA_DIR", "/var/lib/reboot-trace")),
            proc_root=Path(os.getenv("RT_PROC_ROOT", "/host/proc")),
            host_passwd=Path(passwd) if passwd else None,
            storage_limit_bytes=_int("RT_STORAGE_LIMIT_MIB", 50) * 1024 * 1024,
            sample_interval_ms=_int("RT_SAMPLE_INTERVAL_SECONDS", 5) * 1000,
            process_top_n=_int("RT_PROCESS_TOP_N", 50, maximum=500),
            cmdline_max_bytes=max_cmd,
            cors_origins=origins,
            host_id_override=os.getenv("RT_HOST_ID"),
        )
