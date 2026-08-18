from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import replace

import pytest

import reboot_trace.storage_migrate as storage_migrate
from reboot_trace.database import Repository
from reboot_trace.storage_migrate import migrate
from test_database import sample


def test_active_database_uses_delete_journal_and_no_auto_vacuum(settings):
    repo = Repository(settings, "host", "test")
    try:
        assert repo.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert repo.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        metadata = repo.db.execute("SELECT format_version,generation,state FROM storage_meta").fetchone()
        assert tuple(metadata) == (1, 1, "active")
        assert (settings.data_dir / "segments.json").is_file()
    finally:
        repo.close()


def test_rotation_preserves_global_ids_and_union_queries(settings):
    repo = Repository(settings, "host", "test")
    lifecycle_id = repo.start_lifecycle("boot-a", 1)
    first = repo.write_snapshot(sample("boot-a"))
    assert first
    assert repo._rotate() == 0
    second_data = sample("boot-a")
    second_data.captured_at_ms += 1_000
    second_data.scheduled_at_ms += 1_000
    second = repo.write_snapshot(second_data)
    assert second and second > first
    connection = repo.read_connection()
    try:
        assert connection.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()[0] == 2
        assert [row[0] for row in connection.execute("SELECT id FROM snapshot ORDER BY id")] == [first, second]
    finally:
        connection.close()
        repo.close()


def test_rotation_nulls_event_links_to_snapshots_outside_carry_window(settings):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    base = int(time.time() * 1000) - 30 * 60 * 1000
    linked_event_id = None
    linked_snapshot_id = None
    for index in range(14):
        data = sample("boot-a")
        data.captured_at_ms = base + index * 60_000
        data.scheduled_at_ms = data.captured_at_ms
        if index == 0:
            data.events = [{"type": "collector_error", "details": {"reason": "old"}}]
        snapshot_id = repo.write_snapshot(data)
        if index == 0:
            linked_snapshot_id = snapshot_id
            linked_event_id = repo.db.execute(
                "SELECT id FROM event WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()[0]
    recent = sample("boot-a")
    repo.write_snapshot(recent)

    repo._rotate()
    assert repo.db.execute("SELECT 1 FROM snapshot WHERE id=?", (linked_snapshot_id,)).fetchone() is None
    event = repo.db.execute("SELECT snapshot_id FROM event WHERE id=?", (linked_event_id,)).fetchone()
    assert event is not None and event[0] is None
    assert repo.db.execute("PRAGMA foreign_key_check").fetchall() == []
    repo.close()


def test_missing_manifest_is_rebuilt_from_database_metadata(settings):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.write_snapshot(sample("boot-a"))
    repo._rotate()
    repo.close()
    manifest = settings.data_dir / "segments.json"
    manifest.unlink()
    reopened = Repository(settings, "host", "test")
    try:
        payload = json.loads(manifest.read_text())
        assert payload["format_version"] == 1
        assert len(payload["segments"]) == 1
    finally:
        reopened.close()


def test_interrupted_staging_files_are_quarantined_and_counted(settings):
    repo = Repository(settings, "host", "test")
    repo.close()
    staging = settings.data_dir / "reboot-trace.sqlite3.next"
    manifest_staging = settings.data_dir / "segments.json.tmp"
    staging.write_bytes(b"incomplete database")
    manifest_staging.write_bytes(b"incomplete manifest")

    reopened = Repository(settings, "host", "test")
    try:
        quarantined = list((settings.data_dir / "quarantine").iterdir())
        assert len(quarantined) == 2
        assert not staging.exists()
        assert not manifest_staging.exists()
        assert reopened.managed_bytes() >= sum(path.stat().st_size for path in quarantined)
        assert reopened.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_rotation_refuses_when_peak_headroom_exceeds_budget(settings):
    repo = Repository(settings, "host", "test")
    try:
        active_size = repo.path.stat().st_size
        repo.segment_store.storage_limit_bytes = repo.managed_bytes() + active_size * 2
        with pytest.raises(RuntimeError, match="headroom"):
            repo.segment_store.prepare_rotation(active_size)
        staging = settings.data_dir / "reboot-trace.sqlite3.next"
        staging.write_bytes(b"x" * 4096)
        assert repo.managed_bytes() >= repo.path.stat().st_size + staging.stat().st_size
    finally:
        repo.close()


def test_current_v3_single_file_requires_explicit_migration(settings):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit(); connection.close()
    (settings.data_dir / "segments.json").unlink()
    with pytest.raises(RuntimeError, match="offline migration"):
        Repository(settings, "host", "test")


def test_offline_migration_preserves_ids_and_creates_backup(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    lifecycle_id = repo.start_lifecycle("boot-a", 1)
    base = int(time.time() * 1000) - 30 * 60 * 1000
    linked_event_id = None
    snapshot_id = None
    for index in range(15):
        data = sample("boot-a")
        data.captured_at_ms = base + index * 60_000
        data.scheduled_at_ms = data.captured_at_ms
        if index == 0:
            data.events = [{"type": "collector_error", "details": {"reason": "old"}}]
        current_id = repo.write_snapshot(data)
        if index == 0:
            snapshot_id = current_id
            linked_event_id = repo.db.execute(
                "SELECT id FROM event WHERE snapshot_id=?", (current_id,)
            ).fetchone()[0]
    repo.write_snapshot(sample("boot-a"))
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit(); connection.close()
    (settings.data_dir / "segments.json").unlink()
    dry = migrate(settings.data_dir, tmp_path / "backups", dry_run=True)
    assert dry["dry_run"] is True
    result = migrate(settings.data_dir, tmp_path / "backups")
    assert result["dry_run"] is False
    assert (settings.data_dir / "segments.json").is_file()
    assert (settings.data_dir / "segments").is_dir()
    migrated = Repository(replace(settings, segment_target_bytes=1024 * 1024), "host", "test")
    connection = migrated.read_connection()
    try:
        assert connection.execute("SELECT id FROM lifecycle WHERE lifecycle_key='boot-a'").fetchone()[0] == lifecycle_id
        assert connection.execute("SELECT id FROM snapshot WHERE id=?", (snapshot_id,)).fetchone()[0] == snapshot_id
        assert migrated.db.execute("SELECT snapshot_id FROM event WHERE id=?", (linked_event_id,)).fetchone()[0] is None
    finally:
        connection.close()
        migrated.close()


def test_offline_migration_resumes_after_install_interruption(settings, tmp_path, monkeypatch):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.write_snapshot(sample("boot-a"))
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit()
    connection.close()
    (settings.data_dir / "segments.json").unlink()

    original_move = storage_migrate._move_once
    interrupted = False

    def fail_after_segments(source, destination):
        nonlocal interrupted
        if destination.name == "quarantine" and not interrupted:
            interrupted = True
            raise OSError("injected install interruption")
        return original_move(source, destination)

    monkeypatch.setattr(storage_migrate, "_move_once", fail_after_segments)
    with pytest.raises(OSError, match="injected"):
        migrate(settings.data_dir, tmp_path / "backups")
    assert (settings.data_dir / ".segment-migration.json").is_file()
    assert (settings.data_dir / "segments").is_dir()

    monkeypatch.setattr(storage_migrate, "_move_once", original_move)
    result = migrate(settings.data_dir, tmp_path / "backups")
    assert result["resumed"] is True
    assert not (settings.data_dir / ".segment-migration.json").exists()
    reopened = Repository(settings, "host", "test")
    reopened.close()
