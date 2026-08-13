import json
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


def test_repository_rejects_data_on_container_root_overlay(settings):
    import pytest
    unsafe=replace(settings,data_dir=settings.project_dir/"ephemeral-data")
    with pytest.raises(RuntimeError,match="container ephemeral root filesystem"):
        Repository(unsafe,"host","test")


def test_repository_rejects_data_and_marker_on_same_mount(settings):
    import pytest
    unsafe=replace(settings,instance_marker_path=settings.data_dir/"marker")
    with pytest.raises(RuntimeError,match="must not use the same mount"):
        Repository(unsafe,"host","test")


def identity(instance: str, ticks: int = 100, kernel_boot: str = "kernel-a") -> dict:
    return {"container_instance_id":instance,"pid1_start_ticks":ticks,"pid_namespace_inode":42,"cgroup_hash":"abc","fingerprint":f"fp-{ticks}","preferred_key":instance,"kernel_boot_id":kernel_boot}


def test_container_instance_change_creates_lifecycle_on_same_kernel_boot(settings):
    r=Repository(settings,"host","test")
    first=r.start_lifecycle("kernel-a",1,identity("instance-a",100))
    second=r.start_lifecycle("kernel-a",2,identity("instance-b",200),start_session=False)
    assert first != second
    assert r.db.execute("select count(*) from lifecycle where boot_id='kernel-a'").fetchone()[0] == 2
    lifecycle=r.db.execute("select detection_method,detection_confidence from lifecycle where id=?",(second,)).fetchone()
    assert tuple(lifecycle)==("instance_marker_pid1","confirmed")
    assert r.db.execute("select type from event where lifecycle_id=? order by id desc",(second,)).fetchone()[0] == "container_instance_changed"
    r.close()


def test_marker_change_without_pid1_change_is_probable(settings):
    r=Repository(settings,"host","test")
    first=r.start_lifecycle("kernel-a",1,identity("instance-a",100))
    second=r.start_lifecycle("kernel-a",2,identity("instance-b",100),start_session=False)
    assert first != second
    row=r.db.execute("select detection_method,detection_confidence from lifecycle where id=?",(second,)).fetchone()
    assert tuple(row)==("instance_marker","probable")
    assert r.db.execute("select type from event where lifecycle_id=? order by id desc",(second,)).fetchone()[0]=="container_instance_changed"
    r.close()


def test_kernel_boot_change_alone_keeps_container_lifecycle(settings):
    r=Repository(settings,"host","test")
    first=r.start_lifecycle("kernel-a",1,identity("instance-a"))
    second=r.start_lifecycle("kernel-b",2,identity("instance-a"),start_session=False)
    assert first == second
    assert r.db.execute("select boot_id from lifecycle where id=?",(first,)).fetchone()[0] == "kernel-b"
    r.close()


def test_pid1_change_without_marker_is_probable_restart(settings):
    r=Repository(settings,"host","test")
    first_identity=identity("",100);first_identity["container_instance_id"]=None;first_identity["preferred_key"]="pid1-a"
    second_identity=identity("",200);second_identity["container_instance_id"]=None;second_identity["preferred_key"]="pid1-b"
    first=r.start_lifecycle("kernel-a",1,first_identity)
    second=r.start_lifecycle("kernel-a",2,second_identity,start_session=False)
    assert first != second
    row=r.db.execute("select detection_method,detection_confidence from lifecycle where id=?",(second,)).fetchone()
    assert tuple(row)==("pid1_fingerprint","probable")
    r.close()


