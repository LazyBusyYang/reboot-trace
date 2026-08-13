from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config=ConfigDict(extra="allow")


class StorageStatus(ContractModel):
    used_bytes:int;limit_bytes:int;persistence_state:str
    data_path:str|None=None;mountpoint:str|None=None;fs_type:str|None=None
    persistence_capability:str|None=None;persistence_reason:str|None=None


class LifecycleStatus(ContractModel):
    termination:str;started_at_ms:int


class CollectorStatus(ContractModel):
    sample_interval_ms:int
    last_duration_ms:int|None=None
    schedule_delay_ms:int|None=None
    last_error:str|None=None


class Capability(ContractModel):
    state:str;reason:str|None=None


class ApiResponse(ContractModel):
    api_version:str;schema_version:int;host_id:str


class StatusResponse(ApiResponse):
    hostname:str;boot_id:str;lifecycle_key:str;server_time_ms:int;backend_version:str
    lifecycle:LifecycleStatus;last_persisted_at_ms:int|None
    storage:StorageStatus;collector:CollectorStatus
    capabilities:dict[str,Capability]
    identity:dict[str,Any]={}


class FilesystemSample(ContractModel):
    mountpoint:str;fs_type:str;total_bytes:int;available_bytes:int
    inode_total:int;inode_available:int


class SystemSample(ContractModel):
    host_cpu_percent:float|None=None
    load1:float|None=None;load5:float|None=None;load15:float|None=None
    memory_total_bytes:int|None=None;memory_available_bytes:int|None=None
    swap_total_bytes:int|None=None;swap_used_bytes:int|None=None
    process_count:int;thread_count:int;uptime_seconds:float
    process_sampled_count:int|None=None
    root_filesystem_total_bytes:int|None=None;root_filesystem_available_bytes:int|None=None
    root_filesystem_inode_total:int|None=None;root_filesystem_inode_available:int|None=None
    filesystems:list[FilesystemSample]=[]
    oom_kill_total:int|None=None;oom_kill_delta:int|None=None
    psi_cpu_some_avg10:float|None=None;psi_cpu_some_avg60:float|None=None;psi_cpu_some_avg300:float|None=None;psi_cpu_some_total:int|None=None
    psi_memory_some_avg10:float|None=None;psi_memory_some_avg60:float|None=None;psi_memory_some_avg300:float|None=None;psi_memory_some_total:int|None=None
    psi_memory_full_avg10:float|None=None;psi_memory_full_avg60:float|None=None;psi_memory_full_avg300:float|None=None;psi_memory_full_total:int|None=None
    psi_io_some_avg10:float|None=None;psi_io_some_avg60:float|None=None;psi_io_some_avg300:float|None=None;psi_io_some_total:int|None=None
    psi_io_full_avg10:float|None=None;psi_io_full_avg60:float|None=None;psi_io_full_avg300:float|None=None;psi_io_full_total:int|None=None
    capabilities:dict[str,Capability]={}


class LatestCollector(ContractModel):
    duration_ms:int;schedule_delay_ms:int


class LatestResponse(ApiResponse):
    boot_id:str;lifecycle_key:str;snapshot_id:int;captured_at_ms:int;detail_level:str
    system:SystemSample;collector:LatestCollector;storage:StorageStatus


class PublicConfigResponse(ApiResponse):
    sample_interval_ms:int;process_top_n:int;storage_limit_bytes:int;cmdline_max_bytes:int


class LifecycleSummary(ContractModel):
    id:int;host_id:str;boot_id:str;lifecycle_key:str;started_at_ms:int;last_seen_at_ms:int
    ended_at_ms:int|None=None;termination:str;summary_json:str
    snapshot_count:int=0;retention_state:str="complete"
    container_instance_id:str|None=None;pid1_start_ticks:int|None=None;pid_namespace_inode:int|None=None
    cgroup_hash:str|None=None;detection_method:str="legacy";detection_confidence:str="unknown"
    identity_first_observed_at_ms:int|None=None


class LifecycleResponse(ApiResponse,LifecycleSummary):
    pass


class LifecyclePage(ApiResponse):
    items:list[LifecycleSummary];next_cursor:str|None=None


class SnapshotSummary(ContractModel):
    id:int;boot_id:str;lifecycle_key:str|None=None;captured_at_ms:int;scheduled_at_ms:int
    duration_ms:int;sample_interval_ms:int|None=None;detail_level:str
    persistence_state:str="normal"


class SnapshotPage(ApiResponse):
    boot_id:str;items:list[SnapshotSummary];next_cursor:str|None=None


class RankValue(ContractModel):
    rank:int;value:float


class ProcessSampleResponse(ContractModel):
    snapshot_id:int;pid:int;create_time_ms:int;ppid:int|None=None;uid:int;username:str|None=None
    comm:str;state:str|None=None;threads:int|None=None;cpu_percent:float|None=None
    rss_bytes:int|None=None;swap_bytes:int|None=None;read_bps:float|None=None;write_bps:float|None=None
    read_bytes:int|None=None;write_bytes:int|None=None;cmdline_redacted:str;cmdline_hash:str
    truncated:bool;redaction_status:str;ranks:dict[str,RankValue]={}


class UserSampleResponse(ContractModel):
    uid:int;username:str|None=None;cpu_percent:float|None=None;rss_bytes:int|None=None;swap_bytes:int|None=None
    read_bps:float|None=None;write_bps:float|None=None;process_count:int;thread_count:int


class FullSnapshotResponse(ApiResponse,SnapshotSummary):
    boot_id:str;system:SystemSample;users:list[UserSampleResponse]
    processes:list[ProcessSampleResponse];top:dict[str,list[ProcessSampleResponse]]


class FinalSnapshot(SnapshotSummary):
    system:SystemSample;users:list[UserSampleResponse];top:dict[str,list[ProcessSampleResponse]]


class FinalResponse(ApiResponse):
    boot_id:str;lifecycle_key:str|None=None;termination:str;last_persisted_at_ms:int;evidence_gap_ms:int|None=None;snapshots:list[FinalSnapshot]


class EventResponse(ContractModel):
    id:int;boot_id:str;lifecycle_key:str|None=None;snapshot_id:int|None=None;occurred_at_ms:int;type:str;details:dict[str,Any]


class EventPage(ApiResponse):
    boot_id:str;items:list[EventResponse];next_cursor:str|None=None


class ProcessPage(ApiResponse):
    boot_id:str;snapshot_id:int;dimension:str;items:list[ProcessSampleResponse];next_cursor:str|None=None


class UserPage(ApiResponse):
    boot_id:str;snapshot_id:int;items:list[UserSampleResponse]


class SeriesItem(ContractModel):
    metric:str;unit:str;points:list[list[int|float|None]]


class SeriesResponse(ApiResponse):
    boot_id:str;requested_resolution:str;actual_resolution:str;series:list[SeriesItem]


class CompareResponse(ApiResponse):
    boot_id:str;left_snapshot_id:int;right_snapshot_id:int;system_deltas:list[dict[str,Any]]
    processes:dict[str,list[Any]];users:list[dict[str,Any]]


class ProcessHistoryResponse(ApiResponse):
    boot_id:str;pid:int;create_time_ms:int;items:list[ProcessSampleResponse];next_cursor:str|None=None
