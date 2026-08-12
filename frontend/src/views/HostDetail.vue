<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '../store'
import { api, fmtBytes, fmtPercent, fmtTime, terminationLabel } from '../api'
import type { Lifecycle } from '../types'

const route = useRoute()
const store = useAppStore()
const items = ref<Lifecycle[]>([])
const error = ref('')
const loading = ref(false)
const nextCursor = ref<string>()
let controller: AbortController | undefined
const backend = computed(() => store.backendById(String(route.params.backendId)))
const host = computed(() => store.hostById(String(route.params.backendId)))

async function loadPage(reset = false) {
  const value = backend.value
  if (!value) return
  controller?.abort()
  controller = new AbortController()
  loading.value = true
  try {
    error.value = ''
    const suffix = reset || !nextCursor.value ? '' : `?cursor=${encodeURIComponent(nextCursor.value)}`
    const response = await api<any>(value, `/lifecycles${suffix}`, store.config?.requestTimeoutMs, controller.signal)
    items.value = reset ? response.items : [...items.value, ...response.items]
    nextCursor.value = response.next_cursor || undefined
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') error.value = (reason as Error).message
  } finally { loading.value = false }
}

watch(backend, async value => {
  controller?.abort()
  if (!value) return
  items.value = []
  nextCursor.value = undefined
  await loadPage(true)
}, { immediate: true })
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <section class="page">
    <div class="page-title"><div><h1>{{ backend?.name || '未知主机' }}</h1><span class="muted">{{ host?.status?.hostname }} · Boot {{ host?.status?.boot_id.slice(0, 8) || '—' }}</span></div></div>
    <p v-if="error" class="warning" role="alert">{{ error }}</p>
    <div v-if="host?.status || host?.latest" class="grid">
      <div class="card metric"><span>状态</span><b>{{ terminationLabel(host.status?.lifecycle.termination || 'unknown') }}</b></div>
      <div class="card metric"><span>CPU</span><b>{{ fmtPercent(host.latest?.system.host_cpu_percent) }}</b></div>
      <div class="card metric"><span>可用内存</span><b>{{ fmtBytes(host.latest?.system.memory_available_bytes) }}</b></div>
      <div class="card metric"><span>持久化</span><b>{{ host.status?.storage.persistence_state || '—' }}</b><small>{{ fmtBytes(host.status?.storage.used_bytes) }} / {{ fmtBytes(host.status?.storage.limit_bytes) }}</small></div>
    </div>
    <details v-if="host?.status?.capabilities"><summary>采集能力</summary><ul><li v-for="(capability, name) in host.status.capabilities" :key="name">{{ name }}：{{ capability.state }}{{ capability.reason ? `（${capability.reason}）` : '' }}</li></ul></details>
    <h2>生命周期记录</h2>
    <div v-if="loading && !items.length" class="skeleton" aria-label="正在加载"></div>
    <table v-else><thead><tr><th>Boot ID</th><th>启动</th><th>最后观察</th><th>状态</th><th>完整度</th><th>快照</th><th></th></tr></thead><tbody><tr v-for="item in items" :key="item.boot_id"><td><code :title="item.boot_id">{{ item.boot_id.slice(0, 8) }}</code></td><td>{{ fmtTime(item.started_at_ms) }}</td><td>{{ fmtTime(item.last_seen_at_ms) }}</td><td><span class="badge">{{ terminationLabel(item.termination) }}</span></td><td>{{ item.retention_state }}</td><td>{{ item.snapshot_count }}</td><td><router-link :to="`/hosts/${route.params.backendId}/lifecycles/${item.boot_id}`">分析</router-link></td></tr></tbody></table>
    <button v-if="nextCursor" :disabled="loading" @click="loadPage(false)">{{ loading ? '加载中…' : '加载更多生命周期' }}</button>
    <p v-if="!loading && !items.length" class="card muted">该主机暂无生命周期记录。</p>
  </section>
</template>
