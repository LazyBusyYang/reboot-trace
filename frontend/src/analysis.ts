import type { Lifecycle, ProcessSample, RankedProcess, ResourceDimension, UserSample, UserSortKey } from './types'

export const WINDOW_10_MIN=10*60_000
export const WINDOW_1_HOUR=60*60_000
export const DIMENSIONS:ResourceDimension[]=['cpu','rss','swap','read','write']
export const PROCESS_STATES=['R','S','D','T','Z','I'] as const

export interface AnalysisQuery { from:number;to:number;snapshot?:number;dimension:ResourceDimension;uid?:number;process:string;state:string }
export interface NormalizedQuery { value:AnalysisQuery;changed:boolean;notice?:string }

const finiteInteger=(value:unknown):number|undefined=>{
  const parsed=typeof value==='string' && value.trim()!=='' ? Number(value) : typeof value==='number' ? value : NaN
  return Number.isSafeInteger(parsed) && parsed>=0 ? parsed : undefined
}

export function defaultWindow(lifecycle:Lifecycle,duration=WINDOW_10_MIN){
  const to=lifecycle.last_seen_at_ms
  return {from:Math.max(lifecycle.started_at_ms,to-duration),to}
}

export function clampWindow(from:number,to:number,lifecycle:Lifecycle):{from:number;to:number;invalid:boolean}{
  const fallback=defaultWindow(lifecycle)
  if(!Number.isFinite(from)||!Number.isFinite(to)||from>=to) return {...fallback,invalid:true}
  const boundedFrom=Math.max(lifecycle.started_at_ms,Math.min(from,lifecycle.last_seen_at_ms))
  const boundedTo=Math.max(lifecycle.started_at_ms,Math.min(to,lifecycle.last_seen_at_ms))
  if(boundedFrom>=boundedTo) return {...fallback,invalid:true}
  return {from:boundedFrom,to:boundedTo,invalid:false}
}

export function windowAround(timestamp:number,lifecycle:Lifecycle,duration=WINDOW_10_MIN){
  const half=duration/2
  let from=Math.max(lifecycle.started_at_ms,timestamp-half)
  let to=Math.min(lifecycle.last_seen_at_ms,from+duration)
  from=Math.max(lifecycle.started_at_ms,to-duration)
  return {from:Math.round(from),to:Math.round(to)}
}

export function shiftWindow(from:number,to:number,direction:-1|1,lifecycle:Lifecycle){
  const width=to-from
  let nextFrom=from+direction*width
  let nextTo=to+direction*width
  if(nextFrom<lifecycle.started_at_ms){ nextFrom=lifecycle.started_at_ms;nextTo=Math.min(lifecycle.last_seen_at_ms,nextFrom+width) }
  if(nextTo>lifecycle.last_seen_at_ms){ nextTo=lifecycle.last_seen_at_ms;nextFrom=Math.max(lifecycle.started_at_ms,nextTo-width) }
  return {from:nextFrom,to:nextTo}
}

export const trendResolution=(from:number,to:number)=>to-from<=30*60_000?'10s':to-from<=6*60*60_000?'1m':'5m'

export function normalizeAnalysisQuery(query:Record<string,unknown>,lifecycle:Lifecycle):NormalizedQuery{
  const fallback=defaultWindow(lifecycle)
  const rawFrom=finiteInteger(query.from) ?? fallback.from
  const rawTo=finiteInteger(query.to) ?? fallback.to
  const window=clampWindow(rawFrom,rawTo,lifecycle)
  const dimension=DIMENSIONS.includes(query.dimension as ResourceDimension) ? query.dimension as ResourceDimension : 'cpu'
  const uid=finiteInteger(query.uid)
  const snapshot=finiteInteger(query.snapshot)
  const process=typeof query.process==='string' ? query.process.trim().slice(0,200) : ''
  const state=typeof query.state==='string' && PROCESS_STATES.includes(query.state as typeof PROCESS_STATES[number]) ? query.state : ''
  const value={from:window.from,to:window.to,snapshot,dimension,uid,process,state}
  const canonical=analysisQueryParams(value)
  const current=Object.fromEntries(Object.entries(query).filter(([,item])=>typeof item==='string' && item!==''))
  const changed=Object.keys({...canonical,...current}).some(key=>String(canonical[key]??'')!==String(current[key]??''))
  return {value,changed,notice:window.invalid?'时间范围无效，已恢复为生命周期末尾十分钟。':undefined}
}

export function analysisQueryParams(value:AnalysisQuery):Record<string,string>{
  const result:Record<string,string>={from:String(value.from),to:String(value.to),dimension:value.dimension}
  if(value.snapshot!=null) result.snapshot=String(value.snapshot)
  if(value.uid!=null) result.uid=String(value.uid)
  if(value.process) result.process=value.process
  if(value.state) result.state=value.state
  return result
}

const userMetric=(user:UserSample,key:UserSortKey)=>Number(user[key]??0)
export const sortUsers=(users:UserSample[],key:UserSortKey)=>[...users].sort((a,b)=>userMetric(b,key)-userMetric(a,key)||a.uid-b.uid)

export function filterProcesses(items:RankedProcess[],keyword:string,state:string,activeOnly:boolean){
  const term=keyword.trim().toLocaleLowerCase()
  return items.filter(item=>{
    const matchesText=!term || [item.pid,item.ppid,item.comm,item.cmdline_redacted,item.username,item.uid].some(value=>String(value??'').toLocaleLowerCase().includes(term))
    const matchesState=!state || item.state===state
    const active=!activeOnly || [item.cpu_percent,item.rss_bytes,item.swap_bytes,item.read_bps,item.write_bps].some(value=>(value??0)>0)
    return matchesText&&matchesState&&active
  })
}

export interface IdentityHint { name:string;paths:string[];count:number }
export function extractIdentityHints(processes:ProcessSample[]):IdentityHint[]{
  const evidence=new Map<string,Set<string>>()
  const patterns=[/\/mnt\/aigc\/users\/([A-Za-z0-9._-]+)(?=\/)/g,/\/mnt\/aigc\/([A-Za-z0-9._-]+)(?=\/)/g,/\/home\/([A-Za-z0-9._-]+)(?=\/)/g]
  const ignored=new Set(['users','root','tmp','shared','public','common'])
  for(const process of processes){
    const command=process.cmdline_redacted||''
    for(const pattern of patterns){
      pattern.lastIndex=0
      for(const match of command.matchAll(pattern)){
        const name=match[1]
        if(ignored.has(name.toLocaleLowerCase())) continue
        const path=match[0].slice(0,-1)
        if(!evidence.has(name)) evidence.set(name,new Set())
        evidence.get(name)!.add(path)
      }
    }
  }
  return [...evidence.entries()].map(([name,paths])=>({name,paths:[...paths],count:processes.filter(item=>paths.size>0&&[...paths].some(path=>item.cmdline_redacted.includes(path))).length})).sort((a,b)=>b.count-a.count||a.name.localeCompare(b.name))
}

export function toLocalDateTime(value:number){
  const date=new Date(value-dateOffset(value));return date.toISOString().slice(0,19)
}
const dateOffset=(value:number)=>new Date(value).getTimezoneOffset()*60_000
export function fromLocalDateTime(value:string){ const parsed=new Date(value).getTime();return Number.isFinite(parsed)?parsed:undefined }
