import time
from reboot_trace.collector import ProcCollector


def test_collects_redacted_top_process_and_user(settings,fake_process,monkeypatch):
    c=ProcCollector(settings)
    # Windows may report a different synthetic inode for the fake namespace
    # file between stat calls. Identity transitions are covered separately;
    # keep this process-delta test focused on a stable container lifecycle.
    stable_identity=c.current_identity
    monkeypatch.setattr(c.identity_reader,"read",lambda:stable_identity)
    first=c.collect()
    assert first.system["memory_available_bytes"] == 50000*1024
    assert first.system["filesystems"][0]["mountpoint"] == "/"
    assert all(x["fs_type"] != "proc" for x in first.system["filesystems"])
    assert first.processes[0].cmdline_redacted.endswith("[REDACTED]")
    assert "supersecret" not in first.processes[0].cmdline_redacted
    user=next(item for item in first.users if item["uid"]==1000)
    assert user["cpu_percent"] is None
    assert user["read_bps"] is None
    assert user["rss_bytes"] == 1024*1024
    assert c.previous_mono_ns is not None
    c.previous_mono_ns -= 1_000_000_000
    raw=(fake_process/"stat").read_text(); end=raw.rfind(")"); rest=raw[end+2:].split(); rest[11]="20"; (fake_process/"stat").write_text(raw[:end+2]+" ".join(rest))
    second=c.collect()
    assert second.processes[0].cpu_percent is not None
    assert second.processes[0].comm == "python worker"
    assert any(item["uid"]==1000 for item in second.users)


def test_io_permission_failure_keeps_process_evidence(settings,fake_process,monkeypatch):
    original=type(fake_process).read_text
    def guarded(path,*args,**kwargs):
        if path.name=="io": raise PermissionError
        return original(path,*args,**kwargs)
    monkeypatch.setattr(type(fake_process),"read_text",guarded)
    result=ProcCollector(settings).collect()
    assert result.processes and result.processes[0].rss_bytes == 1024*1024
    assert result.processes[0].read_bps is None


def test_kernel_boot_change_alone_preserves_container_rate_baselines(settings,fake_process):
    collector=ProcCollector(settings)
    collector.collect()
    fingerprint=collector.current_identity.fingerprint
    (settings.proc_root/"sys/kernel/random/boot_id").write_text("22222222-2222-2222-2222-222222222222\n")
    identity=collector.identity()
    assert identity.kernel_boot_id.startswith("22222222")
    assert identity.fingerprint == fingerprint


def test_sampling_delay_uses_monotonic_gap_and_preserves_schedule_delay(settings,fake_process):
    collector=ProcCollector(settings)
    collector.collect()
    collector.previous_mono_ns=time.monotonic_ns()-settings.sample_interval_ms*3*1_000_000
    collector.previous_wall_ms=int(time.time()*1000)+999_999
    scheduled=int(time.time()*1000)-500
    result=collector.collect(scheduled)
    event=next(item for item in result.events if item["type"]=="sampling_delay")
    assert event["details"]["gap_ms"] >= settings.sample_interval_ms*2
    assert event["details"]["schedule_delay_ms"] >= 500


def test_process_identity_uses_stable_kernel_boot_time(settings,fake_process,monkeypatch):
    (settings.proc_root/"stat").write_text("cpu  100 0 20 800 0 0 0 0 0 0\nbtime 1700000000\n")
    first=ProcCollector(settings).collect().processes[0].create_time_ms
    monkeypatch.setattr(time,"time",lambda:9999999999)
    second=ProcCollector(settings).collect().processes[0].create_time_ms
    assert first == second
