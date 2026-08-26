import {describe,expect,it} from 'vitest'
import {analysisQueryParams,clampWindow,extractIdentityHints,filterProcesses,normalizeAnalysisQuery,shiftWindow,sortUsers,trendResolution,windowAround} from './analysis'
import type { Lifecycle, RankedProcess } from './types'

const lifecycle:Lifecycle={id:1,boot_id:'boot',lifecycle_key:'life',started_at_ms:1_000,last_seen_at_ms:10_000,ended_at_ms:10_000,termination:'unclean_or_unknown',snapshot_count:12,retention_state:'final_complete'}

describe('analysis helpers',()=>{
  it('clamps, shifts and centers time windows inside lifecycle bounds',()=>{
    expect(clampWindow(0,20_000,lifecycle)).toMatchObject({from:1_000,to:10_000,invalid:false})
    expect(shiftWindow(2_000,4_000,-1,lifecycle)).toEqual({from:1_000,to:3_000})
    expect(windowAround(9_500,lifecycle,2_000)).toEqual({from:8_000,to:10_000})
  })
  it('selects trend resolution by duration',()=>{
    expect(trendResolution(0,30*60_000)).toBe('10s')
    expect(trendResolution(0,31*60_000)).toBe('1m')
    expect(trendResolution(0,7*60*60_000)).toBe('5m')
  })
  it('normalizes invalid URL state and round-trips canonical parameters',()=>{
    const result=normalizeAnalysisQuery({from:'bad',to:'2',dimension:'future',uid:'10597',process:' rg ',state:'R'},lifecycle)
    expect(result.value).toMatchObject({from:1_000,to:10_000,dimension:'cpu',uid:10597,process:'rg',state:'R'})
    expect(analysisQueryParams(result.value)).toMatchObject({uid:'10597',process:'rg',state:'R'})
  })
  it('sorts users by resource metrics',()=>{
    expect(sortUsers([{uid:1,cpu_percent:2,process_count:1,thread_count:1},{uid:2,cpu_percent:8,process_count:1,thread_count:1}],'cpu_percent').map(item=>item.uid)).toEqual([2,1])
  })
  it('filters loaded processes by identity, state and activity',()=>{
    const items=[{pid:42,ppid:1,uid:10597,username:null,comm:'rg',state:'R',cpu_percent:300,rss_bytes:1,cmdline_redacted:'rg --files',cmdline_hash:'x',snapshot_id:1,create_time_ms:1,truncated:false,redaction_status:'ok',rank:1,value:300,dimension:'cpu'}] as RankedProcess[]
    expect(filterProcesses(items,'10597','R',true)).toHaveLength(1)
    expect(filterProcesses(items,'node','',false)).toHaveLength(0)
  })
  it('extracts user directory evidence without treating system paths as identities',()=>{
    const processes=[
      {cmdline_redacted:'/mnt/aigc/yangpei1/.vscode-server/node',pid:1,uid:1,comm:'node'},
      {cmdline_redacted:'/mnt/aigc/users/wangruisi/.cursor-server/node',pid:2,uid:2,comm:'node'},
      {cmdline_redacted:'/root/.vscode-server/node /tmp/x /mnt/aigc/common/tool',pid:3,uid:0,comm:'node'},
    ].map(item=>({...item,snapshot_id:1,create_time_ms:1,cmdline_hash:'x',truncated:false,redaction_status:'ok'}))
    expect(extractIdentityHints(processes).map(item=>item.name)).toEqual(['wangruisi','yangpei1'])
  })
})