def test_pid1_change_with_same_marker_is_identity_conflict_and_probable(settings):
    r=Repository(settings,"host","test")
    first=r.start_lifecycle("kernel-a",1,identity("instance-a",100))
    second=r.start_lifecycle("kernel-a",2,identity("instance-a",200),start_session=False)
    assert first != second
    row=r.db.execute("select detection_method,detection_confidence from lifecycle where id=?",(second,)).fetchone()
    assert tuple(row)==("pid1_fingerprint","probable")
    event=r.db.execute("select type,details_json from event where lifecycle_id=? order by id desc",(second,)).fetchone()
    assert event["type"]=="identity_conflict"
    assert json.loads(event["details_json"])["marker_changed"] is False
    assert json.loads(event["details_json"])["pid1_fingerprint_changed"] is True
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
    assert r.db.execute("pragma user_version").fetchone()[0] == 3
    assert r.db.execute("select persistence_state from snapshot where id=1").fetchone()[0] == "normal"
    assert r.db.execute("select lifecycle_key from lifecycle where id=1").fetchone()[0] == "boot-a"
    r.close()


def test_schema_v2_database_migrates_legacy_boot_to_lifecycle_key(settings):
    import sqlite3
    path=settings.data_dir/"reboot-trace.sqlite3"
    db=sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE host(id TEXT PRIMARY KEY,hostname TEXT NOT NULL,created_at_ms INTEGER NOT NULL);
      CREATE TABLE lifecycle(id INTEGER PRIMARY KEY,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,started_at_ms INTEGER NOT NULL,last_seen_at_ms INTEGER NOT NULL,ended_at_ms INTEGER,termination TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}',UNIQUE(host_id,boot_id));
      CREATE TABLE snapshot(id INTEGER PRIMARY KEY,lifecycle_id INTEGER NOT NULL,captured_at_ms INTEGER NOT NULL,scheduled_at_ms INTEGER NOT NULL,duration_ms INTEGER NOT NULL,sample_interval_ms INTEGER,detail_level TEXT NOT NULL,persistence_state TEXT NOT NULL DEFAULT 'normal');
      CREATE TABLE system_sample(snapshot_id INTEGER PRIMARY KEY,data_json TEXT NOT NULL);
      INSERT INTO host VALUES('host-test','host',1);
      INSERT INTO lifecycle VALUES(1,'host-test','legacy-boot',1,2,NULL,'active','{}');
      INSERT INTO snapshot VALUES(7,1,2,2,1,NULL,'full','normal');
      INSERT INTO system_sample VALUES(7,'{"process_count":0,"thread_count":0,"uptime_seconds":1}');
      PRAGMA user_version=2;
    """);db.close()
    repo=Repository(settings,"host","3.0")
    lifecycle=repo.db.execute("select lifecycle_key,boot_id,detection_method from lifecycle where id=1").fetchone()
    assert tuple(lifecycle)==("legacy-boot","legacy-boot","legacy")
    assert repo.db.execute("select count(*) from snapshot where lifecycle_id=1").fetchone()[0]==1
    assert repo.db.execute("pragma foreign_key_check").fetchall()==[]
    repo.close()


def test_schema_v3_migration_failure_rolls_back_all_table_changes(settings,monkeypatch):
    import sqlite3
    import pytest
    path=settings.data_dir/"reboot-trace.sqlite3"
    db=sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE host(id TEXT PRIMARY KEY,hostname TEXT NOT NULL,created_at_ms INTEGER NOT NULL);
      CREATE TABLE lifecycle(id INTEGER PRIMARY KEY,host_id TEXT NOT NULL,boot_id TEXT NOT NULL,started_at_ms INTEGER NOT NULL,last_seen_at_ms INTEGER NOT NULL,ended_at_ms INTEGER,termination TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE snapshot(id INTEGER PRIMARY KEY,lifecycle_id INTEGER NOT NULL,captured_at_ms INTEGER NOT NULL,scheduled_at_ms INTEGER NOT NULL,duration_ms INTEGER NOT NULL,sample_interval_ms INTEGER,detail_level TEXT NOT NULL);
      CREATE TABLE event(id INTEGER PRIMARY KEY,lifecycle_id INTEGER NOT NULL,snapshot_id INTEGER,occurred_at_ms INTEGER NOT NULL,type TEXT NOT NULL CHECK(type IN ('collector_started')),details_json TEXT NOT NULL);
      INSERT INTO host VALUES('host-test','host',1);
      INSERT INTO lifecycle VALUES(1,'host-test','legacy-boot',1,2,NULL,'active','{}');
      INSERT INTO snapshot VALUES(7,1,2,2,1,NULL,'full');
      INSERT INTO event VALUES(9,1,NULL,2,'collector_started','{}');
      PRAGMA user_version=1;
    """);db.close()
    original=Repository._migration_checkpoint
    def fail_after_lifecycle(self,stage):
        if stage=="after_lifecycle":raise RuntimeError("injected migration failure")
    monkeypatch.setattr(Repository,"_migration_checkpoint",fail_after_lifecycle)
    with pytest.raises(RuntimeError,match="injected migration failure"):
        Repository(settings,"host","3.0")
    check=sqlite3.connect(path)
    assert "lifecycle_key" not in {row[1] for row in check.execute("pragma table_info(lifecycle)")}
    assert "persistence_state" not in {row[1] for row in check.execute("pragma table_info(snapshot)")}
    assert check.execute("pragma user_version").fetchone()[0]==1
    assert check.execute("select count(*) from event").fetchone()[0]==1
    check.close()
    monkeypatch.setattr(Repository,"_migration_checkpoint",original)
    recovered=Repository(settings,"host","3.0")
    assert recovered.db.execute("pragma user_version").fetchone()[0]==3
    assert recovered.db.execute("select count(*) from event").fetchone()[0]==1
    recovered.close()


