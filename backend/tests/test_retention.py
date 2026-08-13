from dataclasses import replace
from reboot_trace.database import Repository
from test_database import sample


def test_historical_final_window_is_preserved_when_downsampling(settings):
    repo=Repository(settings,"host","test");repo.start_lifecycle("old",1)
    base=1_000_000
    ids=[]
    for offset in (0,10_000,70_000,700_000,710_000):
        data=sample("old");data.captured_at_ms=base+offset;data.scheduled_at_ms=base+offset
        ids.append(repo.write_snapshot(data))
    repo.start_lifecycle("new",2)
    # Force reclaim entry without making the DB itself invalid.
    original=repo.managed_bytes
    calls=iter([settings.storage_limit_bytes,0,0,0,0])
    repo.managed_bytes=lambda: next(calls,0)  # type: ignore[method-assign]
    repo.reclaim();repo.managed_bytes=original  # type: ignore[method-assign]
    old=repo.db.execute("select id,detail_level,captured_at_ms from snapshot where lifecycle_id=? order by captured_at_ms",(1,)).fetchall()
    final_window=[x for x in old if x["captured_at_ms"]>=base+110_000]
    assert final_window and all(x["detail_level"] in {"full","final"} for x in final_window)
    assert sum(x["detail_level"]=="final" for x in final_window) == 2
    assert all(x["detail_level"]=="final" for x in old)
    summary=repo.db.execute("select summary_json from lifecycle where id=1").fetchone()[0]
    assert '"retention_state":"final_partial"' in summary
    repo.close()


def test_old_final_is_downgraded_before_newest_historical_final(settings):
    repo=Repository(settings,"host","test")
    for index,boot_id in enumerate(("oldest","newest","current"),1):
        repo.start_lifecycle(boot_id,index)
        if boot_id != "current":
            data=sample(boot_id);data.captured_at_ms=index*1000;data.scheduled_at_ms=index*1000
            repo.write_snapshot(data)
    original=repo.managed_bytes
    sizes=iter([settings.storage_limit_bytes,settings.storage_limit_bytes,0,0,0])
    repo.managed_bytes=lambda:next(sizes,0)  # type: ignore[method-assign]
    repo.reclaim();repo.managed_bytes=original  # type: ignore[method-assign]
    levels={row["boot_id"]:row["detail_level"] for row in repo.db.execute("SELECT l.boot_id,s.detail_level FROM snapshot s JOIN lifecycle l ON l.id=s.lifecycle_id")}
    assert levels == {"newest":"final"}
    repo.close()


def test_final_only_pressure_is_recoverable_and_current_lifecycle_can_write(settings):
    repo=Repository(settings,"host","test")
    for index,boot_id in enumerate(("old-a","old-b","current"),1):
        repo.start_lifecycle(boot_id,index)
        if boot_id != "current":
            data=sample(boot_id);data.captured_at_ms=index*1000;data.scheduled_at_ms=index*1000
            repo.write_snapshot(data)
    original=repo.managed_bytes
    repo.managed_bytes=lambda: settings.storage_limit_bytes if repo.db.execute("SELECT 1 FROM snapshot WHERE detail_level='final' LIMIT 1").fetchone() else 0  # type: ignore[method-assign]
    repo.reclaim()
    assert repo.db.execute("SELECT count(*) FROM snapshot WHERE detail_level='final'").fetchone()[0] == 1
    assert repo.persistence_state == "paused"
    repo.managed_bytes=original  # type: ignore[method-assign]
    assert repo.write_snapshot(sample("current"))
    repo.close()


def test_recent_completed_lifecycle_keeps_twelve_complete_final_snapshots(settings):
    repo=Repository(settings,"host","test");repo.start_lifecycle("old",1)
    for index in range(20):
        data=sample("old");data.captured_at_ms=1_000_000+index*5000;data.scheduled_at_ms=data.captured_at_ms
        assert repo.write_snapshot(data)
    repo.start_lifecycle("current",2)
    original=repo.managed_bytes
    repo.managed_bytes=lambda: settings.storage_limit_bytes if repo.db.execute("select count(*) from snapshot where lifecycle_id=1").fetchone()[0]>12 else 0  # type: ignore[method-assign]
    repo.reclaim();repo.managed_bytes=original  # type: ignore[method-assign]
    rows=repo.db.execute("select detail_level,persistence_state from snapshot where lifecycle_id=1 order by captured_at_ms").fetchall()
    assert len(rows)==12
    assert all(tuple(row)==("final","normal") for row in rows)
    repo.close()
