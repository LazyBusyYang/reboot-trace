from __future__ import annotations

import json
import os
import sqlite3
from collections import namedtuple
from dataclasses import replace
from pathlib import Path

import pytest

import reboot_trace.storage_repack as storage_repack
from reboot_trace.database import Repository
from reboot_trace.storage_repack import repack
from test_database import sample


def test_active_database_uses_format_v2_delete_journal_and_no_auto_vacuum(settings):
    repo = Repository(settings, "host", "test")
    try:
        assert repo.db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert repo.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        metadata = repo.db.execute("SELECT format_version,generation,state FROM storage_meta").fetchone()
        assert tuple(metadata) == (2, 1, "active")
        payload = json.loads((settings.data_dir / "evidence.json").read_text())
        assert payload["format_version"] == 2
        assert payload["evidence"] == []
    finally:
        repo.close()


def test_lifecycle_sealing_preserves_global_ids_and_opens_one_capsule(settings):
    repo = Repository(settings, "host", "test")
    lifecycle_id = repo.start_lifecycle("boot-a", 1)
    snapshot_id = repo.write_snapshot(sample("boot-a"))
    repo.start_lifecycle("boot-b", 2)
    try:
        assert repo.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()[0] == 0
        capsule = repo.segment_store.evidence_for(lifecycle_id)
        assert capsule is not None and capsule.is_file()
        connection = repo.read_connection(lifecycle_ref="boot-a")
        try:
            assert [row[1] for row in connection.execute("PRAGMA database_list")] == ["main"]
            assert connection.execute("SELECT id FROM snapshot WHERE lifecycle_id=?", (lifecycle_id,)).fetchone()[0] == snapshot_id
        finally:
            connection.close()
        assert repo.db.execute("SELECT next_id FROM storage_counter WHERE name='snapshot'").fetchone()[0] > snapshot_id
    finally:
        repo.close()


def test_missing_manifest_is_rebuilt_from_capsule_metadata(settings):
    repo = Repository(settings, "host", "test")
    lifecycle_id = repo.start_lifecycle("boot-a", 1)
    repo.write_snapshot(sample("boot-a"))
    repo.start_lifecycle("boot-b", 2)
    repo.close()
    manifest = settings.data_dir / "evidence.json"
    manifest.unlink()

    reopened = Repository(settings, "host", "test")
    try:
        payload = json.loads(manifest.read_text())
        assert payload["format_version"] == 2
        assert [item["lifecycle_id"] for item in payload["evidence"]] == [lifecycle_id]
    finally:
        reopened.close()


def test_interrupted_invalid_capsule_and_manifest_are_quarantined(settings):
    repo = Repository(settings, "host", "test")
    repo.close()
    staging = settings.data_dir / "evidence" / "lifecycle-000000000001-invalid.sqlite3.next"
    manifest_staging = settings.data_dir / "evidence.json.tmp"
    staging.write_bytes(b"incomplete database")
    manifest_staging.write_bytes(b"incomplete manifest")

    reopened = Repository(settings, "host", "test")
    try:
        names = [path.name for path in (settings.data_dir / "quarantine").iterdir()]
        assert any("invalid.sqlite3.next.interrupted" in name for name in names)
        assert any("evidence.json.tmp.interrupted" in name for name in names)
        assert not staging.exists()
        assert not manifest_staging.exists()
    finally:
        reopened.close()


def test_format_v1_requires_explicit_storage_repack(settings):
    repo = Repository(settings, "host", "test")
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("UPDATE storage_meta SET format_version=1")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="storage_repack"):
        Repository(settings, "host", "test")


