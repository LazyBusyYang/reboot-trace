<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '../store'
import { api, fmtBytes, fmtPercent, fmtTime, terminationLabel } from '../api'
import TrendChart from '../components/TrendChart.vue'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const lifecycle = ref<any>()
const snapshots = ref<any[]>([])
const events = ref<any[]>([])
const series = ref<any[]>([])
const processes = ref<any[]>([])
const users = ref<any[]>([])
const selected = ref<number>()
const snapshotCursor = ref<string>()
const eventCursor = ref<string>()
const processCursor = ref<string>()
const dimension = ref(String(route.query.dimension || 'cpu'))
const error = ref('')
const detailError = ref('')
let pageController: AbortController | undefined
let detailController: AbortController | undefined
const backend = computed(() => store.backendById(String(route.params.backendId)))

async function selectSnapshot(id: number) {
  const target = backend.value
  if (!target) return
  detailController?.abort()
  detailController = new AbortController()
  selected.value = id
  await router.replace({ query: { ...route.query, snapshot: String(id), dimension: dimension.value } })
  const base = `/lifecycles/${route.params.bootId}/snapshots/${id}`
  try {
    detailError.value = ''
    const [processResult, userResult] = await Promise.all([
      api<any>(target, `${base}/processes?dimension=${dimension.value}`, store.config?.requestTimeoutMs, detailController.signal),
      api<any>(target, `${base}/users`, store.config?.requestTimeoutMs, detailController.signal),
    ])
    processes.value = processResult.items
    processCursor.value = processResult.next_cursor || undefined
    users.value = userResult.items
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') detailError.value = (reason as Error).message
  }
}

async function loadMoreSnapshots() {
  const target = backend.value
  if (!target || !snapshotCursor.value) return
  const base = `/lifecycles/${route.params.bootId}`
  const from = Number(route.query.from)
  const to = Number(route.query.to)
  const response = await api<any>(target, `${base}/snapshots?from_ms=${from}&to_ms=${to}&limit=200&cursor=${encodeURIComponent(snapshotCursor.value)}`, store.config?.requestTimeoutMs)
  snapshots.value.push(...response.items)
  snapshotCursor.value = response.next_cursor || undefined
}

async function loadMoreEvents() {
  const target = backend.value
  if (!target || !eventCursor.value) return
  const base = `/lifecycles/${route.params.bootId}`
  const response = await api<any>(target, `${base}/events?limit=1000&cursor=${encodeURIComponent(eventCursor.value)}`, store.config?.requestTimeoutMs)
  const from = Number(route.query.from)
  const to = Number(route.query.to)
  events.value.push(...response.items.filter((event: any) => event.occurred_at_ms >= from && event.occurred_at_ms <= to))
  eventCursor.value = response.next_cursor || undefined
}

async function loadMoreProcesses() {
  const target = backend.value
  if (!target || !selected.value || !processCursor.value) return
  const base = `/lifecycles/${route.params.bootId}/snapshots/${selected.value}`
  const response = await api<any>(target, `${base}/processes?dimension=${dimension.value}&cursor=${encodeURIComponent(processCursor.value)}`, store.config?.requestTimeoutMs)
  processes.value.push(...response.items)
  processCursor.value = response.next_cursor || undefined
}

watch(dimension, () => { if (selected.value) void selectSnapshot(selected.value) })
watch([backend, () => store.lastRefreshAt], async ([value]) => {
  pageController?.abort()
  pageController = new AbortController()
  if (!value) return
  try {
    error.value = ''
    const base = `/lifecycles/${route.params.bootId}`
    lifecycle.value = await api<any>(value, base, store.config?.requestTimeoutMs, pageController.signal)
    const defaultTo = lifecycle.value.last_seen_at_ms
    const to = Number(route.query.to || defaultTo)
    const from = Number(route.query.from || Math.max(lifecycle.value.started_at_ms, to - 10 * 60_000))
    if (!route.query.from || !route.query.to) await router.replace({ query: { ...route.query, from: String(from), to: String(to), dimension: dimension.value } })
    const [snapshotResult, eventResult, seriesResult] = await Promise.all([
      api<any>(value, `${base}/snapshots?from_ms=${from}&to_ms=${to}&limit=200`, store.config?.requestTimeoutMs, pageController.signal),
      api<any>(value, `${base}/events`, store.config?.requestTimeoutMs, pageController.signal),
      api<any>(value, `${base}/series?metrics=host_cpu_percent,load1,memory_available_bytes,swap_used_bytes,psi_memory_full_avg10&from_ms=${from}&to_ms=${to}&resolution=10s`, store.config?.requestTimeoutMs, pageController.signal),
    ])
    snapshots.value = snapshotResult.items
    snapshotCursor.value = snapshotResult.next_cursor || undefined
    events.value = eventResult.items.filter((event: any) => event.occurred_at_ms >= from && event.occurred_at_ms <= to)
    eventCursor.value = eventResult.next_cursor || undefined
    series.value = seriesResult.series
    const requested = Number(route.query.snapshot)
    const chosen = snapshots.value.find(item => item.id === requested && item.detail_level !== 'downsampled' && item.detail_level !== 'summary_only')?.id
      ?? snapshots.value.find(item => item.detail_level !== 'downsampled' && item.detail_level !== 'summary_only')?.id
    if (chosen) await selectSnapshot(chosen)
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') error.value = (reason as Error).message
  }
}, { immediate: true })
onBeforeUnmount(() => { pageController?.abort(); detailController?.abort() })
</script>

