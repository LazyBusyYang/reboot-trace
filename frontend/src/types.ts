export interface BackendConfig { id:string; name:string; baseUrl:string }
export interface RuntimeConfig { backends:BackendConfig[]; requestTimeoutMs:number; refreshIntervalMs:number }
export interface ApiBase { api_version:string; schema_version:number; host_id:string }
export interface Status extends ApiBase { hostname:string; boot_id:string; server_time_ms:number; backend_version:string; lifecycle:{termination:string;started_at_ms:number}; last_persisted_at_ms:number|null; storage:{used_bytes:number;limit_bytes:number;persistence_state:string}; collector:{sample_interval_ms:number;last_duration_ms:number|null;schedule_delay_ms:number|null;last_error?:string|null};capabilities?:Record<string,{state:string;reason?:string|null}> }
export interface Latest extends ApiBase { boot_id:string;snapshot_id:number;captured_at_ms:number;detail_level:string;system:Record<string,number|null>;collector:{duration_ms:number;schedule_delay_ms:number};storage:Status['storage'] }
export interface Lifecycle { id:number;boot_id:string;started_at_ms:number;last_seen_at_ms:number;ended_at_ms:number|null;termination:string;snapshot_count:number;retention_state:string }
export type HostFailureKind='timeout'|'network'|'forbidden'|'incompatible'|'backend'
export interface HostState { config:BackendConfig; status?:Status;latest?:Latest;error?:string;warning?:string;failureKind?:HostFailureKind;loading:boolean;lastSuccessAt?:number;clockOffsetMs?:number }