def test_offline_repack_dry_run_backup_and_id_preservation(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    lifecycle_id = repo.start_lifecycle("boot-a", 1)
    snapshot_id = repo.write_snapshot(sample("boot-a"))
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit()
    connection.close()
    (settings.data_dir / "evidence.json").unlink()

    backups = tmp_path / "backups"
    dry = repack(settings.data_dir, backups, dry_run=True)
    assert dry["dry_run"] is True
    result = repack(settings.data_dir, backups)
    assert result["dry_run"] is False
    assert result["resumed"] is False
    assert (Path(str(result["backup_dir"])) / "sha256.json").is_file()

    reopened = Repository(replace(settings, storage_limit_bytes=32 * 1024 * 1024), "host", "test")
    try:
        assert reopened.db.execute("SELECT id FROM lifecycle WHERE lifecycle_key='boot-a'").fetchone()[0] == lifecycle_id
        assert reopened.db.execute("SELECT id FROM snapshot WHERE id=?", (snapshot_id,)).fetchone()[0] == snapshot_id
    finally:
        reopened.close()


def test_offline_repack_resumes_after_atomic_install_interruption(settings, tmp_path, monkeypatch):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.write_snapshot(sample("boot-a"))
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit()
    connection.close()
    (settings.data_dir / "evidence.json").unlink()

    original = storage_repack._install_checkpoint
    interrupted = False

    def fail_after_old_layout_moved(phase: str) -> None:
        nonlocal interrupted
        if phase == "old_moved" and not interrupted:
            interrupted = True
            raise OSError("injected install interruption")

    monkeypatch.setattr(storage_repack, "_install_checkpoint", fail_after_old_layout_moved)
    with pytest.raises(OSError, match="injected"):
        repack(settings.data_dir, tmp_path / "backups")
    assert (settings.data_dir / storage_repack.STATE_FILE).is_file()

    monkeypatch.setattr(storage_repack, "_install_checkpoint", original)
    result = repack(settings.data_dir, tmp_path / "backups")
    assert result["resumed"] is True
    assert not (settings.data_dir / storage_repack.STATE_FILE).exists()
    reopened = Repository(settings, "host", "test")
    reopened.close()


def test_repack_refuses_an_already_format_v2_layout(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    repo.close()
    with pytest.raises(RuntimeError, match="already format v2"):
        repack(settings.data_dir, tmp_path / "backups")


def test_format_v1_segments_become_per_lifecycle_capsules(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    old_id = repo.start_lifecycle("boot-a", 1)
    old_snapshot = repo.write_snapshot(sample("boot-a"))
    repo.start_lifecycle("boot-b", 2)
    old_capsule = repo.segment_store.evidence_for(old_id)
    assert old_capsule is not None
    repo.close()

    segments = settings.data_dir / "segments"
    segments.mkdir()
    old_capsule.chmod(0o600)
    os.replace(old_capsule, segments / "segment-000001.sqlite3")
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("UPDATE storage_meta SET format_version=1")
    connection.commit()
    connection.close()
    (settings.data_dir / "evidence.json").unlink()
    (settings.data_dir / "segments.json").write_text("{}\n")

    result = repack(settings.data_dir, tmp_path / "backups")
    assert result["source_format"] == 1
    reopened = Repository(settings, "host", "test")
    history = reopened.read_connection(lifecycle_ref="boot-a")
    try:
        assert history.execute("SELECT id FROM snapshot WHERE id=?", (old_snapshot,)).fetchone()[0] == old_snapshot
        assert reopened.segment_store.evidence_for(old_id) is not None
    finally:
        history.close()
        reopened.close()


def test_repack_corrects_false_final_state_when_old_segment_is_missing(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    old_id = repo.start_lifecycle("boot-a", 1)
    repo.write_snapshot(sample("boot-a"))
    repo.start_lifecycle("boot-b", 2)
    old_capsule = repo.segment_store.evidence_for(old_id)
    assert old_capsule is not None
    old_capsule.unlink()
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("UPDATE storage_meta SET format_version=1")
    connection.commit()
    connection.close()
    (settings.data_dir / "evidence.json").unlink()
    (settings.data_dir / "segments").mkdir()
    (settings.data_dir / "segments.json").write_text("{}\n")

    repack(settings.data_dir, tmp_path / "backups")
    reopened = Repository(settings, "host", "test")
    try:
        summary = json.loads(reopened.db.execute("SELECT summary_json FROM lifecycle WHERE id=?", (old_id,)).fetchone()[0])
        assert summary["retention_state"] == "summary_only"
        assert summary["full_snapshot_count"] == 0
        assert summary["trend_snapshot_count"] == 0
    finally:
        reopened.close()


def test_repack_rejects_an_active_writer(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit()
    (settings.data_dir / "evidence.json").unlink()
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="database is in use"):
            repack(settings.data_dir, tmp_path / "backups")
    finally:
        connection.rollback()
        connection.close()


def test_repack_rejects_insufficient_filesystem_space(settings, tmp_path, monkeypatch):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("DROP TABLE storage_counter")
    connection.execute("DROP TABLE storage_meta")
    connection.commit()
    connection.close()
    (settings.data_dir / "evidence.json").unlink()
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(storage_repack.shutil, "disk_usage", lambda _path: usage(1, 1, 0))
    with pytest.raises(RuntimeError, match="insufficient filesystem space"):
        repack(settings.data_dir, tmp_path / "backups")


def test_repack_rejects_a_corrupt_legacy_segment(settings, tmp_path):
    repo = Repository(settings, "host", "test")
    repo.start_lifecycle("boot-a", 1)
    repo.close()
    connection = sqlite3.connect(settings.data_dir / "reboot-trace.sqlite3")
    connection.execute("UPDATE storage_meta SET format_version=1")
    connection.commit()
    connection.close()
    (settings.data_dir / "evidence.json").unlink()
    segments = settings.data_dir / "segments"
    segments.mkdir()
    (segments / "segment-000001.sqlite3").write_bytes(b"not a sqlite database")
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        repack(settings.data_dir, tmp_path / "backups")