<template>
  <section class="page">
    <div class="page-title"><div><p class="breadcrumb"><router-link :to="`/hosts/${route.params.backendId}`">{{ backend?.name }}</router-link> / 生命周期</p><h1>生命周期 {{ String(route.params.bootId).slice(0, 8) }}</h1><span v-if="lifecycle" class="badge">{{ terminationLabel(lifecycle.termination) }}</span></div><div class="actions"><router-link :to="`${route.path}/final`"><button>最终证据</button></router-link><router-link v-if="snapshots.length > 1" :to="`${route.path}/compare?left=${snapshots.at(-1)?.id}&right=${snapshots[0]?.id}`"><button>快照对比</button></router-link></div></div>
    <p v-if="error" class="warning" role="alert">{{ error }}</p>
    <div v-if="lifecycle" class="grid"><div class="card"><h2>时间范围</h2><p>{{ fmtTime(lifecycle.started_at_ms) }} — {{ fmtTime(lifecycle.last_seen_at_ms) }}</p></div><div class="card"><h2>证据完整度</h2><p>{{ lifecycle.retention_state || '无法确定' }}</p></div><div class="card"><h2>快照数量</h2><p>{{ lifecycle.snapshot_count }}</p></div></div>
    <h2>系统趋势（当前窗口）</h2><TrendChart :series="series" :events="events" />
    <h2>快照时间线</h2>
    <table><thead><tr><th>采样时间</th><th>耗时</th><th>漂移</th><th>详情</th><th>持久化状态</th><th></th></tr></thead><tbody><tr v-for="snapshot in snapshots" :key="snapshot.id" :class="{ selected: selected === snapshot.id }"><td>{{ fmtTime(snapshot.captured_at_ms) }}</td><td>{{ snapshot.duration_ms }} ms</td><td>{{ snapshot.captured_at_ms - snapshot.scheduled_at_ms }} ms</td><td>{{ snapshot.detail_level }}</td><td>{{ snapshot.persistence_state }}</td><td><button :disabled="['downsampled', 'summary_only'].includes(snapshot.detail_level)" @click="selectSnapshot(snapshot.id)">查看明细</button></td></tr></tbody></table>
    <button v-if="snapshotCursor" @click="loadMoreSnapshots">加载更多快照</button>
    <p v-if="!snapshots.length" class="card muted">当前十分钟窗口没有快照，可能尚未采样或数据已被清理。</p>
    <p v-if="detailError" class="warning" role="alert">{{ detailError }}</p>
    <template v-if="selected">
      <h2>所选快照用户汇总</h2><table><thead><tr><th>用户</th><th>CPU</th><th>RSS</th><th>Swap</th><th>读/写</th><th>进程/线程</th></tr></thead><tbody><tr v-for="user in users" :key="user.uid"><td>{{ user.username ?? user.uid }}</td><td>{{ fmtPercent(user.cpu_percent) }}</td><td>{{ fmtBytes(user.rss_bytes) }}</td><td>{{ fmtBytes(user.swap_bytes) }}</td><td>{{ fmtBytes(user.read_bps) }}/s · {{ fmtBytes(user.write_bps) }}/s</td><td>{{ user.process_count }}/{{ user.thread_count }}</td></tr></tbody></table>
      <h2>进程排行</h2><div class="tabs" role="tablist"><button v-for="item in ['cpu', 'rss', 'swap', 'read', 'write']" :key="item" :class="{ active: dimension === item }" role="tab" :aria-selected="dimension === item" @click="dimension = item">{{ item.toUpperCase() }}</button></div>
      <table><thead><tr><th>#</th><th>用户</th><th>PID</th><th>进程</th><th>线程</th><th>状态</th><th>值</th><th>脱敏命令</th></tr></thead><tbody><tr v-for="process in processes" :key="`${process.pid}-${process.create_time_ms}`"><td>{{ process.rank }}</td><td>{{ process.username ?? process.uid }}</td><td>{{ process.pid }}</td><td>{{ process.comm }}</td><td>{{ process.threads ?? '—' }}</td><td>{{ process.state ?? '—' }}</td><td>{{ process.value }}</td><td><code :title="process.cmdline_redacted">{{ process.cmdline_redacted }}{{ process.truncated ? ' …（已截断）' : '' }}</code><small v-if="process.redaction_status === 'failed_closed'"> 脱敏失败，已隐藏</small></td></tr></tbody></table>
      <button v-if="processCursor" @click="loadMoreProcesses">加载更多进程</button>
    </template>
    <h2>事件</h2><ul class="event-list"><li v-for="event in events" :key="event.id"><time>{{ fmtTime(event.occurred_at_ms) }}</time> · {{ event.type }}</li></ul><button v-if="eventCursor" @click="loadMoreEvents">加载更多事件</button><p v-if="!events.length" class="muted">当前窗口没有记录事件。</p>
  </section>
</template>
