from dataclasses import replace

import pytest

from reboot_trace.database import Repository
from test_database import sample


def write_samples(repo: Repository, boot_id: str, count: int, *, start: int = 1_000_000, step: int = 5_000):
    ids=[]
    for index in range(count):
        data=sample(boot_id);data.captured_at_ms=start+index*step;data.scheduled_at_ms=data.captured_at_ms
        ids.append(repo.write_snapshot(data))
    return ids


def test_active_lifecycle_is_bounded_to_twelve_full_and_twelve_trend(settings):
    repo=Repository(settings,"host","test");lifecycle_id=repo.start_lifecycle("active",1)
    write_samples(repo,"active",1_000,step=30_000)
    rows=repo.db.execute(
        "SELECT id,detail_level FROM snapshot WHERE lifecycle_id=? ORDER BY captured_at_ms DESC",(lifecycle_id,)
    ).fetchall()
    assert len(rows)==24
    assert sum(row["detail_level"]!="summary_only" for row in rows)==12
    assert sum(row["detail_level"]=="summary_only" for row in rows)==12
    trend_ids=[row["id"] for row in rows if row["detail_level"]=="summary_only"]
    assert all(repo.db.execute("SELECT count(*) FROM process_sample WHERE snapshot_id=?",(snapshot_id,)).fetchone()[0]==0 for snapshot_id in trend_ids)
    assert all(repo.db.execute("SELECT count(*) FROM user_sample WHERE snapshot_id=?",(snapshot_id,)).fetchone()[0]==0 for snapshot_id in trend_ids)
    repo.close()


def test_removed_snapshot_keeps_event_with_null_link(settings):
    repo=Repository(settings,"host","test");repo.start_lifecycle("active",1)
    first=sample("active");first.captured_at_ms=1_000;first.scheduled_at_ms=1_000
    first.events=[{"type":"collector_error","details":{"reason":"old"}}]
    snapshot_id=repo.write_snapshot(first)
    event_id=repo.db.execute("SELECT id FROM event WHERE snapshot_id=?",(snapshot_id,)).fetchone()[0]
    write_samples(repo,"active",40,start=1_000_000,step=300_000)
    assert repo.db.execute("SELECT 1 FROM snapshot WHERE id=?",(snapshot_id,)).fetchone() is None
    assert repo.db.execute("SELECT snapshot_id FROM event WHERE id=?",(event_id,)).fetchone()[0] is None
    repo.close()


def test_trend_snapshots_do_not_reach_beyond_the_recent_hour(settings):
    repo=Repository(settings,"host","test");lifecycle_id=repo.start_lifecycle("active",1)
    write_samples(repo,"active",20,start=1_000_000,step=5_000)
    write_samples(repo,"active",20,start=10_000_000,step=5_000)
    oldest=repo.db.execute(
        "SELECT min(captured_at_ms) FROM snapshot WHERE lifecycle_id=? AND detail_level='summary_only'",(lifecycle_id,)
    ).fetchone()[0]
    newest=repo.db.execute("SELECT max(captured_at_ms) FROM snapshot WHERE lifecycle_id=?",(lifecycle_id,)).fetchone()[0]
    assert oldest is None or oldest >= newest-3_600_000
    repo.close()


def test_completed_lifecycle_has_immutable_capsule_and_twelve_final(settings):
    repo=Repository(settings,"host","test");old=repo.start_lifecycle("old",1)
    write_samples(repo,"old",800,start=1_000_000,step=5_000)
    repo.start_lifecycle("current",2)
    capsule=repo.segment_store.evidence_for(old)
    assert capsule and capsule.stat().st_mode & 0o222 == 0
    assert repo.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(old,)).fetchone()[0]==0
    connection=repo.read_connection(lifecycle_ref="old")
    try:
        full,trend=repo._snapshot_counts(connection,old)
        assert (full,trend)==(12,12)
        assert connection.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=? AND detail_level='final'",(old,)).fetchone()[0]==12
    finally:connection.close()
    repo.close()


