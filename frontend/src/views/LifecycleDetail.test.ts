import {flushPromises,mount} from '@vue/test-utils'
import {createPinia,setActivePinia} from 'pinia'
import {describe,expect,it,vi} from 'vitest'
import {createMemoryHistory,createRouter} from 'vue-router'
import {useAppStore} from '../store'
import LifecycleDetail from './LifecycleDetail.vue'

const base={api_version:'1',schema_version:3,host_id:'host-4'}
const lifecycle={...base,id:7,boot_id:'boot-4',lifecycle_key:'life',started_at_ms:1_000,last_seen_at_ms:10_000,ended_at_ms:10_000,termination:'unclean_or_unknown',snapshot_count:12,retention_state:'final_complete'}
const snapshot={...base,id:111725,boot_id:'boot-4',lifecycle_key:'life',captured_at_ms:9_000,scheduled_at_ms:8_999,duration_ms:10,sample_interval_ms:5_000,detail_level:'final',persistence_state:'normal'}
const user={uid:10597,username:null,cpu_percent:300,rss_bytes:2_000,swap_bytes:0,read_bps:0,write_bps:0,process_count:1,thread_count:14}
const process={snapshot_id:111725,pid:42,ppid:1,create_time_ms:2_000,uid:10597,username:null,comm:'rg',state:'R',threads:14,cpu_percent:300,rss_bytes:2_000,swap_bytes:0,read_bps:0,write_bps:0,cmdline_redacted:'/mnt/aigc/yangpei1/.vscode-server/rg --files',cmdline_hash:'hash',truncated:false,redaction_status:'no_sensitive_value',rank:1,value:300,dimension:'cpu'}

function response(value:unknown){return {ok:true,status:200,headers:{get:()=>null},json:async()=>value}}
function mockFetch(){
  return vi.fn(async(input:string|URL|Request)=>{
    const url=String(input)
    if(url.endsWith('/lifecycles/life'))return response(lifecycle)
    if(url.includes('/snapshots/111725/processes'))return response({...base,boot_id:'boot-4',snapshot_id:111725,dimension:url.includes('dimension=rss')?'rss':'cpu',items:[{...process,dimension:url.includes('dimension=rss')?'rss':'cpu'}],next_cursor:null})
    if(url.endsWith('/snapshots/111725/users'))return response({...base,boot_id:'boot-4',snapshot_id:111725,items:[user]})
    if(url.endsWith('/snapshots/111725'))return response({...snapshot,system:{},users:[user],processes:[process],top:{cpu:[process],rss:[],swap:[],read:[],write:[]}})
    if(url.includes('/snapshots?'))return response({...base,boot_id:'boot-4',items:[snapshot],next_cursor:null})
    if(url.includes('/events'))return response({...base,boot_id:'boot-4',items:[],next_cursor:null})
    if(url.includes('/series'))return response({...base,boot_id:'boot-4',requested_resolution:'10s',actual_resolution:'10s',series:[]})
    throw new Error(`Unexpected URL: ${url}`)
  })
}

describe('LifecycleDetail',()=>{
  it('keeps URL state shareable and sends the selected UID to the process endpoint',async()=>{
    vi.stubGlobal('crypto',{randomUUID:()=> 'request-id'});const fetchMock=mockFetch();vi.stubGlobal('fetch',fetchMock)
    const pinia=createPinia();setActivePinia(pinia);const store=useAppStore();store.config={backends:[{id:'reboot-trace-4',name:'Dev 4',baseUrl:'https://dev4.example/api/v1'}],requestTimeoutMs:8_000,refreshIntervalMs:5_000};store.hosts=[]
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/hosts/:backendId/lifecycles/:bootId',component:LifecycleDetail},{path:'/hosts/:backendId/lifecycles/:bootId/final',component:{template:'<div />'}},{path:'/hosts/:backendId/lifecycles/:bootId/compare',component:{template:'<div />'}},{path:'/hosts/:backendId/lifecycles/:bootId/processes/:pid',component:{template:'<div />'}},{path:'/hosts/:backendId',component:{template:'<div />'}}]})
    await router.push('/hosts/reboot-trace-4/lifecycles/life?from=1000&to=10000&snapshot=111725&dimension=cpu&process=rg&state=R');await router.isReady()
    const wrapper=mount(LifecycleDetail,{global:{plugins:[pinia,router]}});await flushPromises()
    expect(wrapper.text()).toContain('UID 10597')
    await wrapper.find('tbody tr[tabindex="0"]').trigger('click');await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({from:'1000',to:'10000',snapshot:'111725',dimension:'cpu',uid:'10597',process:'rg',state:'R'})
    expect(fetchMock.mock.calls.some(([url])=>String(url).includes('dimension=cpu&user=10597'))).toBe(true)
    const rssButton=wrapper.findAll('[role="tab"]').find(item=>item.text()==='RSS');expect(rssButton).toBeTruthy();await rssButton!.trigger('click');await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({uid:'10597',process:'rg',state:'R',dimension:'rss'})
    store.lastRefreshAt=Date.now();await flushPromises()
    expect(router.currentRoute.value.query.snapshot).toBe('111725')
    wrapper.unmount()
  })
})
