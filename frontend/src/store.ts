import { defineStore } from 'pinia'
import { api, ApiError, loadRuntimeConfig } from './api'
import type { HostState, Latest, RuntimeConfig, Status } from './types'

export const useAppStore = defineStore('app', {
  state: () => ({
    config: null as RuntimeConfig | null,
    hosts: [] as HostState[],
    fatalError: '',
    refreshing: false,
    lastRefreshAt: undefined as number | undefined,
  }),
  getters: {
    backendById: state => (id: string) => state.config?.backends.find(item => item.id === id),
    hostById: state => (id: string) => state.hosts.find(item => item.config.id === id),
  },
  actions: {
    async initialize() {
      try {
        this.config = await loadRuntimeConfig()
        this.hosts = this.config.backends.map(config => ({ config, loading: true }))
        await this.refresh(true)
      } catch (error) {
        this.fatalError = (error as Error).message
      }
    },
    async refreshHost(host: HostState) {
      if (!this.config) return
      host.loading = !host.status && !host.latest
      const started = Date.now()
      const results = await Promise.allSettled([
        api<Status>(host.config, '/status', this.config.requestTimeoutMs),
        api<Latest>(host.config, '/latest', this.config.requestTimeoutMs),
      ])
      const finished = Date.now()
      const status = results[0].status === 'fulfilled' ? results[0].value : undefined
      const latest = results[1].status === 'fulfilled' ? results[1].value : undefined
      const returnedIds = new Set([status?.host_id, latest?.host_id].filter(Boolean))
      const knownHostId = host.status?.host_id || host.latest?.host_id
      if (returnedIds.size > 1 || knownHostId && [...returnedIds].some(value => value !== knownHostId)) {
        host.error = '同一后端返回了不一致的 host_id，已拒绝更新缓存'
        host.failureKind = 'incompatible'
        host.loading = false
        return
      }
      if (status) {
        host.status = status
        host.clockOffsetMs = status.server_time_ms - Math.round((started + finished) / 2)
      }
      if (latest) host.latest = latest
      if (status || latest) host.lastSuccessAt = finished
      const failures = results.filter(item => item.status === 'rejected') as PromiseRejectedResult[]
      const message = failures.map(item => item.reason?.message || '请求失败').join('；')
      host.error = failures.length === 2 ? message : undefined
      host.warning = failures.length === 1 ? `部分数据不可用：${message}` : undefined
      const apiError = failures.map(item => item.reason).find(item => item instanceof ApiError) as ApiError | undefined
      host.failureKind = apiError?.kind
      host.loading = false
    },
    async refresh(force = false) {
      if (!this.config || this.refreshing) return
      this.refreshing = true
      try {
        let index = 0
        const pendingHosts = force ? this.hosts : this.hosts.filter(host => host.failureKind !== 'forbidden')
        const workers = Array.from({ length: Math.min(6, pendingHosts.length) }, async () => {
          while (index < pendingHosts.length) {
            const host = pendingHosts[index++]
            await this.refreshHost(host)
          }
        })
        await Promise.allSettled(workers)
        this.lastRefreshAt = Date.now()
      } finally {
        this.refreshing = false
      }
    },
    async refreshBackend(id: string, force = false) {
      if (!this.config || this.refreshing) return
      const host = this.hostById(id)
      if (!host || !force && host.failureKind === 'forbidden') return
      this.refreshing = true
      try {
        await this.refreshHost(host)
        this.lastRefreshAt = Date.now()
      } finally {
        this.refreshing = false
      }
    },
  },
})
