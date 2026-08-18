from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .database import FINAL_WINDOW_MS, MIN_FINAL_SNAPSHOTS, SCHEMA, SCHEMA_VERSION
from .segment_store import FORMAT_VERSION, INTERNAL_SCHEMA, SegmentStore, copy_events, copy_rows, database_ok


DATA_TABLES = (
    "host", "lifecycle", "service_session", "snapshot", "system_sample",
    "process_sample", "process_rank", "user_sample", "event",
)
MIGRATION_STATE = ".segment-migration.json"


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _write_state(data_dir: Path, payload: dict[str, object]) -> None:
    path = data_dir / MIGRATION_STATE
    temporary = data_dir / f"{MIGRATION_STATE}.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_once(source: Path, destination: Path) -> None:
    if source.exists() and not destination.exists():
        os.replace(source, destination)
        return
    if destination.exists() and not source.exists():
        return
    raise RuntimeError(f"ambiguous migration install state for {destination.name}")


def _resume_install(data_dir: Path, state: dict[str, object]) -> dict[str, object]:
    stage = data_dir / str(state["stage"])
    steps = (
        ("segments", stage / "segments", data_dir / "segments"),
        ("quarantine", stage / "quarantine", data_dir / "quarantine"),
        ("manifest", stage / "segments.json", data_dir / "segments.json"),
    )
    for phase, source, destination in steps:
        _move_once(source, destination)
        state["phase"] = phase
        _write_state(data_dir, state)
    staged_active = stage / "reboot-trace.sqlite3"
    installed_active = data_dir / "reboot-trace.sqlite3"
    if staged_active.exists():
        os.replace(staged_active, installed_active)
    elif not SegmentStore.is_managed_database(installed_active):
        raise RuntimeError("migration active commit point is missing")
    state["phase"] = "active"
    _write_state(data_dir, state)
    if not SegmentStore.is_managed_database(data_dir / "reboot-trace.sqlite3"):
        raise RuntimeError("installed active database is not a valid segmented database")
    store = SegmentStore(data_dir, 1 << 60, 1 << 20)
    healthy = store.segment_paths()
    if not healthy:
        raise RuntimeError("installed segmented layout has no healthy history segment")
    shutil.rmtree(stage, ignore_errors=True)
    (data_dir / MIGRATION_STATE).unlink()
    return {
        "dry_run": False,
        "backup_dir": str(state["backup_dir"]),
        "resumed": bool(state.get("resumed", False)),
    }


def _create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA auto_vacuum=NONE")
    connection.executescript(SCHEMA)
    connection.executescript(INTERNAL_SCHEMA)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


def _logical_copy(source: Path, target: Path, generation: int, state: str, now: int) -> None:
    destination = _create_database(target)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination.execute("ATTACH DATABASE ? AS source", (str(source),))
        destination.execute("PRAGMA foreign_keys=OFF")
        destination.execute("BEGIN IMMEDIATE")
        for table in DATA_TABLES:
            copy_rows(destination, "source", table)
        destination.execute(
            "INSERT INTO storage_meta VALUES(1,?,?,?,?,?)",
            (FORMAT_VERSION, generation, state, now, now if state == "sealed" else None),
        )
        for table in ("lifecycle", "service_session", "snapshot", "event"):
            next_id = source_connection.execute(f"SELECT COALESCE(max(id),0)+1 FROM {table}").fetchone()[0]
            destination.execute("INSERT INTO storage_counter VALUES(?,?)", (table, next_id))
        destination.execute("COMMIT")
        destination.execute("DETACH DATABASE source")
        destination.execute("PRAGMA foreign_keys=ON")
        destination.execute("PRAGMA journal_mode=DELETE")
        ok, reason = database_ok(destination, full=True)
        if not ok:
            raise RuntimeError(f"rebuilt segment failed validation: {reason}")
    finally:
        source_connection.close()
        destination.close()


