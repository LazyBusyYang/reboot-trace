import time
from dataclasses import replace
from reboot_trace.database import Repository
from reboot_trace.models import ProcessSample, SnapshotData


def sample(boot_id: str) -> SnapshotData:
    p=ProcessSample(1,10,0,1000,"alice","python","R",2,50.0,1024,0,1.0,2.0,3,4,"python x","abc",False,"no_sensitive_value",{"cpu":(1,50.0)})
    now=int(time.time()*1000)
    return SnapshotData(boot_id,now,now,10,None,{"host_cpu_percent":10.0},[p],[{"uid":1000,"username":"alice","cpu_percent":50.0,"rss_bytes":1024,"swap_bytes":0,"read_bps":1.0,"write_bps":2.0,"process_count":1,"thread_count":2}])


def test_lifecycle_service_restart_does_not_close_boot(settings):
    r=Repository(settings,"host","1.0")
    first=r.start_lifecycle("boot-a",1); r.stop_session(); second=r.start_lifecycle("boot-a",1)
    assert first == second
    assert r.db.execute("select termination from lifecycle where id=?",(first,)).fetchone()[0] == "active"
    r.close()


def test_stop_session_is_idempotent(settings):
    r=Repository(settings,"host","1.0")
    lifecycle_id=r.start_lifecycle("boot-a",1)
    r.stop_session();r.stop_session();r.close()
    check=Repository(settings,"host","1.1")
    assert check.db.execute("select count(*) from event where lifecycle_id=? and type='collector_stopped'",(lifecycle_id,)).fetchone()[0] == 1
    check.close()


def test_repository_reopen_preserves_host_and_active_lifecycle(settings):
    local=replace(settings,host_id_override=None)
    first_repo=Repository(local,"host","1.0")
    lifecycle_id=first_repo.start_lifecycle("boot-a",1)
    host_id=first_repo.host_id
    first_repo.write_snapshot(sample("boot-a"))
    first_repo.close()
    second_repo=Repository(local,"host","1.1")
    resumed=second_repo.start_lifecycle("boot-a",1)
    assert second_repo.host_id == host_id
    assert resumed == lifecycle_id
    assert second_repo.db.execute("select count(*) from service_session where lifecycle_id=?",(lifecycle_id,)).fetchone()[0] == 2
    assert second_repo.db.execute("select termination from lifecycle where id=?",(lifecycle_id,)).fetchone()[0] == "active"
    second_repo.close()


def test_missing_host_id_file_recovers_single_database_identity(settings):
    local=replace(settings,host_id_override=None)
    first=Repository(local,"host","1.0");first.start_lifecycle("boot-a",1);host_id=first.host_id;first.close()
    (settings.data_dir/"host-id").unlink()
    recovered=Repository(local,"host","1.1")
    assert recovered.host_id == host_id
    assert (settings.data_dir/"host-id").read_text().strip() == host_id
    recovered.close()


def test_new_boot_closes_old_and_snapshot_is_atomic(settings):
    r=Repository(settings,"host","1.0"); old=r.start_lifecycle("boot-a",1)
    snapshot_id=r.write_snapshot(sample("boot-a")); assert snapshot_id
    r.start_lifecycle("boot-b",2)
    assert r.db.execute("select termination from lifecycle where id=?",(old,)).fetchone()[0] == "unclean_or_unknown"
    assert r.db.execute("select count(*) from process_rank where snapshot_id=?",(snapshot_id,)).fetchone()[0] == 1
    final=r.db.execute("select detail_level from snapshot where id=?",(snapshot_id,)).fetchone()[0]
    assert final == "final"
    r.close()


def test_snapshot_hot_path_only_requests_reclaim(settings):
    r=Repository(settings,"host","1.0");r.start_lifecycle("boot-a",1)
    original=r.managed_bytes
    r.managed_bytes=lambda: int(settings.storage_limit_bytes*.9)  # type: ignore[method-assign]
    r.reclaim=lambda: (_ for _ in ()).throw(AssertionError("reclaim must not run in write_snapshot"))  # type: ignore[method-assign]
    r._choose_detail=lambda data,available:("normal",None)  # type: ignore[method-assign]
    assert r.write_snapshot(sample("boot-a"))
    assert r.reclaim_requested is True
    r.managed_bytes=original  # type: ignore[method-assign]
    r.close()


def test_space_planner_degrades_process_detail_before_pausing(settings):
    r=Repository(settings,"host","1.0")
    data=sample("boot-a")
    data.processes[0].cmdline_redacted="x" * 4096
    data.processes[0].ranks={dimension:(rank,1.0) for rank,dimension in enumerate(("cpu","rss","swap","read","write"),1)}
    full=r._estimated_snapshot_bytes(data,None)
    system_only=r._estimated_snapshot_bytes(data,0)
    assert r._choose_detail(data,full)==("normal",None)
    state=r._choose_detail(data,system_only)
    assert state==("system_only",0)
    assert r._choose_detail(data,system_only-1) is None
    r.close()