def test_schema_v3_repairs_mixed_lifecycle_and_legacy_event_schema(settings):
    import sqlite3
    path=settings.data_dir/"reboot-trace.sqlite3"
    db=sqlite3.connect(path)
    db.executescript("""
      CREATE TABLE host(id TEXT PRIMARY KEY,hostname TEXT NOT NULL,created_at_ms INTEGER NOT NULL);
      CREATE TABLE lifecycle(
        id INTEGER PRIMARY KEY,host_id TEXT NOT NULL,lifecycle_key TEXT NOT NULL,boot_id TEXT NOT NULL,container_instance_id TEXT,
        pid1_start_ticks INTEGER,pid_namespace_inode INTEGER,cgroup_hash TEXT,detection_method TEXT NOT NULL DEFAULT 'legacy',
        detection_confidence TEXT NOT NULL DEFAULT 'unknown',identity_first_observed_at_ms INTEGER,started_at_ms INTEGER NOT NULL,
        last_seen_at_ms INTEGER NOT NULL,ended_at_ms INTEGER,termination TEXT NOT NULL,summary_json TEXT NOT NULL DEFAULT '{}',UNIQUE(host_id,lifecycle_key));
      CREATE TABLE event(id INTEGER PRIMARY KEY,lifecycle_id INTEGER NOT NULL,snapshot_id INTEGER,occurred_at_ms INTEGER NOT NULL,type TEXT NOT NULL CHECK(type IN ('collector_started')),details_json TEXT NOT NULL);
      INSERT INTO host VALUES('host-test','host',1);
      INSERT INTO lifecycle VALUES(1,'host-test','instance-a','kernel-a',NULL,NULL,NULL,NULL,'legacy','unknown',NULL,1,2,NULL,'active','{}');
      INSERT INTO event VALUES(1,1,NULL,2,'collector_started','{}');
      PRAGMA user_version=2;
    """);db.close()
    repo=Repository(settings,"host","3.0")
    event_sql=repo.db.execute("select sql from sqlite_master where name='event'").fetchone()[0]
    assert "container_instance_changed" in event_sql
    repo.db.execute("insert into event(lifecycle_id,occurred_at_ms,type,details_json) values(1,3,'container_instance_changed','{}')")
    repo.db.commit()
    assert repo.db.execute("pragma foreign_key_check").fetchall()==[]
    repo.close()


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
