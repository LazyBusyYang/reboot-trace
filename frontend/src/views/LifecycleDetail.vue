<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {useRoute,useRouter} from 'vue-router'
import {analysisQueryParams,defaultWindow,extractIdentityHints,normalizeAnalysisQuery,shiftWindow,trendResolution,WINDOW_1_HOUR,windowAround} from '../analysis'
import {api,ApiError,fmtTime,requestMeta,terminationLabel} from '../api'
import ProcessTable from '../components/ProcessTable.vue'
import SnapshotTimeline from '../components/SnapshotTimeline.vue'
import TimeRangeToolbar from '../components/TimeRangeToolbar.vue'
import TrendChart from '../components/TrendChart.vue'
import UserSummaryTable from '../components/UserSummaryTable.vue'
import {useAppStore} from '../store'
import type {ApiBase,EventItem,EventPage,FullSnapshot,Lifecycle,ProcessPage,RankedProcess,ResourceDimension,SeriesItem,SeriesResponse,SnapshotPage,SnapshotSummary,UserPage,UserSample} from '../types'

type LifecycleResponse=Lifecycle&ApiBase
const route=useRoute();const router=useRouter();const store=useAppStore()
const lifecycle=ref<LifecycleResponse>();const snapshots=ref<SnapshotSummary[]>([]);const events=ref<EventItem[]>([]);const series=ref<SeriesItem[]>([])
const processes=ref<RankedProcess[]>([]);const users=ref<UserSample[]>([]);const selected=ref<number>();const snapshotCursor=ref<string>();const eventCursor=ref<string>();const processCursor=ref<string>()
const dimension=ref<ResourceDimension>('cpu');const selectedUid=ref<number>();const processKeyword=ref('');const processState=ref('')
const loading=ref(false);const detailLoading=ref(false);const notice=ref('');const error=ref('');const detailError=ref('')
let pageController:AbortController|undefined;let detailController:AbortController|undefined
let pendingNotice=''
const backend=computed(()=>store.backendById(String(route.params.backendId)));const host=computed(()=>store.hostById(String(route.params.backendId)))
const identityHints=computed(()=>selectedUid.value==null?[]:extractIdentityHints(processes.value))
const requestDetails=computed(()=>requestMeta(lifecycle.value))
const historyBase=computed(()=>`/hosts/${route.params.backendId}/lifecycles/${route.params.bootId}`)

function errorText(reason:unknown){
  if(!(reason instanceof ApiError)) return (reason as Error).message||'请求失败'
  const suffix=reason.requestId?`（请求 ID ${reason.requestId}）`:''
  if(reason.code==='DATA_REMOVED'||reason.status===410) return `数据已按保留策略清理，当前只能查看生命周期摘要。${suffix}`
  if(reason.code==='NOT_FOUND'||reason.status===404) return `指定生命周期或快照不存在。${suffix}`
  if(reason.kind==='timeout') return `请求超时，请缩小时间范围后重试。${suffix}`
  if(reason.kind==='forbidden') return `后端拒绝访问，请检查 Ingress 与访问策略。${suffix}`
  if(reason.kind==='incompatible') return `前后端版本不兼容：${reason.message}${suffix}`
  return `${reason.message}${suffix}`
}

async function replaceQuery(changes:Record<string,string|undefined>){
  const next={...route.query}
  for(const [key,value] of Object.entries(changes)) value==null||value===''?delete next[key]:next[key]=value
  await router.replace({query:next})
}

async function loadDetails(snapshotId:number){
  const target=backend.value;if(!target)return
  detailController?.abort();detailController=new AbortController();detailLoading.value=true
  try{
    detailError.value=''
    const userSuffix=selectedUid.value==null?'':`&user=${selectedUid.value}`
    const base=`/lifecycles/${route.params.bootId}/snapshots/${snapshotId}`
    const [processResult,userResult]=await Promise.all([
      api<ProcessPage>(target,`${base}/processes?dimension=${dimension.value}${userSuffix}`,store.config?.requestTimeoutMs,detailController.signal),
      api<UserPage>(target,`${base}/users`,store.config?.requestTimeoutMs,detailController.signal),
    ])
    processes.value=processResult.items;processCursor.value=processResult.next_cursor||undefined;users.value=userResult.items
  }catch(reason){if((reason as Error).name!=='AbortError')detailError.value=errorText(reason)}finally{detailLoading.value=false}
}

