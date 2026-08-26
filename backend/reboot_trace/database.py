from __future__ import annotations

import json
import os
import sqlite3
import shutil
import time
import threading
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from .config import Settings
from .identity import MountIdentity, _mount_identity
from .models import SnapshotData
from .segment_store import FORMAT_VERSION, INTERNAL_SCHEMA, SegmentStore, _fsync_directory, copy_events, copy_rows, database_ok

SCHEMA = """
CREATE TABLE IF NOT EXISTS host(id TEXT PRIMARY KEY, hostname TEXT NOT NULL, created_at_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lifecycle(
 id INTEGER PRIMARY KEY, host_id TEXT NOT NULL REFERENCES host(id) ON DELETE CASCADE,
 lifecycle_key TEXT NOT NULL, boot_id TEXT NOT NULL, container_instance_id TEXT,
 pid1_start_ticks INTEGER, pid_namespace_inode INTEGER, cgroup_hash TEXT,
 detection_method TEXT NOT NULL DEFAULT 'legacy', detection_confidence TEXT NOT NULL DEFAULT 'unknown',
 identity_first_observed_at_ms INTEGER, started_at_ms INTEGER NOT NULL, last_seen_at_ms INTEGER NOT NULL,
 ended_at_ms INTEGER, termination TEXT NOT NULL CHECK(termination IN ('active','clean_shutdown_observed','unclean_or_unknown')),
 summary_json TEXT NOT NULL DEFAULT '{}', UNIQUE(host_id,lifecycle_key));
CREATE TABLE IF NOT EXISTS service_session(
 id INTEGER PRIMARY KEY, lifecycle_id INTEGER NOT NULL REFERENCES lifecycle(id) ON DELETE CASCADE,
 started_at_ms INTEGER NOT NULL, stopped_at_ms INTEGER, backend_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot(
 id INTEGER PRIMARY KEY, lifecycle_id INTEGER NOT NULL REFERENCES lifecycle(id) ON DELETE CASCADE,
 captured_at_ms INTEGER NOT NULL, scheduled_at_ms INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
 sample_interval_ms INTEGER, detail_level TEXT NOT NULL CHECK(detail_level IN ('full','downsampled','final','summary_only')),
 persistence_state TEXT NOT NULL DEFAULT 'normal' CHECK(persistence_state IN ('normal','reclaiming','degraded_processes','system_only','paused')));
CREATE TABLE IF NOT EXISTS system_sample(snapshot_id INTEGER PRIMARY KEY REFERENCES snapshot(id) ON DELETE CASCADE, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS process_sample(
 snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE, pid INTEGER NOT NULL, create_time_ms INTEGER NOT NULL,
 ppid INTEGER, uid INTEGER NOT NULL, username TEXT, comm TEXT NOT NULL, state TEXT, threads INTEGER,
 cpu_percent REAL, rss_bytes INTEGER, swap_bytes INTEGER, read_bps REAL, write_bps REAL, read_bytes INTEGER, write_bytes INTEGER,
 cmdline_redacted TEXT NOT NULL CHECK(length(CAST(cmdline_redacted AS BLOB))<=4096), cmdline_hash TEXT NOT NULL,
 truncated INTEGER NOT NULL CHECK(truncated IN (0,1)), redaction_status TEXT NOT NULL CHECK(redaction_status IN ('redacted','no_sensitive_value','failed_closed')),
 PRIMARY KEY(snapshot_id,pid,create_time_ms));
CREATE TABLE IF NOT EXISTS process_rank(
 snapshot_id INTEGER NOT NULL, pid INTEGER NOT NULL, create_time_ms INTEGER NOT NULL, dimension TEXT NOT NULL CHECK(dimension IN ('cpu','rss','swap','read','write')),
 rank INTEGER NOT NULL, value REAL NOT NULL, PRIMARY KEY(snapshot_id,pid,create_time_ms,dimension),
 FOREIGN KEY(snapshot_id,pid,create_time_ms) REFERENCES process_sample(snapshot_id,pid,create_time_ms) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_sample(snapshot_id INTEGER NOT NULL REFERENCES snapshot(id) ON DELETE CASCADE, uid INTEGER NOT NULL, data_json TEXT NOT NULL, PRIMARY KEY(snapshot_id,uid));
CREATE TABLE IF NOT EXISTS event(
 id INTEGER PRIMARY KEY, lifecycle_id INTEGER NOT NULL REFERENCES lifecycle(id) ON DELETE CASCADE,
 snapshot_id INTEGER REFERENCES snapshot(id) ON DELETE SET NULL, occurred_at_ms INTEGER NOT NULL,
 type TEXT NOT NULL CHECK(type IN ('oom_observed','sampling_delay','collector_error','retention','collector_started','collector_stopped','host_shutdown_observed','container_instance_changed','identity_conflict','identity_baseline_established','collection_gap')),
 details_json TEXT NOT NULL CHECK(length(CAST(details_json AS BLOB))<=4096));
CREATE INDEX IF NOT EXISTS idx_snapshot_time ON snapshot(lifecycle_id,captured_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_rank ON process_rank(snapshot_id,dimension,rank);
CREATE INDEX IF NOT EXISTS idx_event_time ON event(lifecycle_id,occurred_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_session_time ON service_session(lifecycle_id,started_at_ms DESC);
"""

SCHEMA_VERSION = 3