def test_many_restarts_keep_independent_capsules_and_monotonic_ids(settings):
    repo=Repository(replace(settings,storage_limit_bytes=64*1024*1024),"host","test")
    snapshot_ids=[]
    for index in range(15):
        repo.start_lifecycle(f"boot-{index}",index+1)
        snapshot_ids.extend(write_samples(repo,f"boot-{index}",12,start=1_000_000+index*1_000_000))
    repo.start_lifecycle("current",100)
    assert len(repo.segment_store.evidence())==15
    assert snapshot_ids==sorted(snapshot_ids) and len(snapshot_ids)==len(set(snapshot_ids))
    for index in range(15):
        connection=repo.read_connection(lifecycle_ref=f"boot-{index}")
        try:assert connection.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(index+1,)).fetchone()[0]==12
        finally:connection.close()
    repo.close()


def test_budget_cleanup_deletes_oldest_whole_capsule_and_fixes_summary(settings):
    repo=Repository(replace(settings,storage_limit_bytes=64*1024*1024),"host","test")
    lifecycle_ids=[]
    for index in range(3):
        lifecycle_ids.append(repo.start_lifecycle(f"old-{index}",index+1))
        write_samples(repo,f"old-{index}",12,start=1_000_000+index*1_000_000)
    repo.start_lifecycle("current",10)
    first_path=repo.segment_store.evidence_for(lifecycle_ids[0]);assert first_path
    repo.settings=replace(repo.settings,storage_limit_bytes=repo.managed_bytes()-1)
    repo._prune_evidence()
    assert repo.segment_store.evidence_for(lifecycle_ids[0]) is None
    summary=repo.db.execute("SELECT summary_json FROM lifecycle WHERE id=?",(lifecycle_ids[0],)).fetchone()[0]
    assert '"retention_state":"summary_only"' in summary
    assert repo.segment_store.evidence_for(lifecycle_ids[-1]) is not None
    repo.close()


@pytest.mark.parametrize("phase",["created","validated","fsynced","renamed","manifest"])
def test_sealing_recovers_without_losing_the_only_copy(settings,monkeypatch,phase):
    class SimulatedProcessLoss(BaseException):
        pass

    repo=Repository(settings,"host","test");old=repo.start_lifecycle("old",1)
    write_samples(repo,"old",12)

    def interrupt(current):
        if current==phase:raise SimulatedProcessLoss()

    monkeypatch.setattr(repo,"_evidence_checkpoint",interrupt)
    with pytest.raises(SimulatedProcessLoss):repo.start_lifecycle("current",2)
    repo.db.close()

    reopened=Repository(settings,"host","test")
    try:
        capsule=reopened.segment_store.evidence_for(old)
        assert capsule is not None
        assert reopened.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(old,)).fetchone()[0]==0
        history=reopened.read_connection(lifecycle_ref="old")
        try:assert history.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(old,)).fetchone()[0]==12
        finally:history.close()
    finally:reopened.close()


def test_failed_seal_keeps_old_details_and_pauses_new_sampling(settings):
    repo=Repository(settings,"host","test");old=repo.start_lifecycle("old",1)
    write_samples(repo,"old",12)
    repo.settings=replace(repo.settings,storage_limit_bytes=repo.managed_bytes()+1024*1024)
    repo.segment_store.storage_limit_bytes=repo.settings.storage_limit_bytes
    repo.start_lifecycle("current",2)
    assert repo.persistence_state=="paused"
    assert repo.db.execute("SELECT count(*) FROM snapshot WHERE lifecycle_id=?",(old,)).fetchone()[0]==12
    assert repo.segment_store.evidence_for(old) is None
    assert repo.write_snapshot(sample("current")) is None
    repo.reclaim()
    assert repo.persistence_state=="paused"
    repo.close()