def _create_active_from_segment(segment: Path, active: Path, now: int) -> None:
    target = _create_database(active)
    source = sqlite3.connect(f"file:{segment}?mode=ro&immutable=1", uri=True)
    try:
        target.execute("ATTACH DATABASE ? AS source", (str(segment),))
        target.execute("PRAGMA foreign_keys=OFF")
        target.execute("BEGIN IMMEDIATE")
        for table in ("host", "lifecycle", "service_session"):
            copy_rows(target, "source", table)
        ids: set[int] = set()
        current = source.execute(
            "SELECT id,last_seen_at_ms FROM lifecycle WHERE termination='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if current:
            ids.update(row[0] for row in source.execute(
                "SELECT id FROM snapshot WHERE lifecycle_id=? AND captured_at_ms>=?",
                (current[0], int(current[1]) - FINAL_WINDOW_MS),
            ))
            ids.update(row[0] for row in source.execute(
                "SELECT id FROM snapshot WHERE lifecycle_id=? ORDER BY captured_at_ms DESC LIMIT ?",
                (current[0], MIN_FINAL_SNAPSHOTS),
            ))
        completed = source.execute(
            "SELECT id FROM lifecycle WHERE termination!='active' ORDER BY last_seen_at_ms DESC LIMIT 1"
        ).fetchone()
        if completed:
            ids.update(row[0] for row in source.execute(
                "SELECT id FROM snapshot WHERE lifecycle_id=? AND detail_level='final' AND persistence_state='normal' "
                "ORDER BY captured_at_ms DESC LIMIT ?", (completed[0], MIN_FINAL_SNAPSHOTS),
            ))
        if ids:
            ordered = sorted(ids)
            placeholders = ",".join("?" for _ in ordered)
            copy_rows(target, "source", "snapshot", where=f"id IN ({placeholders})", params=ordered)
            for table in ("system_sample", "process_sample", "process_rank", "user_sample"):
                copy_rows(target, "source", table, where=f"snapshot_id IN ({placeholders})", params=ordered)
        copy_events(target, "source", sorted(ids))
        target.execute(
            "INSERT INTO storage_meta VALUES(1,?,?, 'active',?,NULL)", (FORMAT_VERSION, 2, now)
        )
        for table in ("lifecycle", "service_session", "snapshot", "event"):
            next_id = source.execute(f"SELECT COALESCE(max(id),0)+1 FROM {table}").fetchone()[0]
            target.execute("INSERT INTO storage_counter VALUES(?,?)", (table, next_id))
        target.execute("COMMIT")
        target.execute("DETACH DATABASE source")
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("PRAGMA journal_mode=DELETE")
        ok, reason = database_ok(target, full=True)
        if not ok:
            raise RuntimeError(f"new active database failed validation: {reason}")
    finally:
        source.close()
        target.close()


def migrate(data_dir: Path, backup_root: Path, *, dry_run: bool = False) -> dict[str, object]:
    data_dir = data_dir.resolve()
    backup_root = backup_root.resolve()
    source = data_dir / "reboot-trace.sqlite3"
    state_path = data_dir / MIGRATION_STATE
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if Path(str(state["backup_dir"])).resolve().parent != backup_root:
            raise RuntimeError("pending migration was created with a different backup directory")
        if dry_run:
            return {"dry_run": True, "pending_phase": state.get("phase"), "resumable": True}
        state["resumed"] = True
        return _resume_install(data_dir, state)
    if not source.is_file():
        raise RuntimeError(f"legacy database does not exist: {source}")
    if SegmentStore.is_managed_database(source) or (data_dir / "segments.json").exists():
        raise RuntimeError("data directory is already using segmented storage")
    if _inside(backup_root, data_dir):
        raise RuntimeError("backup directory must be outside RT_DATA_DIR")
    connection = sqlite3.connect(source, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        ok, reason = database_ok(connection, full=True)
        if not ok:
            raise RuntimeError(f"legacy database failed validation: {reason}")
        counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in DATA_TABLES}
        connection.execute("ROLLBACK")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    required = source.stat().st_size * 2 + 2 * 1024 * 1024
    if shutil.disk_usage(data_dir).free < required:
        raise RuntimeError("insufficient free space for segmented migration")
    result: dict[str, object] = {
        "data_dir": str(data_dir), "source_bytes": source.stat().st_size,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "rows": counts,
    }
    if dry_run:
        result["dry_run"] = True
        return result

    backup_root.mkdir(parents=True, exist_ok=True)
    backup_required = source.stat().st_size + 2 * 1024 * 1024
    if shutil.disk_usage(backup_root).free < backup_required:
        raise RuntimeError("insufficient backup filesystem space for migration backup")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / f"reboot-trace-segment-migration-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_database = backup_dir / source.name
    source_connection = sqlite3.connect(source)
    backup_connection = sqlite3.connect(backup_database)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()
    guard = sqlite3.connect(source, timeout=0, isolation_level=None)
    guard_open = True
    guard.execute("BEGIN EXCLUSIVE")
    ok, reason = database_ok(guard, full=True)
    if not ok:
        guard.execute("ROLLBACK")
        guard.close()
        raise RuntimeError(f"legacy database changed or failed validation after backup: {reason}")
    backup_check = sqlite3.connect(f"file:{backup_database}?mode=ro", uri=True)
    try:
        backup_counts = {table: backup_check.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in DATA_TABLES}
    finally:
        backup_check.close()
    guarded_counts = {table: guard.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in DATA_TABLES}
    if guarded_counts != backup_counts:
        guard.execute("ROLLBACK")
        guard.close()
        raise RuntimeError("legacy database changed while migration backup was created")
    for name in ("reboot-trace.sqlite3-wal", "reboot-trace.sqlite3-shm", "reboot-trace.sqlite3-journal", "host-id"):
        path = data_dir / name
        if path.exists():
            shutil.copy2(path, backup_dir / name)
    (backup_dir / "sha256.json").write_text(json.dumps({
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in backup_dir.iterdir() if path.is_file() and path.name != "sha256.json"
    }, sort_keys=True, indent=2) + "\n")

    stage = data_dir / f".segment-migration-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        segments = stage / "segments"; segments.mkdir()
        (stage / "quarantine").mkdir()
        now = int(time.time() * 1000)
        segment = segments / f"segment-{1:08d}-{now}-{now}.sqlite3"
        _logical_copy(backup_database, segment, 1, "sealed", now)
        active = stage / "reboot-trace.sqlite3"
        _create_active_from_segment(segment, active, now)
        segment.chmod(0o444)
        store = SegmentStore(stage, max(required, source.stat().st_size * 3), max(1024 * 1024, source.stat().st_size))
        active_connection = sqlite3.connect(active)
        try:
            store.rebuild_manifest(active_connection)
        finally:
            active_connection.close()
        final_segments = data_dir / "segments"
        final_quarantine = data_dir / "quarantine"
        for existing in (final_segments, final_quarantine):
            if existing.is_dir() and not any(existing.iterdir()):
                existing.rmdir()
        if final_segments.exists() or final_quarantine.exists() or (data_dir / "segments.json").exists():
            raise RuntimeError("segmented storage paths appeared during migration")
        state = {
            "format_version": 1,
            "stage": stage.name,
            "backup_dir": str(backup_dir),
            "phase": "prepared",
        }
        _write_state(data_dir, state)
        # Keep the exclusive SQLite lock through staging and all non-commit
        # renames. Closing immediately before the active rename is required for
        # portable replacement of the source file.
        for phase, staged, destination in (
            ("segments", segments, final_segments),
            ("quarantine", stage / "quarantine", final_quarantine),
            ("manifest", stage / "segments.json", data_dir / "segments.json"),
        ):
            _move_once(staged, destination)
            state["phase"] = phase
            _write_state(data_dir, state)
        guard.execute("ROLLBACK")
        guard.close()
        guard_open = False
        os.replace(active, source)
        state["phase"] = "active"
        _write_state(data_dir, state)
        completed = _resume_install(data_dir, state)
        result.update(completed)
        return result
    except Exception:
        if guard_open:
            if guard.in_transaction:
                guard.execute("ROLLBACK")
            guard.close()
        if not state_path.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate a stopped legacy reboot-trace SQLite database")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(migrate(args.data_dir, args.backup_dir, dry_run=args.dry_run), ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
