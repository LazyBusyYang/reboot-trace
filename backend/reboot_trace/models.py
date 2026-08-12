from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProcessSample:
    pid: int
    create_time_ms: int
    ppid: int | None
    uid: int
    username: str | None
    comm: str
    state: str | None
    threads: int | None
    cpu_percent: float | None
    rss_bytes: int | None
    swap_bytes: int | None
    read_bps: float | None
    write_bps: float | None
    read_bytes: int | None
    write_bytes: int | None
    cmdline_redacted: str
    cmdline_hash: str
    truncated: bool
    redaction_status: str
    ranks: dict[str, tuple[int, float]] = field(default_factory=dict)


@dataclass(slots=True)
class SnapshotData:
    boot_id: str
    captured_at_ms: int
    scheduled_at_ms: int
    duration_ms: int
    sample_interval_ms: int | None
    system: dict[str, Any]
    processes: list[ProcessSample]
    users: list[dict[str, Any]]
    events: list[dict[str, Any]] = field(default_factory=list)
