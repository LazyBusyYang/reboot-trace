<script setup lang="ts">
import { computed, ref } from 'vue'
import { useAppStore } from '../store'
import { fmtBytes, fmtPercent, fmtTime, terminationLabel } from '../api'

const store = useAppStore()
const search = ref('')
const filter = ref('all')
const sorted = computed(() => store.hosts
  .filter(host => host.config.name.toLowerCase().includes(search.value.toLowerCase()))
  .filter(host => filter.value === 'all' || filter.value === 'offline' && !!host.error || filter.value === 'partial' && !!host.warning || filter.value === 'online' && !host.error && !host.warning)
  .sort((left, right) => (left.error ? 0 : left.warning ? 1 : 2) - (right.error ? 0 : right.warning ? 1 : 2)))
</script>

<template>
  <section class="page">
    <div class="page-title"><div><h1>多主机总览</h1><span class="muted">成功 {{ store.hosts.filter(h => h.status || h.latest).length }} / {{ store.hosts.length }} 个后端</span></div></div>
    <div class="toolbar">
      <label>搜索主机 <input v-model="search" type="search" placeholder="Maoshanwang_dev_…"></label>
      <label>状态 <select v-model="filter"><option value="all">全部</option><option value="offline">不可达</option><option value="partial">部分数据</option><option value="online">正常</option></select></label>
    </div>
    <div class="grid">
      <div v-for="host in sorted" :key="host.config.id">
        <div v-if="host.loading" class="skeleton" aria-label="正在加载"></div>
        <article v-else :class="['card', host.error && 'error']">
          <div class="card-title">
            <router-link :to="`/hosts/${host.config.id}`"><h2><span :class="['dot', host.error ? 'bad' : host.warning ? 'pulse' : 'good']"></span>{{ host.config.name }}</h2></router-link>
            <button v-if="host.error || host.warning" @click="store.refreshHost(host)">重试</button>
          </div>
          <p v-if="host.error" class="warning" role="alert">{{ host.error }}<br><small>上次成功：{{ fmtTime(host.lastSuccessAt) }}</small></p>
          <p v-else-if="host.warning" class="warning">{{ host.warning }}</p>
          <p v-if="host.status"><span class="badge">{{ terminationLabel(host.status.lifecycle.termination) }}</span> 容器实例 {{ host.status.lifecycle_key?.slice(0, 8) || '旧版未知' }} · Kernel boot {{ host.status.boot_id.slice(0, 8) }} · uptime {{ Math.round((host.latest?.system.uptime_seconds || 0) / 60) }} 分钟</p>
          <div v-if="host.latest" class="metrics">
            <div class="metric"><span>CPU</span><b>{{ fmtPercent(host.latest.system.host_cpu_percent) }}</b></div>
            <div class="metric"><span>可用内存</span><b>{{ fmtBytes(host.latest.system.memory_available_bytes) }}</b></div>
            <div class="metric"><span>Memory PSI full</span><b>{{ host.latest.system.psi_memory_full_avg10?.toFixed(1) ?? '—' }}</b></div>
            <div class="metric"><span>存储</span><b>{{ Math.round(host.latest.storage.used_bytes / host.latest.storage.limit_bytes * 100) }}%</b><small>{{ host.latest.storage.persistence_state }}</small></div>
          </div>
          <p class="muted">最后证据：{{ fmtTime(host.latest?.captured_at_ms) }} · 采样漂移 {{ host.latest?.collector.schedule_delay_ms ?? '—' }} ms · 时钟偏差约 {{ host.clockOffsetMs ?? '—' }} ms</p>
        </article>
      </div>
    </div>
    <p v-if="!sorted.length" class="card muted">没有符合筛选条件的主机。请清除筛选条件。</p>
  </section>
</template>
