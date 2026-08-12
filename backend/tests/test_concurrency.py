from concurrent.futures import ThreadPoolExecutor
import time
from reboot_trace.database import Repository
from test_database import sample


def test_concurrent_status_and_snapshot_writes(settings):
    repo=Repository(settings,"host","test");repo.start_lifecycle("boot",1)
    def write():
        for _ in range(20): assert repo.write_snapshot(sample("boot"))
    def read():
        for _ in range(100): assert repo.status()["boot_id"] == "boot"
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(write),pool.submit(read),pool.submit(read)]
        for future in futures: future.result()
    assert repo.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    repo.close()


def test_active_reader_does_not_make_reclaim_or_sampling_wait(settings):
    repo=Repository(settings,"host","test");repo.start_lifecycle("old",1)
    for captured in (1_000,2_000,700_000):
        data=sample("old");data.captured_at_ms=captured;data.scheduled_at_ms=captured
        repo.write_snapshot(data)
    repo.start_lifecycle("new",2)
    reader=repo.read_connection();reader.execute("BEGIN");reader.execute("SELECT * FROM snapshot").fetchone()
    original=repo.managed_bytes
    sizes=iter([settings.storage_limit_bytes,settings.storage_limit_bytes,0,0,0])
    repo.managed_bytes=lambda:next(sizes,0)  # type: ignore[method-assign]
    started=time.monotonic();repo.reclaim();elapsed=time.monotonic()-started
    repo.managed_bytes=original  # type: ignore[method-assign]
    assert elapsed < .5
    started=time.monotonic();assert repo.write_snapshot(sample("new"));assert time.monotonic()-started < .5
    reader.rollback();reader.close();repo.close()
