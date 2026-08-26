export interface BackendConfig { id:string; name:string; baseUrl:string }
export interface RuntimeConfig { backends:BackendConfig[]; requestTimeoutMs:number; refreshIntervalMs:number }
export interface ApiBase { api_version:string; schema_version:number; host_id:string }
export interface Status extends ApiBase { hostname:string; boot_id:string; lifecycle_key?:string; identity?:Record<string,string|number|null>; server_time_ms:number; backend_version:string; lifecycle:{termination:string;started_at_ms:number}; last_persisted_at_ms:number|null; storage:{used_bytes:number;limit_bytes:number;persistence_state:string;data_path?:string|null;mountpoint?:string|null;fs_type?:string|null;persistence_capability?:string|null;persistence_reason?:string|null}; collector:{sample_interval_ms:number;last_duration_ms:number|null;schedule_delay_ms:number|null;last_error?:string|null};capabilities?:Record<string,{state:string;reason?:string|null}> }
export interface Latest extends ApiBase { boot_id:string;lifecycle_key?:string;snapshot_id:number;captured_at_ms:number;detail_level:string;system:Record<string,any>;collector:{duration_ms:number;schedule_delay_ms:number};storage:Status['storage'] }
export interface Lifecycle { id:number;boot_id:string;lifecycle_key?:string;container_instance_id?:string|null;detection_method?:string;detection_confidence?:string;started_at_ms:number;last_seen_at_ms:number;ended_at_ms:number|null;termination:string;snapshot_count:number;full_snapshot_count?:number;trend_snapshot_count?:number;retention_state:string }
export type ResourceDimension='cpu'|'rss'|'swap'|'read'|'write'
export type UserSortKey='cpu_percent'|'rss_bytes'|'swap_bytes'|'read_bps'|'write_bps'|'process_count'|'thread_count'
export interface SnapshotSummary extends ApiBase { id:number;boot_id:string;lifecycle_key?:string|null;captured_at_ms:number;scheduled_at_ms:number;duration_ms:number;sample_interval_ms?:number|null;detail_level:string;persistence_state:string }
export interface SnapshotPage extends ApiBase { boot_id:string;lifecycle_key?:string|null;items:SnapshotSummary[];next_cursor?:string|null }
export interface RankValue { rank:number;value:number }
export interface ProcessSample {
  snapshot_id:number;pid:number;create_time_ms:number;ppid?:number|null;uid:number;username?:string|null;comm:string;state?:string|null;threads?:number|null
  cpu_percent?:number|null;rss_bytes?:number|null;swap_bytes?:number|null;read_bps?:number|null;write_bps?:number|null;read_bytes?:number|null;write_bytes?:number|null
  cmdline_redacted:string;cmdline_hash:string;truncated:boolean;redaction_status:string;ranks?:Partial<Record<ResourceDimension,RankValue>>
}
export interface RankedProcess extends ProcessSample { rank:number;value:number;dimension:ResourceDimension }
export interface ProcessPage extends ApiBase { boot_id:string;lifecycle_key?:string|null;snapshot_id:number;dimension:ResourceDimension;items:RankedProcess[];next_cursor?:string|null }
export interface UserSample { uid:number;username?:string|null;cpu_percent?:number|null;rss_bytes?:number|null;swap_bytes?:number|null;read_bps?:number|null;write_bps?:number|null;process_count:number;thread_count:number }
export interface UserPage extends ApiBase { boot_id:string;lifecycle_key?:string|null;snapshot_id:number;items:UserSample[] }
export interface FullSnapshot extends SnapshotSummary { system:Record<string,unknown>;users:UserSample[];processes:ProcessSample[];top:Record<ResourceDimension,ProcessSample[]> }
export interface EventItem { id:number;boot_id:string;lifecycle_key?:string|null;snapshot_id?:number|null;occurred_at_ms:number;type:string;details:Record<string,unknown> }
export interface EventPage extends ApiBase { boot_id:string;lifecycle_key?:string|null;items:EventItem[];next_cursor?:string|null }
export interface SeriesItem { metric:string;unit:string;points:Array<[number,number|null]> }
export interface SeriesResponse extends ApiBase { boot_id:string;lifecycle_key?:string|null;requested_resolution:string;actual_resolution:string;series:SeriesItem[] }
export interface ProcessHistoryPage extends ApiBase { boot_id:string;lifecycle_key?:string|null;pid:number;create_time_ms:number;items:Array<ProcessSample&{captured_at_ms:number}>;next_cursor?:string|null }
export type HostFailureKind='timeout'|'network'|'forbidden'|'incompatible'|'backend'
export interface HostState { config:BackendConfig; status?:Status;latest?:Latest;error?:string;warning?:string;failureKind?:HostFailureKind;loading:boolean;lastSuccessAt?:number;clockOffsetMs?:number }
