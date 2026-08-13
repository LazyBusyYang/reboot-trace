import {describe,expect,it,vi} from 'vitest'
import {api,evidenceGap,fmtBytes,processIdentity,terminationLabel} from './api'

describe('API client',()=>{
  it('always omits credentials',async()=>{
    const fetchMock=vi.fn().mockResolvedValue({ok:true,json:async()=>({ok:true})})
    vi.stubGlobal('fetch',fetchMock);vi.stubGlobal('crypto',{randomUUID:()=> 'request-id'})
    await api({id:'a',name:'A',baseUrl:'https://host.example/api/v1'},'/status')
    expect(fetchMock.mock.calls[0][1].credentials).toBe('omit')
  })
  it('uses evidence-safe lifecycle wording',()=>{
    expect(terminationLabel('unclean_or_unknown')).toContain('原因未知')
    expect(terminationLabel('unclean_or_unknown')).not.toContain('异常重启')
  })
  it('formats null as unknown',()=>expect(fmtBytes(null)).toBe('—'))
  it('formats bytes with IEC units',()=>expect(fmtBytes(1024)).toBe('1 KiB'))
  it('does not invent an evidence gap without trusted termination time',()=>{
    expect(evidenceGap(1000,null)).toBeNull()
    expect(evidenceGap(1000,1250)).toBe(250)
  })
  it('keeps PID reuse identities distinct',()=>expect(processIdentity(42,100)).not.toBe(processIdentity(42,200)))
  it('keeps unknown enum values visible',()=>expect(terminationLabel('future_state')).toContain('future_state'))
  it('classifies ingress HTML failures without requiring JSON',async()=>{
    const response={ok:false,status:403,json:async()=>{throw new Error('not json')}}
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(response));vi.stubGlobal('crypto',{randomUUID:()=> 'request-id'})
    await expect(api({id:'a',name:'A',baseUrl:'https://host.example/api/v1'},'/status')).rejects.toMatchObject({kind:'forbidden',status:403})
  })
  it('rejects a future unknown schema version',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>({api_version:'1',schema_version:99})}));vi.stubGlobal('crypto',{randomUUID:()=> 'request-id'})
    await expect(api({id:'a',name:'A',baseUrl:'https://host.example/api/v1'},'/status')).rejects.toMatchObject({kind:'incompatible',code:'VERSION_UNSUPPORTED'})
  })
  it('accepts schema v3 lifecycle identity responses',async()=>{
    const payload={api_version:'1',schema_version:3,lifecycle_key:'instance-a',boot_id:'kernel-a'}
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,json:async()=>payload}));vi.stubGlobal('crypto',{randomUUID:()=> 'request-id'})
    await expect(api({id:'a',name:'A',baseUrl:'https://host.example/api/v1'},'/status')).resolves.toMatchObject(payload)
  })
  it('accepts schema v2 and v3 backends during a rolling upgrade',async()=>{
    const v2={api_version:'1',schema_version:2,boot_id:'kernel-a'}
    const v3={api_version:'1',schema_version:3,lifecycle_key:'instance-b',boot_id:'kernel-b'}
    vi.stubGlobal('fetch',vi.fn()
      .mockResolvedValueOnce({ok:true,json:async()=>v2})
      .mockResolvedValueOnce({ok:true,json:async()=>v3}))
    vi.stubGlobal('crypto',{randomUUID:()=> 'request-id'})
    const values=await Promise.all([
      api<{schema_version:number}>({id:'a',name:'A',baseUrl:'https://a.example/api/v1'},'/status'),
      api<{schema_version:number}>({id:'b',name:'B',baseUrl:'https://b.example/api/v1'},'/status'),
    ])
    expect(values.map(value=>value.schema_version)).toEqual([2,3])
  })
})
