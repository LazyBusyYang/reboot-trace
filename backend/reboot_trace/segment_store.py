from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable


FORMAT_VERSION = 2
INTERNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_meta(
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  format_version INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','evidence')),
  created_at_ms INTEGER NOT NULL,
  sealed_at_ms INTEGER,
  lifecycle_id INTEGER,
  lifecycle_key TEXT
);
CREATE TABLE IF NOT EXISTS storage_counter(
  name TEXT PRIMARY KEY,
  next_id INTEGER NOT NULL CHECK(next_id>=1)
);
"""

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "host": ("id",), "lifecycle": ("id",), "service_session": ("id",),
    "snapshot": ("id",), "system_sample": ("snapshot_id",),
    "process_sample": ("snapshot_id", "pid", "create_time_ms"),
    "process_rank": ("snapshot_id", "pid", "create_time_ms", "dimension"),
    "user_sample": ("snapshot_id", "uid"), "event": ("id",),
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
        if connection.execute("PRAGMA foreign_key_check").fetchmany(2):
            return False, "foreign key violations detected"
    except sqlite3.DatabaseError as exc:
        return False, str(exc)
    return True, None


def copy_rows(target: sqlite3.Connection, source_schema: str, table: str, *, where: str = "1", params: Iterable[object] = ()) -> None:
    columns = [str(row[1]) for row in target.execute(f"PRAGMA table_info('{table}')")]
    quoted = ",".join(f'"{column}"' for column in columns)
    target.execute(
        f'INSERT OR REPLACE INTO "{table}"({quoted}) SELECT {quoted} FROM {source_schema}."{table}" WHERE {where}',
        tuple(params),
    )


def copy_events(target: sqlite3.Connection, source_schema: str, retained_snapshot_ids: Iterable[int], *, lifecycle_id: int | None = None) -> None:
    retained = tuple(retained_snapshot_ids)
    if retained:
        placeholders = ",".join("?" for _ in retained)
        snapshot_expression = f"CASE WHEN snapshot_id IN ({placeholders}) THEN snapshot_id ELSE NULL END"
    else:
        snapshot_expression = "NULL"
    where = "" if lifecycle_id is None else " WHERE lifecycle_id=?"
    params: tuple[object, ...] = retained + (() if lifecycle_id is None else (lifecycle_id,))
    target.execute(
        f'''INSERT OR REPLACE INTO event(id,lifecycle_id,snapshot_id,occurred_at_ms,type,details_json)
            SELECT id,lifecycle_id,{snapshot_expression},occurred_at_ms,type,details_json
            FROM {source_schema}.event{where}''',
        params,
    )


class SegmentStore:
    """Format-v2 active database plus immutable per-lifecycle evidence capsules."""

    def __init__(self, data_dir: Path, storage_limit_bytes: int):
        self.data_dir = data_dir
        self.active_path = data_dir / "reboot-trace.sqlite3"
        self.evidence_dir = data_dir / "evidence"
        self.quarantine_dir = data_dir / "quarantine"
        self.manifest_path = data_dir / "evidence.json"
        self.storage_limit_bytes = storage_limit_bytes
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._recover_or_quarantine_staging()

    @staticmethod
    def is_managed_database(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='storage_meta'").fetchone() is not None
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def format_version(path: Path) -> int | None:
        if not SegmentStore.is_managed_database(path):
            return None
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT format_version FROM storage_meta WHERE singleton=1").fetchone()
            return int(row[0]) if row else None
        except sqlite3.DatabaseError:
            return None
        finally:
            connection.close()

    def require_migrated_or_empty(self) -> None:
        if not self.active_path.exists() or self.active_path.stat().st_size == 0:
            return
        version = self.format_version(self.active_path)
        if version == FORMAT_VERSION:
            return
        if version == 1 or (self.data_dir / "segments.json").exists():
            raise RuntimeError(
                "format v1 segmented storage requires offline repack; run "
                "python -m reboot_trace.storage_repack --data-dir PATH --backup-dir PATH"
            )
        if version is None:
            connection = sqlite3.connect(f"file:{self.active_path}?mode=ro", uri=True)
            try:
                schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            finally:
                connection.close()
            if schema_version >= 3:
                raise RuntimeError(
                    "legacy reboot-trace.sqlite3 requires offline repack; run "
                    "python -m reboot_trace.storage_repack --data-dir PATH --backup-dir PATH"
                )

    def initialize_active(self, connection: sqlite3.Connection) -> None:
        connection.executescript(INTERNAL_SCHEMA)
        row = connection.execute("SELECT format_version,state FROM storage_meta WHERE singleton=1").fetchone()
        now = int(time.time() * 1000)
        if row is None:
            connection.execute(
                "INSERT INTO storage_meta(singleton,format_version,generation,state,created_at_ms) VALUES(1,?,?, 'active',?)",
                (FORMAT_VERSION, 1, now),
            )
        elif int(row[0]) != FORMAT_VERSION or str(row[1]) != "active":
            raise RuntimeError("active database storage metadata is incompatible")
        for table in ("lifecycle", "service_session", "snapshot", "event"):
            next_id = connection.execute(f"SELECT COALESCE(max(id),0)+1 FROM {table}").fetchone()[0]
            connection.execute("INSERT OR IGNORE INTO storage_counter VALUES(?,?)", (table, next_id))
        connection.commit()
        self.rebuild_manifest()

    @staticmethod
    def capsule_name(lifecycle_id: int, lifecycle_key: str) -> str:
        digest = hashlib.sha256(lifecycle_key.encode()).hexdigest()[:12]
        return f"lifecycle-{lifecycle_id:012d}-{digest}.sqlite3"

    def capsule_path(self, lifecycle_id: int, lifecycle_key: str) -> Path:
        return self.evidence_dir / self.capsule_name(lifecycle_id, lifecycle_key)

    def _metadata(self, path: Path) -> dict[str, object] | None:
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
            try:
                ok, _ = database_ok(connection, full=True)
                if not ok:
                    return None
                row = connection.execute(
                    "SELECT format_version,generation,state,created_at_ms,sealed_at_ms,lifecycle_id,lifecycle_key FROM storage_meta WHERE singleton=1"
                ).fetchone()
                if not row or int(row[0]) != FORMAT_VERSION or row[2] != "evidence" or row[5] is None or not row[6]:
                    return None
                lifecycle = connection.execute(
                    "SELECT lifecycle_key FROM lifecycle WHERE id=?", (int(row[5]),)
                ).fetchall()
                if len(lifecycle) != 1 or str(lifecycle[0][0]) != str(row[6]):
                    return None
                if connection.execute("SELECT count(*) FROM lifecycle").fetchone()[0] != 1:
                    return None
                if connection.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id!=?", (int(row[5]),)).fetchone()[0]:
                    return None
                return {
                    "format_version": int(row[0]), "generation": int(row[1]), "state": str(row[2]),
                    "created_at_ms": int(row[3]), "sealed_at_ms": int(row[4]),
                    "lifecycle_id": int(row[5]), "lifecycle_key": str(row[6]),
                    "file": path.name, "bytes": path.stat().st_size,
                }
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return None

    def _quarantine(self, path: Path, reason: str) -> None:
        if not path.exists():
            return
        destination = self.quarantine_dir / f"{path.name}.{reason}.{int(time.time() * 1000)}"
        os.replace(path, destination)
        _fsync_directory(self.quarantine_dir)

    def _recover_or_quarantine_staging(self) -> None:
        for path in self.evidence_dir.glob("*.next"):
            metadata = self._metadata(path)
            if metadata is None:
                self._quarantine(path, "interrupted")
                continue
            final = path.with_suffix("")
            if final.exists():
                self._quarantine(path, "duplicate")
            else:
                os.replace(path, final)
                _fsync_directory(self.evidence_dir)
        temporary = self.manifest_path.with_suffix(".json.tmp")
        if temporary.exists():
            self._quarantine(temporary, "interrupted")

    def evidence(self, *, validate: bool = True) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for path in sorted(self.evidence_dir.glob("lifecycle-*.sqlite3")):
            metadata = self._metadata(path)
            if metadata is None:
                if validate:
                    self._quarantine(path, "invalid")
                continue
            result.append(metadata)
        return sorted(result, key=lambda item: (int(item["lifecycle_id"]), str(item["file"])))

    def evidence_for(self, lifecycle_id: int) -> Path | None:
        for item in self.evidence():
            if int(item["lifecycle_id"]) == lifecycle_id:
                return self.evidence_dir / str(item["file"])
        return None

    def rebuild_manifest(self) -> None:
        payload = {"format_version": FORMAT_VERSION, "active": "reboot-trace.sqlite3", "evidence": self.evidence(validate=True)}
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
        for directory in (self.evidence_dir, self.quarantine_dir):
            total += sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
        return total

    def read_connection(self, timeout_seconds: float = 10.0, evidence_path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:", timeout=2, check_same_thread=False, uri=True)
        connection.row_factory = sqlite3.Row
        sources = [("active", f"{self.active_path.as_uri()}?mode=ro", 2)]
        if evidence_path is not None:
            sources.append(("evidence", f"{evidence_path.as_uri()}?mode=ro&immutable=1", 1))
        for alias, uri, _ in sources:
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (uri,))
        for table, keys in TABLE_KEYS.items():
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA active.table_info('{table}')")]
            quoted = ",".join(f'"{column}"' for column in columns)
            unions = [f"SELECT {quoted},{priority} AS _priority FROM {alias}.\"{table}\"" for alias, _, priority in sources]
            partition = ",".join(f'"{key}"' for key in keys)
            connection.execute(
                f'''CREATE TABLE "{table}" AS SELECT {quoted} FROM (
                  SELECT {quoted},row_number() OVER(PARTITION BY {partition} ORDER BY _priority DESC) AS _rn
                  FROM ({' UNION ALL '.join(unions)})
                ) WHERE _rn=1'''
            )
        for alias, _, _ in reversed(sources):
            connection.execute(f"DETACH DATABASE {alias}")
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + timeout_seconds
        connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        return connection

    def remove_capsule(self, item: dict[str, object]) -> None:
        path = self.evidence_dir / str(item["file"])
        if path.exists():
            tombstone = self.quarantine_dir / f"{path.name}.removed"
            os.replace(path, tombstone)
            _fsync_directory(self.evidence_dir)
            tombstone.unlink(missing_ok=True)
            _fsync_directory(self.quarantine_dir)
