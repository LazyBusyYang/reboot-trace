import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAppStore } from './store'

const backend = { id: 'dev-01', name: 'Dev 01', baseUrl: 'https://dev-01.example/api/v1' }
const status = { api_version: '1', schema_version: 2, host_id: 'host-a', hostname: 'dev-01', boot_id: 'boot-a', server_time_ms: Date.now(), backend_version: '1', lifecycle: { termination: 'active', started_at_ms: 1 }, last_persisted_at_ms: 1, storage: { used_bytes: 1, limit_bytes: 2, persistence_state: 'normal' }, collector: { sample_interval_ms: 5000, last_duration_ms: 1, schedule_delay_ms: 0 } }
const latest = { api_version: '1', schema_version: 2, host_id: 'host-a', boot_id: 'boot-a', snapshot_id: 1, captured_at_ms: 1, detail_level: 'full', system: {}, collector: { duration_ms: 1, schedule_delay_ms: 0 }, storage: status.storage }

describe('multi-backend state isolation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal('crypto', { randomUUID: () => 'request-id' })
  })
  it('keeps successful partial data when the sibling request fails', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => status })
      .mockRejectedValueOnce(new Error('offline')))
    const store = useAppStore()
    store.config = { backends: [backend], requestTimeoutMs: 1000, refreshIntervalMs: 5000 }
    store.hosts = [{ config: backend, loading: true }]
    await store.refreshHost(store.hosts[0])
    expect(store.hosts[0].status?.host_id).toBe('host-a')
    expect(store.hosts[0].warning).toContain('部分数据不可用')
    expect(store.hosts[0].error).toBeUndefined()
  })
  it('rejects inconsistent host identity responses', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => status })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ...latest, host_id: 'host-b' }) }))
    const store = useAppStore()
    store.config = { backends: [backend], requestTimeoutMs: 1000, refreshIntervalMs: 5000 }
    store.hosts = [{ config: backend, loading: true }]
    await store.refreshHost(store.hosts[0])
    expect(store.hosts[0].error).toContain('host_id')
    expect(store.hosts[0].status).toBeUndefined()
  })
  it('keeps a healthy backend usable when another backend is offline', async () => {
    const second={...backend,id:'dev-02',name:'Dev 02',baseUrl:'https://dev-02.example/api/v1'}
    vi.stubGlobal('fetch',vi.fn()
      .mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ok:true,json:async()=>({...status,host_id:'host-b',hostname:'dev-02'})})
      .mockResolvedValueOnce({ok:true,json:async()=>({...latest,host_id:'host-b'})}))
    const store=useAppStore()
    store.config={backends:[backend,second],requestTimeoutMs:1000,refreshIntervalMs:5000}
    store.hosts=[{config:backend,loading:true},{config:second,loading:true}]
    await store.refresh()
    expect(store.hosts[0].error).toContain('offline')
    expect(store.hosts[1].status?.host_id).toBe('host-b')
    expect(store.hosts[1].error).toBeUndefined()
  })
  it('does not automatically retry an ingress-forbidden backend', async () => {
    const fetchMock=vi.fn().mockResolvedValue({ok:false,status:403,json:async()=>{throw new Error('html')}})
    vi.stubGlobal('fetch',fetchMock)
    const store=useAppStore();store.config={backends:[backend],requestTimeoutMs:1000,refreshIntervalMs:5000};store.hosts=[{config:backend,loading:true}]
    await store.refresh(true)
    expect(store.hosts[0].failureKind).toBe('forbidden')
    const calls=fetchMock.mock.calls.length
    await store.refresh(false)
    expect(fetchMock.mock.calls.length).toBe(calls)
    await store.refresh(true)
    expect(fetchMock.mock.calls.length).toBeGreaterThan(calls)
  })
})
