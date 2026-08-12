from __future__ import annotations

import os
from pathlib import Path

import pytest

from reboot_trace.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    proc = tmp_path / "proc"; sys = tmp_path / "sys"; data = tmp_path / "data"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "pressure").mkdir()
    sys.mkdir(); data.mkdir()
    (proc / "sys/kernel/random/boot_id").write_text("11111111-1111-1111-1111-111111111111\n")
    (proc / "uptime").write_text("100.00 10.00\n")
    (proc / "stat").write_text("cpu  100 0 20 800 0 0 0 0 0 0\n")
    (proc / "loadavg").write_text("1.00 2.00 3.00 1/10 1\n")
    (proc / "meminfo").write_text("MemTotal: 100000 kB\nMemAvailable: 50000 kB\nSwapTotal: 1000 kB\nSwapFree: 400 kB\n")
    (proc / "vmstat").write_text("pgfault 10\noom_kill 0\n")
    (proc / "1/root").mkdir(parents=True)
    (proc / "1/mountinfo").write_text("1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 0:1 / /proc rw - proc proc rw\n")
    for name in ("cpu","memory","io"):
        (proc / "pressure" / name).write_text("some avg10=1.00 avg60=2.00 avg300=3.00 total=10\nfull avg10=0.10 avg60=0.20 avg300=0.30 total=2\n" if name != "cpu" else "some avg10=1.00 avg60=2.00 avg300=3.00 total=10\n")
    passwd=tmp_path/"passwd"; passwd.write_text("alice:x:1000:1000::/home/alice:/bin/bash\n")
    return Settings(data,proc,passwd,8*1024*1024,100,5,4096,("https://frontend.example",),"host-test")


@pytest.fixture
def fake_process(settings: Settings):
    root=settings.proc_root/"123"; root.mkdir()
    rest=["R","1"]+["0"]*17+["100"]+["0"]*20
    rest[11]="10"; rest[12]="5"; rest[19]="100"
    (root/"stat").write_text("123 (python worker) "+" ".join(rest))
    (root/"status").write_text("Uid:\t1000\t1000\t1000\t1000\nThreads:\t4\nVmRSS:\t1024 kB\nVmSwap:\t10 kB\n")
    (root/"io").write_text("read_bytes: 100\nwrite_bytes: 200\n")
    (root/"cmdline").write_bytes(b"python\0train.py\0--token\0supersecret\0")
    return root
