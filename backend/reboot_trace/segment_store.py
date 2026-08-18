from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import shutil
import time
from pathlib import Path
from typing import Iterable


FORMAT_VERSION = 1
OVERLAP_MS = 10 * 60 * 1000
MIN_OVERLAP_SNAPSHOTS = 12
MAX_SEALED_SEGMENTS = 8
INTERNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  format_version INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','sealed')),
  created_at_ms INTEGER NOT NULL,
  sealed_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS storage_counter(
  name TEXT PRIMARY KEY,
  next_id INTEGER NOT NULL CHECK(next_id>=1)
);
"""

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "host": ("id",),
    "lifecycle": ("id",),
    "service_session": ("id",),
    "snapshot": ("id",),
    "system_sample": ("snapshot_id",),
    "process_sample": ("snapshot_id", "pid", "create_time_ms"),
    "process_rank": ("snapshot_id", "pid", "create_time_ms", "dimension"),
    "user_sample": ("snapshot_id", "uid"),
    "event": ("id",),
}


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def database_ok(connection: sqlite3.Connection, *, full: bool = False) -> tuple[bool, str | None]:
    pragma = "integrity_check" if full else "quick_check"
    try:
        rows = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}").fetchall()]
        if rows != ["ok"]:
            return False, "; ".join(rows[:8])
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(2)
        if violations:
            return False, "foreign key violations detected"
    except sqlite3.DatabaseError as exc:
        return False, str(exc)
    return True, None


class SegmentStore:
    def __init__(self, data_dir: Path, storage_limit_bytes: int, target_bytes: int):
        self.data_dir = data_dir
        self.active_path = data_dir / "reboot-trace.sqlite3"
        self.segments_dir = data_dir / "segments"
        self.quarantine_dir = data_dir / "quarantine"
        self.manifest_path = data_dir / "segments.json"
        self.storage_limit_bytes = storage_limit_bytes
        self.target_bytes = target_bytes
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._quarantine_interrupted_files()

    def _quarantine_interrupted_files(self) -> None:
        candidates = [self.active_path.with_suffix(".sqlite3.next")]
        candidates.extend(self.segments_dir.glob("*.tmp"))
        candidates.append(self.manifest_path.with_suffix(".json.tmp"))
        for path in candidates:
            if path.is_file():
                self._quarantine(path, "interrupted")

    @staticmethod
    def is_managed_database(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_meta'"
                ).fetchone() is not None
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False

    def require_migrated_or_empty(self) -> None:
        if not self.active_path.exists() or self.active_path.stat().st_size == 0:
            return
        if not self.is_managed_database(self.active_path):
            connection = sqlite3.connect(f"file:{self.active_path}?mode=ro", uri=True)
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            finally:
                connection.close()
            # Preserve the existing v1/v2 schema-upgrade contract. A current v3
            # single-file database must use the explicit offline layout migration.
            if version < 3:
                return
            raise RuntimeError(
                "legacy reboot-trace.sqlite3 requires offline migration; run "
                "python -m reboot_trace.storage_migrate --data-dir PATH --backup-dir PATH"
            )

    def initialize_active(self, connection: sqlite3.Connection, *, generation: int = 1) -> None:
        now = int(time.time() * 1000)
        connection.executescript(INTERNAL_SCHEMA)
        row = connection.execute("SELECT generation FROM storage_meta WHERE singleton=1").fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO storage_meta VALUES(1,?,?, 'active',?,NULL)",
                (FORMAT_VERSION, generation, now),
            )
        for table in ("lifecycle", "service_session", "snapshot", "event"):
            next_id = connection.execute(f"SELECT COALESCE(max(id),0)+1 FROM {table}").fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO storage_counter(name,next_id) VALUES(?,?)",
                (table, next_id),
            )
        connection.commit()
        active_generation = int(connection.execute(
            "SELECT generation FROM storage_meta WHERE singleton=1"
        ).fetchone()[0])
        for path in self.segment_paths(validate=False):
            metadata = self._metadata(path)
            if metadata and metadata[0] >= active_generation:
                self._quarantine(path, "uncommitted-generation")
        self.rebuild_manifest(active_connection=connection)

    def _metadata(self, path: Path) -> tuple[int, str, int, int | None] | None:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                row = connection.execute(
                    "SELECT generation,state,created_at_ms,sealed_at_ms FROM storage_meta WHERE singleton=1"
                ).fetchone()
                if not row:
                    return None
                return int(row[0]), str(row[1]), int(row[2]), int(row[3]) if row[3] is not None else None
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return None

    def segment_paths(self, *, validate: bool = True) -> list[Path]:
        healthy: list[tuple[int, Path]] = []
        for path in self.segments_dir.glob("segment-*.sqlite3"):
            metadata = self._metadata(path)
            if metadata is None:
                if validate:
                    self._quarantine(path, "metadata")
                continue
            generation, state, _, _ = metadata
            if state != "sealed":
                if validate:
                    self._quarantine(path, "state")
                continue
            if validate:
                connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
                try:
                    ok, _ = database_ok(connection)
                finally:
                    connection.close()
                if not ok:
                    self._quarantine(path, "integrity")
                    continue
            healthy.append((generation, path))
        return [path for _, path in sorted(healthy)]

    def _quarantine(self, path: Path, reason: str) -> None:
        target = self.quarantine_dir / f"{path.name}.{reason}.{int(time.time() * 1000)}"
        try:
            os.replace(path, target)
        except OSError:
            return
        _fsync_directory(self.quarantine_dir)

    def _manifest(self, active_connection: sqlite3.Connection | None = None) -> dict[str, object]:
        active_meta = None
        if active_connection is not None:
            active_meta = active_connection.execute(
                "SELECT generation,created_at_ms FROM storage_meta WHERE singleton=1"
            ).fetchone()
        elif self.active_path.exists():
            active_meta = self._metadata(self.active_path)
        generation = int(active_meta[0]) if active_meta else 1
        created_at_ms = int(active_meta[1] if active_connection is not None else active_meta[2]) if active_meta else 0
        segments = []
        for path in self.segment_paths(validate=False):
            meta = self._metadata(path)
            if meta is None:
                continue
            segments.append({
                "file": str(path.relative_to(self.data_dir)),
                "generation": meta[0],
                "created_at_ms": meta[2],
                "sealed_at_ms": meta[3],
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        return {
            "format_version": FORMAT_VERSION,
            "active": "reboot-trace.sqlite3",
            "active_generation": generation,
            "active_created_at_ms": created_at_ms,
            "segments": segments,
        }

    def rebuild_manifest(self, active_connection: sqlite3.Connection | None = None) -> None:
        payload = self._manifest(active_connection)
        temporary = self.manifest_path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.manifest_path)
        _fsync_directory(self.data_dir)

    def managed_bytes(self) -> int:
        total = sum(path.stat().st_size for path in self.data_dir.iterdir() if path.is_file())
        for directory in (self.segments_dir, self.quarantine_dir):
            total += sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
        return total

    def prepare_rotation(self, active_size: int) -> list[Path]:
        reserve = 2 * 1024 * 1024
        required_headroom = active_size * 2 + reserve
        removed: list[Path] = []
        for path in self.segment_paths(validate=False):
            if self.managed_bytes() + required_headroom <= self.storage_limit_bytes:
                break
            path.unlink()
            removed.append(path)
        if self.managed_bytes() + required_headroom > self.storage_limit_bytes:
            raise RuntimeError("insufficient managed storage headroom for safe segment rotation")
        if shutil.disk_usage(self.data_dir).free < required_headroom:
            raise RuntimeError("insufficient filesystem space for safe segment rotation")
        if removed:
            self.rebuild_manifest()
            _fsync_directory(self.segments_dir)
        return removed

    def needs_rotation(self) -> bool:
        journal = Path(str(self.active_path) + "-journal")
        return self.active_path.stat().st_size + (journal.stat().st_size if journal.exists() else 0) >= self.target_bytes

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, schema: str, table: str) -> list[str]:
        return [str(row[1]) for row in connection.execute(f"PRAGMA {schema}.table_info('{table}')")]

    def read_connection(self, timeout_seconds: float = 10.0) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:", timeout=2, check_same_thread=False, uri=True)
        connection.row_factory = sqlite3.Row
        sources = [("active", f"{self.active_path.as_uri()}?mode=ro", 1_000_000_000)]
        for index, path in enumerate(reversed(self.segment_paths()), 1):
            sources.append((f"seg{index}", f"{path.as_uri()}?mode=ro&immutable=1", 1_000_000_000 - index))
        for alias, uri, _ in sources:
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
        for table, keys in TABLE_KEYS.items():
            columns = self._table_columns(connection, "active", table)
            quoted = ",".join(f'"{column}"' for column in columns)
            unions = []
            for alias, _, priority in sources:
                unions.append(f"SELECT {quoted},{priority} AS _priority FROM {alias}.\"{table}\"")
            partition = ",".join(f'"{key}"' for key in keys)
            connection.execute(
                f'''CREATE TEMP VIEW "{table}" AS
                    SELECT {quoted} FROM (
                      SELECT {quoted},row_number() OVER(PARTITION BY {partition} ORDER BY _priority DESC) AS _rn
                      FROM ({' UNION ALL '.join(unions)})
                    ) WHERE _rn=1'''
            )
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + timeout_seconds
        connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        return connection

    def next_generation(self, active_connection: sqlite3.Connection) -> int:
        current = active_connection.execute("SELECT generation FROM storage_meta WHERE singleton=1").fetchone()
        observed = [int(current[0]) if current else 0]
        observed.extend(meta[0] for path in self.segment_paths(validate=False) if (meta := self._metadata(path)))
        return max(observed, default=0) + 1

    def prune(self) -> list[Path]:
        removed: list[Path] = []
        paths = self.segment_paths(validate=False)
        if self.managed_bytes() < int(self.storage_limit_bytes * 0.85) and len(paths) <= MAX_SEALED_SEGMENTS:
            return removed
        target = int(self.storage_limit_bytes * 0.75)
        for path in paths:
            if self.managed_bytes() <= target and len(paths) - len(removed) <= MAX_SEALED_SEGMENTS:
                break
            path.unlink()
            removed.append(path)
        if removed:
            self.rebuild_manifest()
            _fsync_directory(self.segments_dir)
        return removed


def copy_rows(
    target: sqlite3.Connection,
    source_schema: str,
    table: str,
    *,
    where: str = "1",
    params: Iterable[object] = (),
) -> None:
    columns = [str(row[1]) for row in target.execute(f"PRAGMA table_info('{table}')")]
    quoted = ",".join(f'"{column}"' for column in columns)
    target.execute(
        f'INSERT OR REPLACE INTO "{table}"({quoted}) SELECT {quoted} FROM {source_schema}."{table}" WHERE {where}',
        tuple(params),
    )


def copy_events(
    target: sqlite3.Connection,
    source_schema: str,
    retained_snapshot_ids: Iterable[int],
) -> None:
    retained = tuple(retained_snapshot_ids)
    if retained:
        placeholders = ",".join("?" for _ in retained)
        snapshot_expression = f"CASE WHEN snapshot_id IN ({placeholders}) THEN snapshot_id ELSE NULL END"
    else:
        snapshot_expression = "NULL"
    target.execute(
        f'''INSERT OR REPLACE INTO event(id,lifecycle_id,snapshot_id,occurred_at_ms,type,details_json)
            SELECT id,lifecycle_id,{snapshot_expression},occurred_at_ms,type,details_json
            FROM {source_schema}.event''',
        retained,
    )
