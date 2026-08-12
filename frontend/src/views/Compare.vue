<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '../store'
import { api, fmtBytes, fmtTime } from '../api'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const data = ref<any>()
const snapshots = ref<any[]>([])
const left = ref(Number(route.query.left))
const right = ref(Number(route.query.right))
const error = ref('')
const nextCursor = ref<string>()
let listController: AbortController | undefined
let compareController: AbortController | undefined
const backend = computed(() => store.backendById(String(route.params.backendId)))
const growing = computed(() => [...(data.value?.processes?.continued || [])].sort((a: any, b: any) => ((b.right.rss_bytes ?? 0) - (b.left.rss_bytes ?? 0)) - ((a.right.rss_bytes ?? 0) - (a.left.rss_bytes ?? 0))))

async function loadList(more = false) {
  const target = backend.value
  if (!target) return
  listController?.abort()
  listController = new AbortController()
  try {
    const cursor = more && nextCursor.value ? `&cursor=${encodeURIComponent(nextCursor.value)}` : ''
    const response = await api<any>(target, `/lifecycles/${route.params.bootId}/snapshots?limit=100${cursor}`, store.config?.requestTimeoutMs, listController.signal)
    const page = response.items.filter((item: any) => !['downsampled', 'summary_only'].includes(item.detail_level))
    snapshots.value = more ? [...snapshots.value, ...page] : page
    nextCursor.value = response.next_cursor || undefined
    if (!left.value) left.value = snapshots.value.at(-1)?.id
    if (!right.value) right.value = snapshots.value[0]?.id
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') error.value = (reason as Error).message
  }
}
async function loadComparison() {
  const target = backend.value
  if (!target || !left.value || !right.value) return
  compareController?.abort()
  compareController = new AbortController()
  try {
    error.value = ''
    await router.replace({ query: { left: String(left.value), right: String(right.value) } })
    data.value = await api<any>(target, `/lifecycles/${route.params.bootId}/compare?left_snapshot_id=${left.value}&right_snapshot_id=${right.value}`, store.config?.requestTimeoutMs, compareController.signal)
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') error.value = (reason as Error).message
  }
}
function swap() { const value = left.value; left.value = right.value; right.value = value }
watch(backend, () => loadList(false), { immediate: true })
watch([backend, left, right], loadComparison, { immediate: true })
onBeforeUnmount(() => { listController?.abort(); compareController?.abort() })
</script>

<template>
  <section class="page">
    <p class="breadcrumb"><router-link :to="`/hosts/${route.params.backendId}/lifecycles/${route.params.bootId}`">生命周期 {{ String(route.params.bootId).slice(0, 8) }}</router-link> / 对比</p><h1>快照对比</h1>
    <div class="toolbar compare-picker"><label>左侧（较早）<select v-model.number="left"><option v-for="snapshot in snapshots" :key="snapshot.id" :value="snapshot.id">{{ fmtTime(snapshot.captured_at_ms) }}</option></select></label><button aria-label="交换左右快照" @click="swap">⇄ 交换</button><label>右侧（较晚）<select v-model.number="right"><option v-for="snapshot in snapshots" :key="snapshot.id" :value="snapshot.id">{{ fmtTime(snapshot.captured_at_ms) }}</option></select></label></div>
    <button v-if="nextCursor" @click="loadList(true)">加载更早快照</button>
    <p v-if="left && right && left === right" class="warning">左右选择了同一快照，变化值将为零。</p>
    <p v-if="error" class="warning" role="alert">{{ error }}</p>
    <template v-if="data">
      <div class="grid"><div class="card"><h2>新增进程</h2><b>{{ data.processes.created.length }}</b></div><div class="card"><h2>退出进程</h2><b>{{ data.processes.exited.length }}</b></div><div class="card"><h2>持续进程</h2><b>{{ data.processes.continued.length }}</b></div><div class="card"><h2>PID 复用</h2><b>{{ data.processes.pid_reused.length }}</b></div></div>
      <h2>系统指标变化</h2><table><thead><tr><th>指标</th><th>左</th><th>右</th><th>绝对变化</th><th>百分比</th></tr></thead><tbody><tr v-for="delta in data.system_deltas" :key="delta.metric"><td>{{ delta.metric }}（{{ delta.unit }}）</td><td>{{ delta.left ?? '—' }}</td><td>{{ delta.right ?? '—' }}</td><td>{{ delta.absolute_delta ?? '—' }}</td><td>{{ delta.percent_delta == null ? '—' : `${delta.percent_delta.toFixed(1)}%` }}</td></tr></tbody></table>
      <h2>持续进程（按 RSS 增幅）</h2><table><thead><tr><th>用户</th><th>PID</th><th>进程</th><th>左 RSS</th><th>右 RSS</th><th>RSS 增幅</th><th>CPU 变化</th></tr></thead><tbody><tr v-for="item in growing" :key="`${item.identity.pid}-${item.identity.create_time_ms}`"><td>{{ item.right.username ?? item.right.uid }}</td><td>{{ item.identity.pid }}</td><td>{{ item.right.comm }}</td><td>{{ fmtBytes(item.left.rss_bytes) }}</td><td>{{ fmtBytes(item.right.rss_bytes) }}</td><td>{{ fmtBytes(item.right.rss_bytes == null || item.left.rss_bytes == null ? null : item.right.rss_bytes - item.left.rss_bytes) }}</td><td>{{ item.right.cpu_percent == null || item.left.cpu_percent == null ? '—' : `${(item.right.cpu_percent - item.left.cpu_percent).toFixed(1)}%` }}</td></tr></tbody></table>
    </template>
  </section>
</template>
