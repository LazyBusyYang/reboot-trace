<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useAppStore } from './store'
import { useRoute } from 'vue-router'

const store = useAppStore()
const route = useRoute()
const timer = ref<number>()
const clockTimer = ref<number>()
const currentTime = ref(Date.now())
const hostSearch = ref('')

function refreshVisible(force = false) {
  const backendId = route.params.backendId
  if (backendId) return store.refreshBackend(String(backendId), force)
  return store.refresh(force)
}
function visibleAgain() {
  if (!document.hidden) void refreshVisible(false)
}

onMounted(async () => {
  await store.initialize()
  if (store.config) timer.value = window.setInterval(() => {
    if (!document.hidden) void refreshVisible(false)
  }, store.config.refreshIntervalMs)
  clockTimer.value = window.setInterval(() => { currentTime.value = Date.now() }, 1000)
  document.addEventListener('visibilitychange', visibleAgain)
})

onUnmounted(() => {
  clearInterval(timer.value)
  clearInterval(clockTimer.value)
  document.removeEventListener('visibilitychange', visibleAgain)
})
</script>

<template>
  <header>
    <router-link to="/" class="brand">Reboot Trace</router-link>
    <div class="header-actions" aria-live="polite">
      <time :datetime="new Date(currentTime).toISOString()">{{ new Date(currentTime).toLocaleString() }}</time>
      <span :class="['dot', store.refreshing && 'pulse']"></span>
      {{ store.refreshing ? '正在刷新' : `上次完成 ${store.lastRefreshAt ? new Date(store.lastRefreshAt).toLocaleTimeString() : '—'}` }}
      <button :disabled="store.refreshing" @click="refreshVisible(true)">刷新</button>
      <router-link to="/settings">设置</router-link>
    </div>
  </header>
  <div v-if="store.fatalError" class="fatal" role="alert">{{ store.fatalError }}</div>
  <div class="layout">
    <aside>
      <router-link to="/" class="all-hosts">全部主机</router-link>
      <label class="host-search"><span>搜索跳板机</span><input v-model="hostSearch" type="search" placeholder="名称"></label>
      <nav aria-label="跳板机">
        <router-link v-for="host in store.hosts.filter(item => item.config.name.toLowerCase().includes(hostSearch.toLowerCase()))" :key="host.config.id" :to="`/hosts/${host.config.id}`">
          <span :class="['dot', host.error ? 'bad' : host.warning ? 'pulse' : 'good']"></span>{{ host.config.name }}
        </router-link>
      </nav>
    </aside>
    <main><router-view /></main>
  </div>
</template>