async function loadPage(){
  const target=backend.value;if(!target)return
  pageController?.abort();pageController=new AbortController();loading.value=true
  try{
    error.value='';notice.value=pendingNotice;pendingNotice=''
    const base=`/lifecycles/${route.params.bootId}`
    const current=await api<LifecycleResponse>(target,base,store.config?.requestTimeoutMs,pageController.signal)
    lifecycle.value=current
    const normalized=normalizeAnalysisQuery(route.query as Record<string,unknown>,current)
    dimension.value=normalized.value.dimension;selectedUid.value=normalized.value.uid;processKeyword.value=normalized.value.process;processState.value=normalized.value.state
    if(normalized.notice)notice.value=normalized.notice
    if(normalized.changed){await router.replace({query:analysisQueryParams(normalized.value)});return}
    const {from,to,snapshot}=normalized.value
    if(snapshot!=null){
      const exact=await api<FullSnapshot>(target,`${base}/snapshots/${snapshot}`,store.config?.requestTimeoutMs,pageController.signal)
      if(exact.captured_at_ms<from||exact.captured_at_ms>to){
        const adjusted=windowAround(exact.captured_at_ms,current)
        pendingNotice='指定快照不在当前窗口，已自动调整到其附近十分钟。'
        await router.replace({query:analysisQueryParams({...normalized.value,...adjusted})});return
      }
      if(['downsampled','summary_only'].includes(exact.detail_level))detailError.value='该快照不包含可查看的进程明细。'
    }
    const resolution=trendResolution(from,to)
    const [snapshotResult,eventResult,seriesResult]=await Promise.all([
      api<SnapshotPage>(target,`${base}/snapshots?from_ms=${from}&to_ms=${to}&limit=200`,store.config?.requestTimeoutMs,pageController.signal),
      api<EventPage>(target,`${base}/events`,store.config?.requestTimeoutMs,pageController.signal),
      api<SeriesResponse>(target,`${base}/series?metrics=host_cpu_percent,load1,memory_available_bytes,swap_used_bytes,psi_memory_full_avg10&from_ms=${from}&to_ms=${to}&resolution=${resolution}`,store.config?.requestTimeoutMs,pageController.signal),
    ])
    snapshots.value=snapshotResult.items;snapshotCursor.value=snapshotResult.next_cursor||undefined
    events.value=eventResult.items.filter(item=>item.occurred_at_ms>=from&&item.occurred_at_ms<=to);eventCursor.value=eventResult.next_cursor||undefined
    series.value=seriesResult.series
    const candidate=snapshot!=null?snapshots.value.find(item=>item.id===snapshot):snapshots.value.find(item=>!['downsampled','summary_only'].includes(item.detail_level))
    if(snapshot!=null&&!candidate)throw new ApiError('指定快照未出现在调整后的窗口中','backend',404,'NOT_FOUND')
    if(candidate&&!['downsampled','summary_only'].includes(candidate.detail_level)){
      selected.value=candidate.id
      if(snapshot==null){await replaceQuery({snapshot:String(candidate.id)});return}
      await loadDetails(candidate.id)
    }else{selected.value=undefined;processes.value=[];users.value=[]}
  }catch(reason){if((reason as Error).name!=='AbortError')error.value=errorText(reason)}finally{loading.value=false}
}