def _details_json(details: dict[str, Any] | None) -> str:
    encoded = json.dumps(details or {}, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= 4096:
        return encoded
    return '{"truncated":true}'


def _same_mount(left: MountIdentity, right: MountIdentity) -> bool:
    return (left.mountpoint, left.fs_type, left.source) == (right.mountpoint, right.fs_type, right.source)


def _storage_capability(settings: Settings) -> dict[str, Any]:
    if not settings.data_dir.is_absolute():
        raise RuntimeError("RT_DATA_DIR must resolve to an absolute path")
    data_mount = _mount_identity(settings.proc_root, settings.data_dir)
    marker_mount = _mount_identity(settings.proc_root, settings.instance_marker_path)
    if data_mount is None:
        raise RuntimeError("cannot locate RT_DATA_DIR in PID 1 mountinfo")
    if data_mount.mountpoint == "/" and data_mount.fs_type in {"overlay", "fuse-overlayfs"}:
        raise RuntimeError("RT_DATA_DIR is on the container ephemeral root filesystem; choose an operator-verified persistent mount")
    if marker_mount is not None and _same_mount(data_mount, marker_mount):
        raise RuntimeError("RT_DATA_DIR and RT_INSTANCE_MARKER_PATH must not use the same mount")
    return {
        "data_path": str(settings.data_dir),
        "mountpoint": data_mount.mountpoint,
        "fs_type": data_mount.fs_type,
        "persistence_capability": "operator_verification_required",
        "persistence_reason": "separate mount observed; verify retention across a real target-container rebuild",
    }


class Repository:
    def __init__(self, settings: Settings, hostname: str, backend_version: str):
        self.settings = settings
        self.storage_capability = _storage_capability(settings)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "reboot-trace.sqlite3"
        self.segment_store = SegmentStore(
            settings.data_dir,
            settings.storage_limit_bytes,
        )
        self.segment_store.require_migrated_or_empty()
        new = not self.path.exists()
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level="IMMEDIATE")
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        if new:
            self.db.execute("PRAGMA page_size=4096")
            self.db.execute("PRAGMA auto_vacuum=NONE")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(f"PRAGMA max_page_count={max(128, int(settings.storage_limit_bytes * 0.8) // 4096)}")
        version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
        existing_tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        snapshot_columns = {row[1] for row in self.db.execute("PRAGMA table_info(snapshot)")} if "snapshot" in existing_tables else set()
        lifecycle_columns = {row[1] for row in self.db.execute("PRAGMA table_info(lifecycle)")} if "lifecycle" in existing_tables else set()
        event_sql_row = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='event'").fetchone()
        event_is_v3 = not event_sql_row or "container_instance_changed" in (event_sql_row[0] or "")
        snapshot_needs_v2 = bool(snapshot_columns) and "persistence_state" not in snapshot_columns
        lifecycle_needs_v3 = bool(lifecycle_columns) and "lifecycle_key" not in lifecycle_columns
        event_needs_v3 = "event" in existing_tables and not event_is_v3
        needs_migration = bool(existing_tables) and (version < SCHEMA_VERSION or snapshot_needs_v2 or lifecycle_needs_v3 or event_needs_v3)
        if needs_migration:
            migration_reserve = 2 * 1024 * 1024
            managed = sum(p.stat().st_size for p in settings.data_dir.iterdir() if p.is_file())
            free = shutil.disk_usage(settings.data_dir).free
            if managed + migration_reserve > settings.storage_limit_bytes or free < migration_reserve:
                self.db.close()
                raise RuntimeError("insufficient space to migrate database schema")
        if needs_migration:
            self._migrate_schema_v3(snapshot_needs_v2,lifecycle_needs_v3,event_needs_v3)
        else:
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        violations=self.db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            self.db.close()
            raise RuntimeError("database foreign key check failed after schema initialization")
        ok, reason = database_ok(self.db)
        if not ok:
            self.db.close()
            raise RuntimeError(f"database integrity check failed: {reason}")
        self.segment_store.initialize_active(self.db)
        self.host_id = settings.host_id_override or self._load_or_create_host_id()
        now = int(time.time() * 1000)
        self.db.execute("INSERT OR IGNORE INTO host VALUES(?,?,?)", (self.host_id, hostname, now))
        self.db.execute("UPDATE host SET hostname=? WHERE id=?", (hostname, self.host_id))
        self.db.commit()
        self.hostname = hostname
        self.backend_version = backend_version
        self.lifecycle_id: int | None = None
        self.session_id: int | None = None
        self.persistence_state = "paused" if self.managed_bytes() >= settings.storage_limit_bytes - 2 * 1024 * 1024 else "normal"
        self.max_transaction_bytes = 0
        self.reclaim_requested = False
        self._recover_evidence_state()

    def _migration_checkpoint(self, stage: str) -> None:
        """Test hook for proving that schema replacement is failure atomic."""

    def _evidence_checkpoint(self, stage: str) -> None:
        """Test hook for simulating a process loss during capsule sealing."""

    def _migrate_schema_v3(self, snapshot_needs_v2: bool, lifecycle_needs_v3: bool, event_needs_v3: bool) -> None:
        self.db.commit()
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if snapshot_needs_v2:
                self.db.execute("ALTER TABLE snapshot ADD COLUMN persistence_state TEXT NOT NULL DEFAULT 'normal'")
            if lifecycle_needs_v3:
                self.db.execute("""CREATE TABLE lifecycle_v3(
                  id INTEGER PRIMARY KEY, host_id TEXT NOT NULL REFERENCES host(id) ON DELETE CASCADE,
                  lifecycle_key TEXT NOT NULL, boot_id TEXT NOT NULL, container_instance_id TEXT,
                  pid1_start_ticks INTEGER, pid_namespace_inode INTEGER, cgroup_hash TEXT,
                  detection_method TEXT NOT NULL DEFAULT 'legacy', detection_confidence TEXT NOT NULL DEFAULT 'unknown',
                  identity_first_observed_at_ms INTEGER, started_at_ms INTEGER NOT NULL, last_seen_at_ms INTEGER NOT NULL,
                  ended_at_ms INTEGER, termination TEXT NOT NULL CHECK(termination IN ('active','clean_shutdown_observed','unclean_or_unknown')),
                  summary_json TEXT NOT NULL DEFAULT '{}', UNIQUE(host_id,lifecycle_key))""")
                self.db.execute("""INSERT INTO lifecycle_v3(id,host_id,lifecycle_key,boot_id,started_at_ms,last_seen_at_ms,ended_at_ms,termination,summary_json)
                  SELECT id,host_id,boot_id,boot_id,started_at_ms,last_seen_at_ms,ended_at_ms,termination,summary_json FROM lifecycle""")
                self.db.execute("DROP TABLE lifecycle")
                self.db.execute("ALTER TABLE lifecycle_v3 RENAME TO lifecycle")
            self._migration_checkpoint("after_lifecycle")
            if event_needs_v3:
                self.db.execute("""CREATE TABLE event_v3(
                  id INTEGER PRIMARY KEY, lifecycle_id INTEGER NOT NULL REFERENCES lifecycle(id) ON DELETE CASCADE,
                  snapshot_id INTEGER REFERENCES snapshot(id) ON DELETE SET NULL, occurred_at_ms INTEGER NOT NULL,
                  type TEXT NOT NULL CHECK(type IN ('oom_observed','sampling_delay','collector_error','retention','collector_started','collector_stopped','host_shutdown_observed','container_instance_changed','identity_conflict','identity_baseline_established','collection_gap')),
                  details_json TEXT NOT NULL CHECK(length(CAST(details_json AS BLOB))<=4096))""")
                self.db.execute("INSERT INTO event_v3 SELECT * FROM event")
                self.db.execute("DROP TABLE event")
                self.db.execute("ALTER TABLE event_v3 RENAME TO event")
            self._migration_checkpoint("after_event")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            violations=self.db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("database foreign key check failed during migration")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except Exception:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def _load_or_create_host_id(self) -> str:
        path = self.settings.data_dir / "host-id"
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            try: return str(uuid.UUID(value))
            except ValueError: raise RuntimeError("host-id file is invalid") from None
        existing = self.db.execute("SELECT id FROM host ORDER BY created_at_ms LIMIT 2").fetchall()
        if len(existing) == 1:
            value = existing[0][0]
        elif existing:
            raise RuntimeError("host-id file is missing and database contains multiple hosts")
        else:
            value = str(uuid.uuid4())
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(self.settings.data_dir, os.O_RDONLY)
        except (OSError, PermissionError):
            directory_fd = None
        if directory_fd is not None:
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        return value

    def managed_bytes(self) -> int:
        return self.segment_store.managed_bytes()

    def _next_id(self, table: str) -> int:
        row = self.db.execute("SELECT next_id FROM storage_counter WHERE name=?", (table,)).fetchone()
        if row is None:
            raise RuntimeError(f"missing global ID counter for {table}")
        value = int(row[0])
        self.db.execute("UPDATE storage_counter SET next_id=? WHERE name=?", (value + 1, table))
        return value

    def _insert_event(
        self,
        lifecycle_id: int,
        occurred_at_ms: int,
        event_type: str,
        details_json: str,
        snapshot_id: int | None = None,
    ) -> int:
        event_id = self._next_id("event")
        self.db.execute(
            "INSERT INTO event(id,lifecycle_id,snapshot_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?,?,?)",
            (event_id, lifecycle_id, snapshot_id, occurred_at_ms, event_type, details_json),
        )
        return event_id

    def read_connection(self, timeout_seconds: float = 10.0, lifecycle_ref: str | None = None) -> sqlite3.Connection:
        with self.lock:
            evidence_path: Path | None = None
            if lifecycle_ref:
                row = self.db.execute(
                    "SELECT id,termination FROM lifecycle WHERE host_id=? AND lifecycle_key=?", (self.host_id, lifecycle_ref)
                ).fetchone()
                if row is None:
                    matches = self.db.execute(
                        "SELECT id,termination FROM lifecycle WHERE host_id=? AND boot_id=? ORDER BY id DESC LIMIT 2",
                        (self.host_id, lifecycle_ref),
                    ).fetchall()
                    if len(matches) == 1:
                        row = matches[0]
                if row is not None:
                    evidence_path = self.segment_store.evidence_for(int(row[0]))
                    if row["termination"] != "active" and evidence_path is None:
                        active_count = int(self.db.execute(
                            "SELECT count(*) FROM snapshot WHERE lifecycle_id=?", (int(row[0]),)
                        ).fetchone()[0])
                        if active_count == 0:
                            with self.db:
                                self._mark_capsule_removed(int(row[0]))
            return self.segment_store.read_connection(timeout_seconds, evidence_path)

    @staticmethod
    def _estimated_snapshot_bytes(data: SnapshotData, rank_limit: int | None) -> int:
        """Conservative payload estimate used before opening the write transaction."""
        total = len(json.dumps(data.system, separators=(",", ":")).encode())
        total += len(json.dumps(data.users, separators=(",", ":")).encode())
        total += len(json.dumps(data.events, separators=(",", ":")).encode())
        for process in data.processes:
            ranks = process.ranks if rank_limit is None else {
                key: value for key, value in process.ranks.items() if value[0] <= rank_limit
            }
            if not ranks:
                continue
            total += len(process.cmdline_redacted.encode("utf-8")) + 640 + len(ranks) * 96
        # SQLite pages, indexes and transaction journal pages cost more than serialized payloads.
        return max(16 * 1024, total * 2 + 32 * 1024)

    def _choose_detail(self, data: SnapshotData, available: int) -> tuple[str, int | None] | None:
        if self._estimated_snapshot_bytes(data, None) <= available:
            return "normal", None
        rank_limit = max(1, self.settings.process_top_n // 2)
        while rank_limit >= 1:
            if self._estimated_snapshot_bytes(data, rank_limit) <= available:
                return "degraded_processes", rank_limit
            rank_limit //= 2
        if self._estimated_snapshot_bytes(data, 0) <= available:
            return "system_only", 0
        return None

    @staticmethod
    def _fingerprint_changed(row: sqlite3.Row, identity: dict[str, Any]) -> bool:
        values = (identity.get("pid1_start_ticks"), identity.get("pid_namespace_inode"), identity.get("cgroup_hash"))
        stored = (row["pid1_start_ticks"], row["pid_namespace_inode"], row["cgroup_hash"])
        return all(value is not None for value in values) and all(value is not None for value in stored) and values != stored

    def _finalize_lifecycle(self, lifecycle_id: int) -> None:
        self._prune_lifecycle_snapshots(lifecycle_id)
        self.db.execute(
            "UPDATE snapshot SET detail_level='final' WHERE lifecycle_id=? AND detail_level!='summary_only'",
            (lifecycle_id,),
        )
        self.db.execute(
            "UPDATE lifecycle SET termination='unclean_or_unknown',ended_at_ms=last_seen_at_ms WHERE id=?",
            (lifecycle_id,),
        )
        self._update_lifecycle_summary(lifecycle_id)

    def _snapshot_counts(self, connection: sqlite3.Connection, lifecycle_id: int) -> tuple[int, int]:
        full = int(connection.execute(
            "SELECT count(*) FROM snapshot WHERE lifecycle_id=? AND detail_level!='summary_only'",
            (lifecycle_id,),
        ).fetchone()[0])
        trend = int(connection.execute(
            "SELECT count(*) FROM snapshot WHERE lifecycle_id=? AND detail_level='summary_only'",
            (lifecycle_id,),
        ).fetchone()[0])
        return full, trend

    def _update_lifecycle_summary(self, lifecycle_id: int, *, force_summary_only: bool = False) -> None:
        row = self.db.execute("SELECT summary_json,termination FROM lifecycle WHERE id=?", (lifecycle_id,)).fetchone()
        if not row:
            return
        summary = json.loads(row["summary_json"] or "{}")
        full, trend = (0, 0) if force_summary_only else self._snapshot_counts(self.db, lifecycle_id)
        if row["termination"] == "active":
            state = "complete"
        elif full >= self.settings.final_snapshots_per_lifecycle:
            state = "final_complete"
        elif full >= 2:
            state = "final_partial"
        elif full == 1:
            state = "final_minimum"
        else:
            state = "summary_only"
        summary.update(retention_state=state, full_snapshot_count=full, trend_snapshot_count=trend)
        self.db.execute(
            "UPDATE lifecycle SET summary_json=? WHERE id=?",
            (json.dumps(summary, separators=(",", ":")), lifecycle_id),
        )

    def _prune_lifecycle_snapshots(self, lifecycle_id: int) -> None:
        rows = self.db.execute(
            "SELECT id,captured_at_ms,detail_level FROM snapshot WHERE lifecycle_id=? ORDER BY captured_at_ms DESC,id DESC",
            (lifecycle_id,),
        ).fetchall()
        dense = [int(row["id"]) for row in rows if row["detail_level"] != "summary_only"][:self.settings.final_snapshots_per_lifecycle]
        dense_set = set(dense)
        buckets: dict[int, int] = {}
        newest_ms = int(rows[0]["captured_at_ms"]) if rows else 0
        trend_cutoff_ms = newest_ms - self.settings.trend_snapshots_per_lifecycle * self.settings.trend_interval_ms
        for row in rows:
            snapshot_id = int(row["id"])
            if snapshot_id in dense_set:
                continue
            if self.settings.trend_snapshots_per_lifecycle == 0 or int(row["captured_at_ms"]) < trend_cutoff_ms:
                continue
            bucket = int(row["captured_at_ms"]) // self.settings.trend_interval_ms
            buckets.setdefault(bucket, snapshot_id)
        trend = list(buckets.values())[:self.settings.trend_snapshots_per_lifecycle]
        keep = dense_set | set(trend)
        if trend:
            placeholders = ",".join("?" for _ in trend)
            self.db.execute(f"DELETE FROM process_rank WHERE snapshot_id IN ({placeholders})", trend)
            self.db.execute(f"DELETE FROM process_sample WHERE snapshot_id IN ({placeholders})", trend)
            self.db.execute(f"DELETE FROM user_sample WHERE snapshot_id IN ({placeholders})", trend)
            self.db.execute(f"UPDATE snapshot SET detail_level='summary_only',persistence_state='system_only' WHERE id IN ({placeholders})", trend)
        remove = [int(row["id"]) for row in rows if int(row["id"]) not in keep]
        if remove:
            placeholders = ",".join("?" for _ in remove)
            self.db.execute(f"UPDATE event SET snapshot_id=NULL WHERE snapshot_id IN ({placeholders})", remove)
            self.db.execute(f"DELETE FROM snapshot WHERE id IN ({placeholders})", remove)
        self._update_lifecycle_summary(lifecycle_id)

    def _cleanup_sealed_lifecycle(self, lifecycle_id: int) -> None:
        self.db.execute("DELETE FROM service_session WHERE lifecycle_id=?", (lifecycle_id,))
        self.db.execute("DELETE FROM event WHERE lifecycle_id=?", (lifecycle_id,))
        self.db.execute("DELETE FROM snapshot WHERE lifecycle_id=?", (lifecycle_id,))

    def _mark_capsule_removed(self, lifecycle_id: int) -> None:
        self._update_lifecycle_summary(lifecycle_id, force_summary_only=True)

    def _prune_evidence(self, *, required_headroom: int = 0, protect_lifecycle_id: int | None = None) -> list[int]:
        removed: list[int] = []
        target = int(self.settings.storage_limit_bytes * 0.75)
        soft = int(self.settings.storage_limit_bytes * 0.85)
        need_cleanup = self.managed_bytes() >= soft or self.managed_bytes() + required_headroom > self.settings.storage_limit_bytes
        if not need_cleanup:
            return removed
        evidence = self.segment_store.evidence()
        latest = self.db.execute(
            "SELECT id FROM lifecycle WHERE host_id=? AND termination!='active' ORDER BY last_seen_at_ms DESC LIMIT 1",
            (self.host_id,),
        ).fetchone()
        protected = {int(latest[0])} if latest else set()
        if protect_lifecycle_id is not None:
            protected.add(protect_lifecycle_id)
        for item in evidence:
            lifecycle_id = int(item["lifecycle_id"])
            if lifecycle_id in protected:
                continue
            if self.managed_bytes() <= target and self.managed_bytes() + required_headroom <= self.settings.storage_limit_bytes:
                break
            self.segment_store.remove_capsule(item)
            self._mark_capsule_removed(lifecycle_id)
            removed.append(lifecycle_id)
        if removed:
            self.db.commit()
            self.segment_store.rebuild_manifest()
        return removed

    def _seal_lifecycle(self, lifecycle_id: int) -> None:
        lifecycle = self.db.execute("SELECT * FROM lifecycle WHERE id=?", (lifecycle_id,)).fetchone()
        if not lifecycle or lifecycle["termination"] == "active":
            return
        final_path = self.segment_store.capsule_path(lifecycle_id, lifecycle["lifecycle_key"])
        if final_path.exists() and self.segment_store.evidence_for(lifecycle_id):
            with self.db:
                self._cleanup_sealed_lifecycle(lifecycle_id)
            return
        required = self.path.stat().st_size + 2 * 1024 * 1024
        self._prune_evidence(required_headroom=required, protect_lifecycle_id=lifecycle_id)
        if self.managed_bytes() + required > self.settings.storage_limit_bytes:
            self.persistence_state = "paused"
            return
        staging = Path(str(final_path) + ".next")
        staging.unlink(missing_ok=True)
        target = sqlite3.connect(staging, isolation_level=None)
        try:
            target.execute("PRAGMA page_size=4096")
            target.execute("PRAGMA auto_vacuum=NONE")
            target.executescript(SCHEMA)
            target.executescript(INTERNAL_SCHEMA)
            target.execute("ATTACH DATABASE ? AS source", (str(self.path),))
            target.execute("BEGIN IMMEDIATE")
            copy_rows(target, "source", "host", where="id=?", params=(self.host_id,))
            copy_rows(target, "source", "lifecycle", where="id=?", params=(lifecycle_id,))
            copy_rows(target, "source", "service_session", where="lifecycle_id=?", params=(lifecycle_id,))
            ids = [int(row[0]) for row in self.db.execute("SELECT id FROM snapshot WHERE lifecycle_id=?", (lifecycle_id,))]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                copy_rows(target, "source", "snapshot", where=f"id IN ({placeholders})", params=ids)
                for table in ("system_sample", "process_sample", "process_rank", "user_sample"):
                    copy_rows(target, "source", table, where=f"snapshot_id IN ({placeholders})", params=ids)
            copy_events(target, "source", ids, lifecycle_id=lifecycle_id)
            generation = int(self.db.execute("SELECT generation FROM storage_meta WHERE singleton=1").fetchone()[0]) + 1
            now = int(time.time() * 1000)
            target.execute(
                "INSERT INTO storage_meta VALUES(1,?,?, 'evidence',?,?,?,?)",
                (FORMAT_VERSION, generation, now, now, lifecycle_id, lifecycle["lifecycle_key"]),
            )
            for name, next_id in self.db.execute("SELECT name,next_id FROM storage_counter"):
                target.execute("INSERT INTO storage_counter VALUES(?,?)", (name, next_id))
            target.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            target.execute("COMMIT")
            target.execute("DETACH DATABASE source")
            target.execute("PRAGMA journal_mode=DELETE")
            self._evidence_checkpoint("created")
            ok, reason = database_ok(target, full=True)
            if not ok:
                raise RuntimeError(f"evidence capsule validation failed: {reason}")
            full, trend = self._snapshot_counts(target, lifecycle_id)
            if full > self.settings.final_snapshots_per_lifecycle or trend > self.settings.trend_snapshots_per_lifecycle:
                raise RuntimeError("evidence capsule exceeds configured snapshot bounds")
            if target.execute("SELECT count(*) FROM lifecycle WHERE id=?", (lifecycle_id,)).fetchone()[0] != 1:
                raise RuntimeError("evidence capsule does not contain exactly one lifecycle")
            self._evidence_checkpoint("validated")
        except Exception:
            target.close()
            staging.unlink(missing_ok=True)
            raise
        finally:
            try:
                target.close()
            except Exception:
                pass
        descriptor = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._evidence_checkpoint("fsynced")
        os.replace(staging, final_path)
        final_path.chmod(0o444)
        _fsync_directory(self.segment_store.evidence_dir)
        self._evidence_checkpoint("renamed")
        self.segment_store.rebuild_manifest()
        self._evidence_checkpoint("manifest")
        with self.db:
            self._cleanup_sealed_lifecycle(lifecycle_id)
        self._evidence_checkpoint("cleaned")

    def _recover_evidence_state(self) -> None:
        with self.lock:
            self.segment_store.rebuild_manifest()
            completed = self.db.execute(
                "SELECT id FROM lifecycle WHERE termination!='active' ORDER BY id"
            ).fetchall()
            for row in completed:
                lifecycle_id = int(row[0])
                active_count = int(self.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()[0])
                capsule = self.segment_store.evidence_for(lifecycle_id)
                if active_count and capsule:
                    with self.db:
                        self._cleanup_sealed_lifecycle(lifecycle_id)
                elif active_count:
                    try:
                        self._seal_lifecycle(lifecycle_id)
                    except Exception:
                        self.persistence_state = "paused"
                elif capsule is None:
                    with self.db:
                        self._mark_capsule_removed(lifecycle_id)

    def start_lifecycle(self, boot_id: str, started_at_ms: int, identity: dict[str, Any] | None = None, *, start_session: bool = True, collector_started_event: bool = True) -> int:
        now = int(time.time() * 1000)
        identity = identity or {}
        preferred_key = str(identity.get("container_instance_id") or identity.get("preferred_key") or boot_id)
        method = "instance_marker" if identity.get("container_instance_id") else ("pid1_fingerprint" if identity else "legacy_boot_id")
        confidence = "confirmed" if identity.get("container_instance_id") else ("probable" if identity else "legacy")
        finalized_id: int | None = None
        with self.lock, self.db:
            active = self.db.execute("SELECT * FROM lifecycle WHERE host_id=? AND termination='active' ORDER BY id DESC LIMIT 1", (self.host_id,)).fetchone()
            baseline = bool(active and identity and active["pid1_start_ticks"] is None)
            marker_changed = bool(active and identity and active["container_instance_id"] and identity.get("container_instance_id") and active["container_instance_id"] != identity.get("container_instance_id"))
            fingerprint_changed = bool(active and identity and self._fingerprint_changed(active, identity))
            legacy_changed = bool(active and not identity and active["boot_id"] != boot_id)
            changed = marker_changed or fingerprint_changed or legacy_changed
            # Confidence describes the signals that actually caused the lifecycle
            # transition. A locally managed marker is confirmed only when the PID 1
            # fingerprint independently changes with it; either signal alone is
            # probable because a marker may be deleted or a PID identity may conflict.
            if active and marker_changed and fingerprint_changed:
                method = "instance_marker_pid1"
                confidence = "confirmed"
            elif active and marker_changed:
                method = "instance_marker"
                confidence = "probable"
            elif active and fingerprint_changed:
                method = "pid1_fingerprint"
                confidence = "probable"
            if baseline:
                self.db.execute("""
                    UPDATE lifecycle SET boot_id=?,container_instance_id=?,pid1_start_ticks=?,pid_namespace_inode=?,cgroup_hash=?,
                      detection_method='upgrade_baseline',detection_confidence='unknown',identity_first_observed_at_ms=? WHERE id=?
                """, (boot_id,identity.get("container_instance_id"),identity.get("pid1_start_ticks"),identity.get("pid_namespace_inode"),identity.get("cgroup_hash"),now,active["id"]))
                self._insert_event(active["id"], now, "identity_baseline_established", '{"pre_upgrade_gap_unknown":true}')
                lifecycle_id = active["id"]
            elif active and not changed:
                lifecycle_id = active["id"]
                if identity:
                    self.db.execute("UPDATE lifecycle SET boot_id=? WHERE id=?", (boot_id, lifecycle_id))
            else:
                if active:
                    self._finalize_lifecycle(active["id"])
                    finalized_id = int(active["id"])
                    if self.session_id:
                        self.db.execute("UPDATE service_session SET stopped_at_ms=? WHERE id=? AND stopped_at_ms IS NULL", (now, self.session_id))
                        self.session_id = None
                lifecycle_key = preferred_key
                if self.db.execute("SELECT 1 FROM lifecycle WHERE host_id=? AND lifecycle_key=?", (self.host_id,lifecycle_key)).fetchone():
                    suffix = str(identity.get("fingerprint") or uuid.uuid4().hex)[:12]
                    lifecycle_key = f"{preferred_key}:{suffix}:{now}"
                lifecycle_id = self._next_id("lifecycle")
                self.db.execute("""
                    INSERT INTO lifecycle(id,host_id,lifecycle_key,boot_id,container_instance_id,pid1_start_ticks,pid_namespace_inode,cgroup_hash,
                      detection_method,detection_confidence,identity_first_observed_at_ms,started_at_ms,last_seen_at_ms,termination)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (lifecycle_id,self.host_id,lifecycle_key,boot_id,identity.get("container_instance_id"),identity.get("pid1_start_ticks"),identity.get("pid_namespace_inode"),identity.get("cgroup_hash"),method,confidence,now,started_at_ms,now,"active"))
                if active and identity:
                    event_type = "container_instance_changed" if marker_changed else "identity_conflict"
                    details = {"previous_lifecycle_key":active["lifecycle_key"],"marker_changed":marker_changed,"pid1_fingerprint_changed":fingerprint_changed}
                    self._insert_event(lifecycle_id, now, event_type, _details_json(details))
            if start_session:
                self.session_id = self._next_id("service_session")
                self.db.execute("INSERT INTO service_session(id,lifecycle_id,started_at_ms,backend_version) VALUES(?,?,?,?)", (self.session_id, lifecycle_id, now, self.backend_version))
                if collector_started_event:
                    self._insert_event(lifecycle_id, now, "collector_started", "{}")
                current=self.db.execute("SELECT last_seen_at_ms FROM lifecycle WHERE id=?",(lifecycle_id,)).fetchone()
                gap_ms=now-int(current[0]) if current else 0
                if collector_started_event and gap_ms > self.settings.sample_interval_ms * 2:
                    self._insert_event(lifecycle_id, now, "collection_gap", _details_json({"gap_ms":gap_ms,"reason":"collector_unavailable"}))
        self.lifecycle_id = int(lifecycle_id)
        if finalized_id is not None:
            try:
                with self.lock:
                    self._seal_lifecycle(finalized_id)
            except Exception:
                self.persistence_state = "paused"
        latest_state = self.db.execute("SELECT persistence_state FROM snapshot WHERE lifecycle_id=? ORDER BY captured_at_ms DESC LIMIT 1", (self.lifecycle_id,)).fetchone()
        if latest_state and self.persistence_state != "paused":
            self.persistence_state = latest_state[0]
        return self.lifecycle_id

    def identity_changed(self, identity: dict[str, Any]) -> bool:
        if not self.lifecycle_id:
            return True
        with self.lock:
            row = self.db.execute("SELECT * FROM lifecycle WHERE id=?", (self.lifecycle_id,)).fetchone()
        if not row or row["pid1_start_ticks"] is None:
            return False
        marker_changed = bool(row["container_instance_id"] and identity.get("container_instance_id") and row["container_instance_id"] != identity.get("container_instance_id"))
        return marker_changed or self._fingerprint_changed(row, identity)

    def stop_session(self) -> None:
        if not self.session_id or not self.lifecycle_id:
            return
        session_id = self.session_id
        self.session_id = None
        now = int(time.time() * 1000)
        with self.lock, self.db:
            self.db.execute("UPDATE service_session SET stopped_at_ms=? WHERE id=? AND stopped_at_ms IS NULL", (now, session_id))
            self._insert_event(self.lifecycle_id, now, "collector_stopped", "{}")

    def record_event(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        if not self.lifecycle_id: return
        reserve = max(2 * 1024 * 1024, self.max_transaction_bytes * 2)
        if self.managed_bytes() >= self.settings.storage_limit_bytes - reserve:
            self.persistence_state = "paused"
            return
        encoded=_details_json(details)
        with self.lock, self.db:
            self._insert_event(self.lifecycle_id, int(time.time()*1000), event_type, encoded)

    def write_snapshot(self, data: SnapshotData, detail_level: str = "full") -> int | None:
        if not self.lifecycle_id:
            raise RuntimeError("lifecycle is not initialized")
        with self.lock:
            unsealed = self.db.execute(
                """SELECT 1 FROM lifecycle l WHERE l.host_id=? AND l.termination!='active'
                   AND EXISTS(SELECT 1 FROM snapshot s WHERE s.lifecycle_id=l.id) LIMIT 1""",
                (self.host_id,),
            ).fetchone()
            if unsealed:
                self.persistence_state = "paused"
                return None
            lifecycle = self.db.execute("SELECT * FROM lifecycle WHERE id=?", (self.lifecycle_id,)).fetchone()
        if not data.identity and lifecycle and data.boot_id != lifecycle["boot_id"]:
            raise RuntimeError(f"snapshot boot_id {data.boot_id} does not match active legacy lifecycle")
        if data.identity and lifecycle and self._fingerprint_changed(lifecycle, data.identity):
            raise RuntimeError("snapshot container identity does not match active lifecycle")
        before = self.managed_bytes()
        reserve = max(2 * 1024 * 1024, self.max_transaction_bytes * 2)
        soft_limit = min(int(self.settings.storage_limit_bytes * 0.85), self.settings.storage_limit_bytes - reserve)
        if before >= soft_limit:
            self.reclaim_requested = True
        current = self.managed_bytes()
        choice = self._choose_detail(data, self.settings.storage_limit_bytes - reserve - current)
        if choice is None:
            self.persistence_state = "paused"
            return None
        state, rank_limit = choice
        try:
            with self.lock, self.db:
                stored_detail = "summary_only" if state == "system_only" else detail_level
                snapshot_id = self._next_id("snapshot")
                self.db.execute("INSERT INTO snapshot(id,lifecycle_id,captured_at_ms,scheduled_at_ms,duration_ms,sample_interval_ms,detail_level,persistence_state) VALUES(?,?,?,?,?,?,?,?)", (snapshot_id, self.lifecycle_id, data.captured_at_ms, data.scheduled_at_ms, data.duration_ms, data.sample_interval_ms, stored_detail, state))
                self.db.execute("INSERT INTO system_sample VALUES(?,?)", (snapshot_id, json.dumps(data.system, separators=(",", ":"))))
                for p in data.processes:
                    ranks = p.ranks if rank_limit is None else {key:value for key,value in p.ranks.items() if value[0] <= rank_limit}
                    if not ranks:
                        continue
                    self.db.execute("INSERT INTO process_sample VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id,p.pid,p.create_time_ms,p.ppid,p.uid,p.username,p.comm,p.state,p.threads,p.cpu_percent,p.rss_bytes,p.swap_bytes,p.read_bps,p.write_bps,p.read_bytes,p.write_bytes,p.cmdline_redacted,p.cmdline_hash,int(p.truncated),p.redaction_status))
                    for dimension, (rank, value) in ranks.items():
                        self.db.execute("INSERT INTO process_rank VALUES(?,?,?,?,?,?)", (snapshot_id,p.pid,p.create_time_ms,dimension,rank,value))
                for user in data.users:
                    self.db.execute("INSERT INTO user_sample VALUES(?,?,?)", (snapshot_id,user["uid"],json.dumps(user,separators=(",", ":"))))
                for event in data.events:
                    self._insert_event(self.lifecycle_id, event.get("occurred_at_ms", data.captured_at_ms), event["type"], _details_json(event.get("details")), snapshot_id)
                if state != "normal":
                    details = json.dumps({"persistence_state": state, "rank_limit": rank_limit}, separators=(",", ":"))
                    self._insert_event(self.lifecycle_id, data.captured_at_ms, "retention", details, snapshot_id)
                self.db.execute("UPDATE lifecycle SET last_seen_at_ms=? WHERE id=?", (data.captured_at_ms,self.lifecycle_id))
                if data.boot_id != lifecycle["boot_id"]:
                    self.db.execute("UPDATE lifecycle SET boot_id=? WHERE id=?", (data.boot_id,self.lifecycle_id))
                self._prune_lifecycle_snapshots(self.lifecycle_id)
        except sqlite3.OperationalError as exc:
            if "full" not in str(exc).lower():
                raise
            self.persistence_state = "paused"
            return None
        after = self.managed_bytes()
        self.max_transaction_bytes = max(self.max_transaction_bytes, max(0, after-before))
        if after >= int(self.settings.storage_limit_bytes * 0.85):
            self.reclaim_requested = True
        self.persistence_state = state
        return snapshot_id

    def reclaim(self) -> None:
        self.persistence_state = "reclaiming"
        before = self.managed_bytes()
        unsealed = False
        try:
            with self.lock:
                removed = self._prune_evidence()
                self._recover_evidence_state()
                unsealed = self.db.execute(
                    """SELECT 1 FROM lifecycle l WHERE l.host_id=? AND l.termination!='active'
                       AND EXISTS(SELECT 1 FROM snapshot s WHERE s.lifecycle_id=l.id) LIMIT 1""",
                    (self.host_id,),
                ).fetchone() is not None
        finally:
            self.reclaim_requested = False
        after = self.managed_bytes()
        self.persistence_state = "normal" if not unsealed and after < self.settings.storage_limit_bytes - 2 * 1024 * 1024 else "paused"
        if self.persistence_state != "paused":
            try:
                self.record_event("retention", {"state":"evidence_capsules","deleted_lifecycles":removed,"before_bytes":before,"after_bytes":after})
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise

    def status(self, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        db = connection or self.db
        with self.lock if connection is None else nullcontext():
            lifecycle = db.execute("SELECT * FROM lifecycle WHERE id=?", (self.lifecycle_id,)).fetchone()
            last = db.execute("SELECT captured_at_ms,scheduled_at_ms,duration_ms FROM snapshot WHERE lifecycle_id=? ORDER BY captured_at_ms DESC LIMIT 1", (self.lifecycle_id,)).fetchone()
        return {"host_id":self.host_id,"hostname":self.hostname,"boot_id":lifecycle["boot_id"],"lifecycle_key":lifecycle["lifecycle_key"],"lifecycle":dict(lifecycle),"last":dict(last) if last else None,"storage":{"used_bytes":self.managed_bytes(),"limit_bytes":self.settings.storage_limit_bytes,"persistence_state":self.persistence_state,**self.storage_capability}}

    def latest(self, connection: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        db = connection or self.db
        with self.lock if connection is None else nullcontext():
            row = db.execute("SELECT s.*,ss.data_json FROM snapshot s JOIN system_sample ss ON ss.snapshot_id=s.id WHERE s.lifecycle_id=? ORDER BY s.captured_at_ms DESC LIMIT 1", (self.lifecycle_id,)).fetchone()
        if not row: return None
        return {**dict(row),"system":json.loads(row["data_json"])}

    def close(self) -> None:
        self.stop_session()
        with self.lock: self.db.close()