def test_snapshot_cannot_cross_lifecycle_boundary(settings):
    r=Repository(settings,"host","1.0")
    r.start_lifecycle("boot-a",1)
    data=sample("boot-b")
    try:
        r.write_snapshot(data)
        assert False, "mismatched boot_id must be rejected"
    except RuntimeError as error:
        assert "does not match" in str(error)
    assert r.db.execute("select count(*) from snapshot").fetchone()[0] == 0
    r.close()


def test_invalid_child_row_rolls_back_entire_snapshot(settings):
    r=Repository(settings,"host","1.0")
    r.start_lifecycle("boot-a",1)
    data=sample("boot-a")
    data.processes[0].redaction_status="invalid"
    try:
        r.write_snapshot(data)
        assert False, "invalid child row must fail"
    except Exception:
        pass
    assert r.db.execute("select count(*) from snapshot").fetchone()[0] == 0
    assert r.db.execute("select count(*) from system_sample").fetchone()[0] == 0
    assert r.db.execute("select count(*) from process_sample").fetchone()[0] == 0
    r.close()


def test_system_only_degradation_is_persisted_and_queryable(settings):
    r=Repository(settings,"host","1.0")
    r.start_lifecycle("boot-a",1)
    data=sample("boot-a")
    data.processes[0].cmdline_redacted="x"*4096
    minimum=r._estimated_snapshot_bytes(data,0)
    r.settings=replace(settings,storage_limit_bytes=2*1024*1024+minimum)
    original=r.managed_bytes
    r.managed_bytes=lambda:0  # type: ignore[method-assign]
    snapshot_id=r.write_snapshot(data)
    r.managed_bytes=original  # type: ignore[method-assign]
    assert snapshot_id
    row=r.db.execute("select detail_level,persistence_state from snapshot where id=?",(snapshot_id,)).fetchone()
    assert tuple(row)==("summary_only","system_only")
    assert r.db.execute("select count(*) from process_sample where snapshot_id=?",(snapshot_id,)).fetchone()[0] == 0
    assert r.db.execute("select count(*) from user_sample where snapshot_id=?",(snapshot_id,)).fetchone()[0] == 1
    r.close()


def test_schema_v1_database_migrates_without_losing_snapshots(settings):
    import sqlite3
    path=settings.data_dir/"reboot-trace.sqlite3"
    db=sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE host(id TEXT PRIMARY KEY,hostname TEXT NOT NULL,created_at_ms INTEGER NOT NULL);
      CREATE TABLE lifecycle(id INTEGER PRIMARY KEY,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,started_at_ms INTEGER NOT NULL,last_seen_at_ms INTEGER NOT NULL,ended_at_ms INTEGER,termination TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE snapshot(id INTEGER PRIMARY KEY,lifecycle_id INTEGER NOT NULL,captured_at_ms INTEGER NOT NULL,scheduled_at_ms INTEGER NOT NULL,duration_ms INTEGER NOT NULL,sample_interval_ms INTEGER,detail_level TEXT NOT NULL);
      INSERT INTO host VALUES('host-test','host',1);
      INSERT INTO lifecycle VALUES(1,'host-test','boot-a',1,2,NULL,'active','{}');
      INSERT INTO snapshot VALUES(1,1,2,2,1,NULL,'full');
      PRAGMA user_version=1;
    """)
    db.close()
    r=Repository(settings,"host","2.0")
    assert r.db.execute("pragma user_version").fetchone()[0] == 2
    assert r.db.execute("select persistence_state from snapshot where id=1").fetchone()[0] == "normal"
    r.close()


def test_schema_migration_refuses_insufficient_managed_budget(settings):
    import sqlite3
    path=settings.data_dir/"reboot-trace.sqlite3"
    db=sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE host(id TEXT PRIMARY KEY,hostname TEXT NOT NULL,created_at_ms INTEGER NOT NULL);
      CREATE TABLE lifecycle(id INTEGER PRIMARY KEY,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,started_at_ms INTEGER NOT NULL,last_seen_at_ms INTEGER NOT NULL,ended_at_ms INTEGER,termination TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE snapshot(id INTEGER PRIMARY KEY,lifecycle_id INTEGER NOT NULL,captured_at_ms INTEGER NOT NULL,scheduled_at_ms INTEGER NOT NULL,duration_ms INTEGER NOT NULL,sample_interval_ms INTEGER,detail_level TEXT NOT NULL);
      PRAGMA user_version=1;
    """);db.close()
    tiny=replace(settings,storage_limit_bytes=1024)
    try:
        Repository(tiny,"host","2.0")
        assert False,"migration must preserve the old schema when budget is insufficient"
    except RuntimeError as error:
        assert "insufficient space" in str(error)
    check=sqlite3.connect(path)
    assert "persistence_state" not in {row[1] for row in check.execute("pragma table_info(snapshot)")}
    assert check.execute("pragma user_version").fetchone()[0] == 1
    check.close()


def test_reopen_reports_paused_before_next_sampling_attempt(settings):
    local=replace(settings,storage_limit_bytes=2*1024*1024)
    r=Repository(local,"host","1.0");r.start_lifecycle("boot-a",1);r.close()
    reopened=Repository(local,"host","1.1");reopened.start_lifecycle("boot-a",1)
    assert reopened.persistence_state == "paused"
    reopened.close()
