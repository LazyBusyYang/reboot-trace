<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '../store'
import { api, fmtBytes, fmtPercent, fmtTime, terminationLabel } from '../api'

const route = useRoute()
const router = useRouter()
const store = useAppStore()
const data = ref<any>()
const dimension = ref('cpu')
const count = ref(Number(route.query.count || 3))
const error = ref('')
let controller: AbortController | undefined
const backend = computed(() => store.backendById(String(route.params.backendId)))
const current = computed(() => data.value?.snapshots?.[0])
const ranked = computed(() => current.value?.top?.[dimension.value] || [])

async function load() {
  const target = backend.value
  if (!target) return
  controller?.abort()
  controller = new AbortController()
  try {
    error.value = ''
    await router.replace({ query: { ...route.query, count: String(count.value) } })
    data.value = await api<any>(target, `/lifecycles/${route.params.bootId}/final?count=${count.value}`, store.config?.requestTimeoutMs, controller.signal)
  } catch (reason) {
    if ((reason as Error).name !== 'AbortError') error.value = (reason as Error).message
  }
}
watch([backend, () => store.lastRefreshAt], load, { immediate: true })
watch(count, load)
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <section class="page">
    <div class="page-title"><div><p class="breadcrumb"><router-link :to="`/hosts/${route.params.backendId}/lifecycles/${route.params.bootId}`">生命周期 {{ String(route.params.bootId).slice(0, 8) }}</router-link> / 最终证据</p><h1>最终证据</h1><span v-if="data" class="badge">{{ terminationLabel(data.termination) }}</span></div><label>末尾快照数 <select v-model.number="count"><option v-for="value in [1, 3, 5, 10]" :key="value" :value="value">{{ value }}</option></select></label></div>
    <p class="warning">这里只展示最后成功完整落盘的证据，不自动判断重启根因。高资源占用只表示观察到压力。</p>
    <p v-if="error" class="warning" role="alert">{{ error }}</p>
    <template v-if="data && current">
      <p>最终完整快照：{{ fmtTime(current.captured_at_ms) }} · 最后成功落盘：{{ fmtTime(data.last_persisted_at_ms) }} · 证据缺口：{{ data.evidence_gap_ms ?? '无法确定' }}</p>
      <div class="grid">
        <div class="card metric"><span>CPU</span><b>{{ fmtPercent(current.system.host_cpu_percent) }}</b></div>
        <div class="card metric"><span>Load 1/5/15</span><b>{{ current.system.load1 ?? '—' }} / {{ current.system.load5 ?? '—' }} / {{ current.system.load15 ?? '—' }}</b></div>
        <div class="card metric"><span>可用内存</span><b>{{ fmtBytes(current.system.memory_available_bytes) }}</b></div>
        <div class="card metric"><span>Swap</span><b>{{ fmtBytes(current.system.swap_used_bytes) }}</b></div>
        <div class="card metric"><span>CPU PSI some</span><b>{{ current.system.psi_cpu_some_avg10 ?? '—' }}</b></div>
        <div class="card metric"><span>Memory PSI full</span><b>{{ current.system.psi_memory_full_avg10 ?? '—' }}</b></div>
        <div class="card metric"><span>I/O PSI full</span><b>{{ current.system.psi_io_full_avg10 ?? '—' }}</b></div>
        <div class="card metric"><span>采样漂移</span><b>{{ current.captured_at_ms - current.scheduled_at_ms }} ms</b></div>
      </div>
      <div class="actions compare-action"><router-link v-if="data.snapshots.length > 1" :to="`/hosts/${route.params.backendId}/lifecycles/${route.params.bootId}/compare?left=${data.snapshots[1].id}&right=${data.snapshots[0].id}`"><button>查看最终快照前后变化</button></router-link></div>
      <h2>用户汇总</h2><table><thead><tr><th>用户</th><th>CPU</th><th>RSS</th><th>Swap</th><th>读/写</th><th>进程/线程</th></tr></thead><tbody><tr v-for="user in current.users" :key="user.uid"><td>{{ user.username ?? user.uid }}</td><td>{{ fmtPercent(user.cpu_percent) }}</td><td>{{ fmtBytes(user.rss_bytes) }}</td><td>{{ fmtBytes(user.swap_bytes) }}</td><td>{{ fmtBytes(user.read_bps) }}/s · {{ fmtBytes(user.write_bps) }}/s</td><td>{{ user.process_count }}/{{ user.thread_count }}</td></tr></tbody></table>
      <div class="tabs" role="tablist"><button v-for="item in ['cpu', 'rss', 'swap', 'read', 'write']" :key="item" :class="{ active: dimension === item }" role="tab" :aria-selected="dimension === item" @click="dimension = item">{{ item.toUpperCase() }}</button></div>
      <table><thead><tr><th>#</th><th>用户</th><th>PID</th><th>进程</th><th>线程</th><th>状态</th><th>值</th><th>脱敏命令</th></tr></thead><tbody><tr v-for="process in ranked" :key="`${process.pid}-${process.create_time_ms}`"><td>{{ process.ranks[dimension].rank }}</td><td>{{ process.username ?? process.uid }}</td><td>{{ process.pid }}</td><td>{{ process.comm }}</td><td>{{ process.threads ?? '—' }}</td><td>{{ process.state ?? '—' }}</td><td>{{ process.ranks[dimension].value }}</td><td><pre class="command">{{ process.cmdline_redacted }}{{ process.truncated ? ' …（已截断）' : '' }}<small v-if="process.redaction_status === 'failed_closed'"> 脱敏失败，已隐藏</small></pre></td></tr></tbody></table>
    </template>
  </section>
</template>
