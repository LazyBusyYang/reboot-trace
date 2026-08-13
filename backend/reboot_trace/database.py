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
FINAL_WINDOW_MS = 10 * 60 * 1000
MIN_FINAL_SNAPSHOTS = 12


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
        new = not self.path.exists()
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False, isolation_level="IMMEDIATE")
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        if new:
            self.db.execute("PRAGMA page_size=4096")
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(f"PRAGMA journal_size_limit={max(1048576, settings.storage_limit_bytes // 10)}")
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

    def _migration_checkpoint(self, stage: str) -> None:
        """Test hook for proving that schema replacement is failure atomic."""

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
        return sum(p.stat().st_size for p in self.settings.data_dir.iterdir() if p.is_file())

    def read_connection(self, timeout_seconds: float = 10.0) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        deadline = time.monotonic() + timeout_seconds
        connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        return connection

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
        # SQLite pages, indexes and WAL frames cost more than serialized payloads.
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
        row = self.db.execute("SELECT last_seen_at_ms FROM lifecycle WHERE id=?", (lifecycle_id,)).fetchone()
        if not row:
            return
        cutoff = int(row["last_seen_at_ms"]) - FINAL_WINDOW_MS
        self.db.execute("""
            UPDATE snapshot SET detail_level='final'
            WHERE lifecycle_id=? AND persistence_state='normal' AND detail_level='full' AND captured_at_ms>=?
        """, (lifecycle_id, cutoff))
        self.db.execute("""
            UPDATE snapshot SET detail_level='final'
            WHERE id IN (
              SELECT id FROM snapshot WHERE lifecycle_id=? AND persistence_state='normal' AND detail_level='full'
              ORDER BY captured_at_ms DESC LIMIT ?
            )
        """, (lifecycle_id, MIN_FINAL_SNAPSHOTS))
        count = self.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=? AND detail_level='final' AND persistence_state='normal'", (lifecycle_id,)).fetchone()[0]
        retention_state = "final_complete" if count >= MIN_FINAL_SNAPSHOTS else "final_partial"
        self.db.execute("UPDATE lifecycle SET termination='unclean_or_unknown',ended_at_ms=last_seen_at_ms,summary_json=? WHERE id=?", (json.dumps({"retention_state":retention_state}, separators=(",", ":")), lifecycle_id))

    def start_lifecycle(self, boot_id: str, started_at_ms: int, identity: dict[str, Any] | None = None, *, start_session: bool = True, collector_started_event: bool = True) -> int:
        now = int(time.time() * 1000)
        identity = identity or {}
        preferred_key = str(identity.get("container_instance_id") or identity.get("preferred_key") or boot_id)
        method = "instance_marker" if identity.get("container_instance_id") else ("pid1_fingerprint" if identity else "legacy_boot_id")
        confidence = "confirmed" if identity.get("container_instance_id") else ("probable" if identity else "legacy")
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
                self.db.execute("INSERT INTO event(lifecycle_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?)", (active["id"],now,"identity_baseline_established",'{"pre_upgrade_gap_unknown":true}'))
                lifecycle_id = active["id"]
            elif active and not changed:
                lifecycle_id = active["id"]
                if identity:
                    self.db.execute("UPDATE lifecycle SET boot_id=? WHERE id=?", (boot_id, lifecycle_id))
            else:
                if active:
                    self._finalize_lifecycle(active["id"])
                    if self.session_id:
                        self.db.execute("UPDATE service_session SET stopped_at_ms=? WHERE id=? AND stopped_at_ms IS NULL", (now, self.session_id))
                        self.session_id = None
                lifecycle_key = preferred_key
                if self.db.execute("SELECT 1 FROM lifecycle WHERE host_id=? AND lifecycle_key=?", (self.host_id,lifecycle_key)).fetchone():
                    suffix = str(identity.get("fingerprint") or uuid.uuid4().hex)[:12]
                    lifecycle_key = f"{preferred_key}:{suffix}:{now}"
                cur = self.db.execute("""
                    INSERT INTO lifecycle(host_id,lifecycle_key,boot_id,container_instance_id,pid1_start_ticks,pid_namespace_inode,cgroup_hash,
                      detection_method,detection_confidence,identity_first_observed_at_ms,started_at_ms,last_seen_at_ms,termination)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (self.host_id,lifecycle_key,boot_id,identity.get("container_instance_id"),identity.get("pid1_start_ticks"),identity.get("pid_namespace_inode"),identity.get("cgroup_hash"),method,confidence,now,started_at_ms,now,"active"))
                lifecycle_id = cur.lastrowid
                if active and identity:
                    event_type = "container_instance_changed" if marker_changed else "identity_conflict"
                    details = {"previous_lifecycle_key":active["lifecycle_key"],"marker_changed":marker_changed,"pid1_fingerprint_changed":fingerprint_changed}
                    self.db.execute("INSERT INTO event(lifecycle_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?)", (lifecycle_id,now,event_type,_details_json(details)))
            if start_session:
                cur = self.db.execute("INSERT INTO service_session(lifecycle_id,started_at_ms,backend_version) VALUES(?,?,?)", (lifecycle_id, now, self.backend_version))
                self.session_id = cur.lastrowid
                if collector_started_event:
                    self.db.execute("INSERT INTO event(lifecycle_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?)", (lifecycle_id, now, "collector_started", "{}"))
                current=self.db.execute("SELECT last_seen_at_ms FROM lifecycle WHERE id=?",(lifecycle_id,)).fetchone()
                gap_ms=now-int(current[0]) if current else 0
                if collector_started_event and gap_ms > self.settings.sample_interval_ms * 2:
                    self.db.execute("INSERT INTO event(lifecycle_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?)",(lifecycle_id,now,"collection_gap",_details_json({"gap_ms":gap_ms,"reason":"collector_unavailable"})))
        self.lifecycle_id = int(lifecycle_id)
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
            self.db.execute("INSERT INTO event(lifecycle_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?)", (self.lifecycle_id, now, "collector_stopped", "{}"))

    def record_event(self, event_type: str, details: dict[str, Any] | None = None) -> None:
        if not self.lifecycle_id: return
        reserve = max(2 * 1024 * 1024, self.max_transaction_bytes * 2)
        if self.managed_bytes() >= self.settings.storage_limit_bytes - reserve:
            self.persistence_state = "paused"
            return
        encoded=_details_json(details)
        with self.lock, self.db:
            self.db.execute("INSERT INTO event(lifecycle_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?)",(self.lifecycle_id,int(time.time()*1000),event_type,encoded))

    def write_snapshot(self, data: SnapshotData, detail_level: str = "full") -> int | None:
        if not self.lifecycle_id:
            raise RuntimeError("lifecycle is not initialized")
        with self.lock:
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
                cur = self.db.execute("INSERT INTO snapshot(lifecycle_id,captured_at_ms,scheduled_at_ms,duration_ms,sample_interval_ms,detail_level,persistence_state) VALUES(?,?,?,?,?,?,?)", (self.lifecycle_id, data.captured_at_ms, data.scheduled_at_ms, data.duration_ms, data.sample_interval_ms, stored_detail, state))
                snapshot_id = int(cur.lastrowid)
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
                    self.db.execute("INSERT INTO event(lifecycle_id,snapshot_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?,?)", (self.lifecycle_id,snapshot_id,event.get("occurred_at_ms",data.captured_at_ms),event["type"],_details_json(event.get("details"))))
                if state != "normal":
                    details = json.dumps({"persistence_state": state, "rank_limit": rank_limit}, separators=(",", ":"))
                    self.db.execute("INSERT INTO event(lifecycle_id,snapshot_id,occurred_at_ms,type,details_json) VALUES(?,?,?,?,?)", (self.lifecycle_id,snapshot_id,data.captured_at_ms,"retention",details))
                self.db.execute("UPDATE lifecycle SET last_seen_at_ms=? WHERE id=?", (data.captured_at_ms,self.lifecycle_id))
                if data.boot_id != lifecycle["boot_id"]:
                    self.db.execute("UPDATE lifecycle SET boot_id=? WHERE id=?", (data.boot_id,self.lifecycle_id))
        except sqlite3.OperationalError as exc:
            if "full" not in str(exc).lower():
                raise
            self.persistence_state = "paused"
            return None
        after = self.managed_bytes()
        self.max_transaction_bytes = max(self.max_transaction_bytes, max(0, after-before))
        self.persistence_state = state
        return snapshot_id

    def reclaim(self) -> None:
        self.persistence_state = "reclaiming"
        before=self.managed_bytes(); deleted=0
        # Downsample old current-lifecycle history first so a long-running target
        # cannot evict the most recent completed lifecycle's restart evidence.
        with self.lock, self.db:
            representatives=self.db.execute("""
              WITH ranked AS (
                SELECT s.id,row_number() OVER(PARTITION BY s.lifecycle_id,(s.captured_at_ms/60000) ORDER BY s.captured_at_ms DESC) AS n
                FROM snapshot s JOIN lifecycle l ON l.id=s.lifecycle_id
                WHERE s.captured_at_ms < l.last_seen_at_ms-600000 AND s.detail_level='full'
              ) SELECT id FROM ranked WHERE n=1
            """).fetchall()
            if representatives:
                ids=[(row["id"],) for row in representatives]
                self.db.executemany("DELETE FROM process_sample WHERE snapshot_id=?",ids)
                self.db.executemany("DELETE FROM user_sample WHERE snapshot_id=?",ids)
                self.db.executemany("UPDATE snapshot SET detail_level='downsampled' WHERE id=?",ids)
                lifecycle_ids = self.db.execute("SELECT DISTINCT lifecycle_id FROM snapshot WHERE id IN (%s)" % ",".join("?" * len(ids)), [x[0] for x in ids]).fetchall()
                for lifecycle_id in lifecycle_ids:
                    self.db.execute("UPDATE lifecycle SET summary_json=? WHERE id=?", ('{"retention_state":"downsampled"}', lifecycle_id[0]))
        target = int(self.settings.storage_limit_bytes * 0.75)
        while self.managed_bytes() > target:
            downgrade_final = False
            recent_completed = self.db.execute("SELECT id FROM lifecycle WHERE host_id=? AND id!=? AND termination!='active' ORDER BY last_seen_at_ms DESC LIMIT 1",(self.host_id,self.lifecycle_id)).fetchone()
            protected_id = recent_completed[0] if recent_completed else -1
            # Remove high-frequency current history older than ten minutes first.
            rows = self.db.execute("""
                WITH candidates AS (
                  SELECT s.id,s.captured_at_ms,l.last_seen_at_ms,
                    row_number() OVER (PARTITION BY s.lifecycle_id,(s.captured_at_ms/60000) ORDER BY s.captured_at_ms DESC) AS minute_rank
                  FROM snapshot s JOIN lifecycle l ON l.id=s.lifecycle_id
                  WHERE s.lifecycle_id = ? AND s.captured_at_ms < l.last_seen_at_ms-600000 AND s.detail_level!='final'
                ) SELECT id FROM candidates WHERE minute_rank>1 ORDER BY captured_at_ms LIMIT 100
            """, (self.lifecycle_id,)).fetchall()
            if not rows:
                # Then remove historical detail outside protected final windows.
                rows = self.db.execute("""
                    SELECT s.id FROM snapshot s JOIN lifecycle l ON l.id=s.lifecycle_id
                    WHERE s.lifecycle_id != ? AND s.detail_level!='final'
                    ORDER BY s.captured_at_ms LIMIT 100
                """, (self.lifecycle_id,)).fetchall()
            if not rows:
                # Older final evidence yields before the most recent completed lifecycle.
                rows = self.db.execute("""
                    SELECT s.id FROM snapshot s JOIN lifecycle l ON l.id=s.lifecycle_id
                    WHERE s.lifecycle_id NOT IN (?,?) AND s.detail_level='final'
                    ORDER BY l.last_seen_at_ms,s.captured_at_ms LIMIT 100
                """,(self.lifecycle_id,protected_id)).fetchall()
            if not rows:
                # Shrink the newest completed final window, but keep its last 12
                # complete snapshots with process and user evidence intact.
                rows = self.db.execute("""
                    SELECT id FROM snapshot WHERE lifecycle_id=? AND detail_level='final'
                      AND id NOT IN (SELECT id FROM snapshot WHERE lifecycle_id=? AND detail_level='final' AND persistence_state='normal' ORDER BY captured_at_ms DESC LIMIT ?)
                    ORDER BY captured_at_ms LIMIT 100
                """,(protected_id,protected_id,MIN_FINAL_SNAPSHOTS)).fetchall()
            if not rows:
                # Current lifecycle is a ring buffer. Pause rather than deleting the
                # protected twelve completed snapshots.
                rows = self.db.execute("SELECT id FROM snapshot WHERE lifecycle_id=? AND detail_level!='final' ORDER BY captured_at_ms", (self.lifecycle_id,)).fetchall()
                rows = rows[:-2] if len(rows)>2 else []
            if not rows:
                self.persistence_state = "paused"
                break
            with self.lock, self.db:
                affected = self.db.execute("SELECT DISTINCT lifecycle_id FROM snapshot WHERE id IN (%s)" % ",".join("?" * len(rows)), [r["id"] for r in rows]).fetchall()
                self.db.executemany("DELETE FROM snapshot WHERE id=?", [(r["id"],) for r in rows])
                for lifecycle_id in affected:
                    remaining = self.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?", (lifecycle_id[0],)).fetchone()[0]
                    final_count=self.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=? AND detail_level='final' AND persistence_state='normal'",(lifecycle_id[0],)).fetchone()[0]
                    state = "final_minimum" if lifecycle_id[0]==protected_id and final_count==MIN_FINAL_SNAPSHOTS else ("partial" if remaining else "summary_only")
                    self.db.execute("UPDATE lifecycle SET summary_json=? WHERE id=?", (json.dumps({"retention_state":state},separators=(",", ":")), lifecycle_id[0]))
            deleted += len(rows)
            with self.lock:
                self.db.execute("PRAGMA incremental_vacuum(256)")
                checkpoint = self.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if checkpoint and checkpoint[0] == 0:
                    self.db.execute("PRAGMA busy_timeout=0")
                    try:
                        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        self.db.execute("PRAGMA busy_timeout=5000")
        self.reclaim_requested = False
        self.persistence_state = "normal" if self.managed_bytes() < target else "paused"
        if self.persistence_state != "paused":
            self.record_event("retention",{"state":self.persistence_state,"deleted_snapshots":deleted,"before_bytes":before,"after_bytes":self.managed_bytes()})

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
