from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Settings
from .models import ProcessSample, SnapshotData
from .redaction import RedactedCommand, redact_cmdline


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return default


def _key_values(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            result[key] = value.strip()
    return result


def _bytes(value: str | None) -> int | None:
    if not value: return None
    parts = value.split()
    try:
        number = int(parts[0])
        return number * 1024 if len(parts) > 1 and parts[1].lower() == "kb" else number
    except ValueError:
        return None


class ProcCollector:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.proc = settings.proc_root
        self.clock_ticks = os.sysconf("SC_CLK_TCK")
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.cpu_count = os.cpu_count() or 1
        self.previous_process: dict[tuple[int, int], tuple[int, int | None, int | None]] = {}
        self.previous_cmdline: dict[tuple[int, int], RedactedCommand] = {}
        self.previous_cpu: tuple[int, int] | None = None
        self.previous_mono_ns: int | None = None
        self.previous_wall_ms: int | None = None
        self.previous_oom_kill: int | None = None
        self.usernames = self._load_users()
        self.boot_epoch_ms = self.boot_started_at_ms()
        self.current_boot_id = self.boot_id()

    def _load_users(self) -> dict[int, str]:
        result: dict[int, str] = {}
        if self.settings.host_passwd:
            for line in _read(self.settings.host_passwd).splitlines():
                parts = line.split(":")
                if len(parts) >= 3:
                    try: result[int(parts[2])] = parts[0]
                    except ValueError: pass
        return result

    def boot_id(self) -> str:
        value = _read(self.proc / "sys/kernel/random/boot_id").strip()
        if not value:
            raise RuntimeError(f"cannot read boot_id from {self.proc}")
        return value

    def boot_started_at_ms(self) -> int:
        for line in _read(self.proc / "stat").splitlines():
            if line.startswith("btime "):
                try: return int(line.split()[1]) * 1000
                except (ValueError,IndexError): break
        uptime = float(_read(self.proc / "uptime", "0 0").split()[0])
        return int(time.time() * 1000 - uptime * 1000)

    def _cpu(self) -> tuple[float | None, tuple[int, int]]:
        fields = _read(self.proc / "stat").splitlines()[0].split()[1:]
        values = [int(v) for v in fields]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        percent = None
        if self.previous_cpu:
            total_delta = total - self.previous_cpu[0]
            idle_delta = idle - self.previous_cpu[1]
            if total_delta > 0:
                percent = round(max(0.0, min(100.0, (total_delta-idle_delta)*100/total_delta)), 3)
        return percent, (total, idle)

    def _psi(self, resource: str) -> dict[str, float | int | None]:
        out: dict[str, float | int | None] = {}
        for line in _read(self.proc / f"pressure/{resource}").splitlines():
            parts = line.split()
            if not parts: continue
            kind = parts[0]
            for token in parts[1:]:
                key, value = token.split("=", 1)
                out[f"psi_{resource}_{kind}_{key}"] = int(value) if key == "total" else float(value)
        return out

    def _filesystems(self) -> list[dict[str, Any]]:
        excluded={"proc","sysfs","tmpfs","devtmpfs","devpts","cgroup","cgroup2","overlay","squashfs","tracefs","securityfs","pstore","debugfs","mqueue","hugetlbfs","fusectl","configfs","autofs","ramfs"}
        mounts:dict[str,str]={"/":"unknown"}
        for line in _read(self.proc/"1/mountinfo").splitlines():
            left,separator,right=line.partition(" - ")
            if not separator:continue
            fields=left.split();tail=right.split()
            if len(fields)<5 or not tail:continue
            mountpoint=re.sub(r"\\([0-7]{3})",lambda m:chr(int(m.group(1),8)),fields[4])
            if tail[0] not in excluded:mounts[mountpoint]=tail[0]
        result=[]
        for mountpoint,fs_type in sorted(mounts.items()):
            target=self.proc/"1/root"/mountpoint.lstrip("/")
            try:
                fs=os.statvfs(target);result.append({"mountpoint":mountpoint,"fs_type":fs_type,"total_bytes":fs.f_blocks*fs.f_frsize,"available_bytes":fs.f_bavail*fs.f_frsize,"inode_total":fs.f_files,"inode_available":fs.f_favail})
            except OSError:continue
        return result

    def _system(self) -> tuple[dict[str, Any], tuple[int, int]]:
        cpu_percent, cpu_state = self._cpu()
        mem = _key_values(_read(self.proc / "meminfo"))
        load = _read(self.proc / "loadavg", "0 0 0").split()
        system: dict[str, Any] = {
            "host_cpu_percent": cpu_percent,
            "load1": float(load[0]) if len(load) > 0 else None,
            "load5": float(load[1]) if len(load) > 1 else None,
            "load15": float(load[2]) if len(load) > 2 else None,
            "memory_total_bytes": _bytes(mem.get("MemTotal")),
            "memory_available_bytes": _bytes(mem.get("MemAvailable")),
            "swap_total_bytes": _bytes(mem.get("SwapTotal")),
            "swap_used_bytes": None,
            "uptime_seconds": float(_read(self.proc / "uptime", "0").split()[0]),
        }
        if system["swap_total_bytes"] is not None:
            free = _bytes(mem.get("SwapFree"))
            if free is not None:
                system["swap_used_bytes"] = system["swap_total_bytes"] - free
        for resource in ("cpu", "memory", "io"):
            system.update(self._psi(resource))
        vmstat={line.split()[0]:int(line.split()[1]) for line in _read(self.proc/"vmstat").splitlines() if len(line.split())==2 and line.split()[1].isdigit()}
        current_oom=vmstat.get("oom_kill")
        system["oom_kill_total"] = current_oom
        if current_oom is not None and self.previous_oom_kill is not None:
            system["oom_kill_delta"] = max(0,current_oom-self.previous_oom_kill)
        else: system["oom_kill_delta"] = None
        self._next_oom_kill=current_oom
        filesystems=self._filesystems();system["filesystems"]=filesystems
        root_fs=next((x for x in filesystems if x["mountpoint"]=="/"),None)
        if root_fs:
            system.update(root_filesystem_total_bytes=root_fs["total_bytes"],root_filesystem_available_bytes=root_fs["available_bytes"],root_filesystem_inode_total=root_fs["inode_total"],root_filesystem_inode_available=root_fs["inode_available"])
        else:
            system.update(root_filesystem_total_bytes=None,root_filesystem_available_bytes=None,root_filesystem_inode_total=None,root_filesystem_inode_available=None)
        return system, cpu_state

    def _process(self, pid: int, interval_seconds: float | None) -> ProcessSample | None:
        root = self.proc / str(pid)
        try:
            raw_stat=(root / "stat").read_text(); open_paren=raw_stat.find("("); close_paren=raw_stat.rfind(")")
            if open_paren<0 or close_paren<open_paren: return None
            comm=raw_stat[open_paren+1:close_paren]; stat=raw_stat[close_paren+2:].split()
            status = _key_values(_read(root / "status"))
            create_ticks = int(stat[19])
            create_ms = self.boot_epoch_ms + int(create_ticks * 1000 / self.clock_ticks)
            uid_text = status.get("Uid")
            uid = int(uid_text.split()[0]) if uid_text else root.stat().st_uid
            cpu_ticks = int(stat[11]) + int(stat[12])
            io = _key_values(_read(root / "io"))
            read_raw,write_raw=io.get("read_bytes"),io.get("write_bytes")
            read_bytes,write_bytes=(int(read_raw),int(write_raw)) if read_raw is not None and write_raw is not None else (None,None)
            identity = (pid, create_ms)
            cpu_percent = read_bps = write_bps = None
            previous = self.previous_process.get(identity)
            if previous and interval_seconds and interval_seconds > 0:
                cpu_delta = cpu_ticks - previous[0]
                if cpu_delta >= 0: cpu_percent = round(cpu_delta / self.clock_ticks / interval_seconds * 100, 3)
                if read_bytes is not None and previous[1] is not None and read_bytes >= previous[1]: read_bps = round((read_bytes-previous[1])/interval_seconds, 3)
                if write_bytes is not None and previous[2] is not None and write_bytes >= previous[2]: write_bps = round((write_bytes-previous[2])/interval_seconds, 3)
            cmd = self.previous_cmdline.get(identity)
            if cmd is None:
                try: raw=(root/"cmdline").read_bytes();cmd=redact_cmdline(raw,self.settings.cmdline_max_bytes)
                except (OSError,PermissionError): cmd=redact_cmdline(b"[UNAVAILABLE]",self.settings.cmdline_max_bytes)
            self._next_cmdline[identity] = cmd
            self._next_process[identity] = (cpu_ticks, read_bytes, write_bytes)
            threads = int(status["Threads"]) if status.get("Threads") else int(stat[17])
            rss = _bytes(status.get("VmRSS"))
            if rss is None:
                statm = _read(root / "statm").split()
                rss = int(statm[1]) * self.page_size if len(statm) > 1 and statm[1].isdigit() else None
            return ProcessSample(pid,create_ms,int(stat[1]),uid,self.usernames.get(uid),comm,stat[0],threads,cpu_percent,rss,_bytes(status.get("VmSwap")),read_bps,write_bps,read_bytes,write_bytes,cmd.text,cmd.digest,cmd.truncated,cmd.status)
        except (OSError, PermissionError, ValueError, IndexError):
            return None

    def collect(self, scheduled_at_ms: int | None = None) -> SnapshotData:
        current_boot_id = self.boot_id()
        if current_boot_id != self.current_boot_id:
            self.current_boot_id = current_boot_id
            self.boot_epoch_ms = self.boot_started_at_ms()
            self.previous_process = {}
            self.previous_cmdline = {}
            self.previous_cpu = None
            self.previous_mono_ns = None
            self.previous_wall_ms = None
            self.previous_oom_kill = None
        started_ns = time.monotonic_ns()
        captured_ms = int(time.time() * 1000)
        interval = (started_ns-self.previous_mono_ns)/1e9 if self.previous_mono_ns else None
        system, cpu_state = self._system()
        self._next_process: dict[tuple[int,int],tuple[int,int | None,int | None]] = {}
        self._next_cmdline: dict[tuple[int,int],RedactedCommand] = {}
        pid_entries = [entry for entry in self.proc.iterdir() if entry.name.isdigit()]
        all_processes = [sample for entry in pid_entries if (sample := self._process(int(entry.name), interval))]
        dimensions = {"cpu":lambda p:p.cpu_percent,"rss":lambda p:p.rss_bytes,"swap":lambda p:p.swap_bytes,"read":lambda p:p.read_bps,"write":lambda p:p.write_bps}
        selected: dict[tuple[int,int],ProcessSample] = {}
        for dimension, getter in dimensions.items():
            ranked = sorted((p for p in all_processes if getter(p) is not None), key=lambda p: getter(p) or 0, reverse=True)[:self.settings.process_top_n]
            for rank, process in enumerate(ranked, 1):
                process.ranks[dimension] = (rank, float(getter(process) or 0))
                selected[(process.pid,process.create_time_ms)] = process
        users: dict[int,dict[str,Any]] = defaultdict(lambda:{"uid":0,"username":None,"cpu_percent":None,"rss_bytes":None,"swap_bytes":None,"read_bps":None,"write_bps":None,"process_count":0,"thread_count":0})
        for p in all_processes:
            u=users[p.uid]; u["uid"]=p.uid; u["username"]=p.username; u["process_count"]+=1; u["thread_count"]+=p.threads or 0
            for field in ("cpu_percent","rss_bytes","swap_bytes","read_bps","write_bps"):
                value=getattr(p,field)
                if value is not None: u[field]=(u[field] or 0)+value
        system["process_count"] = len(pid_entries)
        system["process_sampled_count"] = len(all_processes)
        system["thread_count"] = sum(p.threads or 0 for p in all_processes)
        system["capabilities"] = {
            **{f"psi_{resource}": {"state": "supported" if (self.proc / "pressure" / resource).exists() else "unsupported", "reason": None} for resource in ("cpu", "memory", "io")},
            "swap": {"state": "supported" if system["swap_total_bytes"] is not None else "unsupported", "reason": None},
            "process_visibility": {"state": "supported" if len(all_processes) == len(pid_entries) else "permission_denied", "reason": None if len(all_processes) == len(pid_entries) else f"sampled {len(all_processes)} of {len(pid_entries)} visible PIDs"},
            "process_io": {"state": "supported" if any(p.read_bytes is not None for p in all_processes) else "temporarily_unavailable", "reason": None},
            "filesystems": {"state": "supported" if system.get("filesystems") else "temporarily_unavailable", "reason": None},
        }
        duration_ms = int((time.monotonic_ns()-started_ns)/1e6)
        events=[]
        if interval is not None and interval * 1000 > self.settings.sample_interval_ms * 2:
            events.append({"type":"sampling_delay","details":{"gap_ms":round(interval*1000),"schedule_delay_ms":max(0,captured_ms-(scheduled_at_ms or captured_ms))}})
        if system.get("oom_kill_delta"):
            events.append({"type":"oom_observed","details":{"count":system["oom_kill_delta"]}})
        self.previous_process=self._next_process; self.previous_cmdline=self._next_cmdline; self.previous_cpu=cpu_state; self.previous_mono_ns=started_ns; self.previous_wall_ms=captured_ms; self.previous_oom_kill=self._next_oom_kill
        return SnapshotData(current_boot_id,captured_ms,scheduled_at_ms or captured_ms,duration_ms,int(interval*1000) if interval else None,system,list(selected.values()),list(users.values()),events)