async function loadMoreSnapshots(){
  if(!backend.value||!snapshotCursor.value)return
  const response=await api<SnapshotPage>(backend.value,`/lifecycles/${route.params.bootId}/snapshots?from_ms=${route.query.from}&to_ms=${route.query.to}&limit=200&cursor=${encodeURIComponent(snapshotCursor.value)}`,store.config?.requestTimeoutMs)
  snapshots.value.push(...response.items);snapshotCursor.value=response.next_cursor||undefined
}
async function loadMoreEvents(){
  if(!backend.value||!eventCursor.value)return
  const response=await api<EventPage>(backend.value,`/lifecycles/${route.params.bootId}/events?limit=1000&cursor=${encodeURIComponent(eventCursor.value)}`,store.config?.requestTimeoutMs)
  const from=Number(route.query.from),to=Number(route.query.to);events.value.push(...response.items.filter(item=>item.occurred_at_ms>=from&&item.occurred_at_ms<=to));eventCursor.value=response.next_cursor||undefined
}
async function loadMoreProcesses(){
  if(!backend.value||selected.value==null||!processCursor.value)return
  const userSuffix=selectedUid.value==null?'':`&user=${selectedUid.value}`
  const response=await api<ProcessPage>(backend.value,`/lifecycles/${route.params.bootId}/snapshots/${selected.value}/processes?dimension=${dimension.value}${userSuffix}&cursor=${encodeURIComponent(processCursor.value)}`,store.config?.requestTimeoutMs)
  processes.value.push(...response.items);processCursor.value=response.next_cursor||undefined
}
function setWindow(from:number,to:number){void replaceQuery({from:String(from),to:String(to),snapshot:undefined})}
function preset(value:'10m'|'1h'|'all'){
  if(!lifecycle.value)return
  if(value==='all')setWindow(lifecycle.value.started_at_ms,lifecycle.value.last_seen_at_ms)
  else{const window=defaultWindow(lifecycle.value,value==='1h'?WINDOW_1_HOUR:undefined);setWindow(window.from,window.to)}
}
function move(direction:-1|1){if(!lifecycle.value)return;const window=shiftWindow(Number(route.query.from),Number(route.query.to),direction,lifecycle.value);setWindow(window.from,window.to)}
function selectSnapshot(id:number){void replaceQuery({snapshot:String(id)})}
async function locateSnapshot(id:number){await replaceQuery({snapshot:String(id)})}
async function locateTime(timestamp:number){
  if(!backend.value||!lifecycle.value)return
  loading.value=true
  try{
    const target=Math.max(lifecycle.value.started_at_ms,Math.min(timestamp,lifecycle.value.last_seen_at_ms))
    const result=await api<SnapshotPage>(backend.value,`/lifecycles/${route.params.bootId}/snapshots?to_ms=${target}&limit=1`,store.config?.requestTimeoutMs)
    if(!result.items.length){notice.value='目标时间之前没有可用快照。';return}
    await replaceQuery({snapshot:String(result.items[0].id)})
  }catch(reason){error.value=errorText(reason)}finally{loading.value=false}
}
function selectUser(uid:number|undefined){void replaceQuery({uid:uid==null?undefined:String(uid)})}
function setDimension(value:ResourceDimension){void replaceQuery({dimension:value})}
function setProcessKeyword(value:string){processKeyword.value=value;void replaceQuery({process:value.trim()||undefined})}
function setProcessState(value:string){processState.value=value;void replaceQuery({state:value||undefined})}

watch([backend,()=>store.lastRefreshAt,()=>route.params.bootId,()=>route.query.from,()=>route.query.to,()=>route.query.snapshot,()=>route.query.dimension,()=>route.query.uid],loadPage,{immediate:true})
watch(()=>route.query.process,value=>{processKeyword.value=typeof value==='string'?value:''})
watch(()=>route.query.state,value=>{processState.value=typeof value==='string'?value:''})
onBeforeUnmount(()=>{pageController?.abort();detailController?.abort()})
</script>

<template>
  <section class="page">
    <div class="page-title"><div><p class="breadcrumb"><router-link :to="`/hosts/${route.params.backendId}`">{{ backend?.name }}</router-link> / 生命周期</p><h1>容器实例 {{ String(route.params.bootId).slice(0,8) }}</h1><span v-if="lifecycle" class="badge">{{ terminationLabel(lifecycle.termination) }}</span><p v-if="lifecycle" class="muted">Kernel boot {{ lifecycle.boot_id.slice(0,8) }} · {{ lifecycle.detection_method||'legacy' }} / {{ lifecycle.detection_confidence||'unknown' }}</p></div><div class="actions"><router-link :to="`${route.path}/final`"><button>最终证据</button></router-link><router-link v-if="snapshots.length>1" :to="`${route.path}/compare?left=${snapshots.at(-1)?.id}&right=${snapshots[0]?.id}`"><button>快照对比</button></router-link></div></div>
    <p v-if="notice" class="status-note" role="status">{{ notice }}</p><p v-if="error" class="warning" role="alert">{{ error }}</p>
    <div v-if="lifecycle" class="grid"><div class="card"><h2>时间范围</h2><p>{{ fmtTime(lifecycle.started_at_ms) }} — {{ fmtTime(lifecycle.last_seen_at_ms) }}</p></div><div class="card"><h2>证据完整度</h2><p>{{ lifecycle.retention_state||'无法确定' }}</p></div><div class="card"><h2>快照数量</h2><p>{{ lifecycle.snapshot_count }}</p></div></div>
    <TimeRangeToolbar v-if="lifecycle&&route.query.from&&route.query.to" :from="Number(route.query.from)" :to="Number(route.query.to)" :started-at="lifecycle.started_at_ms" :ended-at="lifecycle.last_seen_at_ms" :snapshot-count="snapshots.length" :loading="loading" @apply="setWindow" @preset="preset" @shift="move" @end="preset('10m')" @locate-snapshot="locateSnapshot" @locate-time="locateTime" />
    <h2>系统趋势（当前窗口）</h2><TrendChart :series="series" :events="events" />
    <div class="section-heading"><h2>快照时间线</h2><span v-if="loading" class="muted">正在加载…</span></div>
    <SnapshotTimeline :items="snapshots" :selected="selected" :next-cursor="snapshotCursor" :loading="loading" @select="selectSnapshot" @more="loadMoreSnapshots" />
    <p v-if="!loading&&!snapshots.length" class="empty-state">当前窗口没有快照。数据可能尚未采样、已被清理，或需要调整时间范围。</p>
    <p v-if="detailError" class="warning" role="alert">{{ detailError }}</p>
    <template v-if="selected">
      <UserSummaryTable :items="users" :selected-uid="selectedUid" @select="selectUser" />
      <ProcessTable :items="processes" :dimension="dimension" :uid="selectedUid" :keyword="processKeyword" :state="processState" :hints="identityHints" :next-cursor="processCursor" :loading="detailLoading" :history-base="historyBase" @dimension="setDimension" @keyword="setProcessKeyword" @state="setProcessState" @more="loadMoreProcesses" />
    </template>
    <h2>事件</h2><ul class="event-list"><li v-for="event in events" :key="event.id"><time>{{ fmtTime(event.occurred_at_ms) }}</time> · {{ event.type }}</li></ul><button v-if="eventCursor" @click="loadMoreEvents">加载更多事件</button><p v-if="!events.length" class="muted">当前窗口没有记录事件。</p>
    <details class="diagnostics"><summary>连接与诊断信息</summary><dl><dt>Backend URL</dt><dd><code>{{ backend?.baseUrl }}</code></dd><dt>Host ID</dt><dd><code>{{ lifecycle?.host_id||host?.status?.host_id||'—' }}</code></dd><dt>Hostname</dt><dd>{{ host?.status?.hostname||'—' }}</dd><dt>生命周期</dt><dd><code>{{ lifecycle?.lifecycle_key||route.params.bootId }}</code></dd><dt>API / Schema</dt><dd>{{ lifecycle?.api_version||'—' }} / {{ lifecycle?.schema_version||'—' }}</dd><dt>最近成功请求</dt><dd>{{ requestDetails?fmtTime(requestDetails.completedAt):'—' }}</dd><dt>请求 ID</dt><dd><code>{{ requestDetails?.requestId||'—' }}</code></dd></dl></details>
  </section>
</template>
