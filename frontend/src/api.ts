import type { BackendConfig, RuntimeConfig } from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly kind: 'timeout' | 'network' | 'forbidden' | 'incompatible' | 'backend',
    public readonly status?: number,
    public readonly code?: string,
  ) {
    super(message)
  }
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch('/config/runtime-config.json', { cache: 'no-store', credentials: 'omit' })
  if (!response.ok) throw new Error(`运行时配置加载失败 (${response.status})`)
  const data = await response.json()
  if (!Array.isArray(data.backends) || data.backends.length === 0) throw new Error('运行时配置必须包含至少一个后端')
  const ids = new Set<string>()
  for (const backend of data.backends) {
    if (!backend.id || !backend.name || !/^https:\/\//.test(backend.baseUrl) || ids.has(backend.id)) {
      throw new Error(`无效后端配置: ${backend.id || 'unknown'}`)
    }
    backend.baseUrl = backend.baseUrl.replace(/\/$/, '')
    ids.add(backend.id)
  }
  const requestTimeoutMs = Number(data.requestTimeoutMs ?? 8000)
  const refreshIntervalMs = Number(data.refreshIntervalMs ?? 5000)
  if (!Number.isFinite(requestTimeoutMs) || requestTimeoutMs < 1000) throw new Error('requestTimeoutMs 必须不小于 1000')
  if (!Number.isFinite(refreshIntervalMs) || refreshIntervalMs < 1000) throw new Error('refreshIntervalMs 必须不小于 1000')
  return { backends: data.backends, requestTimeoutMs, refreshIntervalMs }
}

function classifyStatus(status: number): ApiError['kind'] {
  if (status === 401 || status === 403) return 'forbidden'
  return 'backend'
}

export async function api<T>(backend: BackendConfig, path: string, timeout = 8000, externalSignal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  let timedOut = false
  const timer = window.setTimeout(() => { timedOut = true; controller.abort() }, timeout)
  const cancel = () => controller.abort()
  externalSignal?.addEventListener('abort', cancel, { once: true })
  try {
    const response = await fetch(`${backend.baseUrl}${path}`, {
      credentials: 'omit',
      headers: { Accept: 'application/json', 'X-Request-ID': crypto.randomUUID() },
      signal: controller.signal,
    })
    if (!response.ok) {
      let code: string | undefined
      let message = `Ingress/后端返回 ${response.status}`
      try {
        const error = await response.json()
        code = error.code
        message = error.message || code || message
      } catch { /* Ingress may return HTML. */ }
      const kind = classifyStatus(response.status)
      throw new ApiError(message, kind, response.status, code)
    }
    const value = await response.json()
    if (value.api_version && value.api_version !== '1') {
      throw new ApiError(`API 版本不兼容: ${value.api_version}`, 'incompatible', undefined, 'VERSION_UNSUPPORTED')
    }
    if (typeof value.schema_version === 'number' && (value.schema_version < 1 || value.schema_version > 2)) {
      throw new ApiError(`数据结构版本不兼容: ${value.schema_version}（前端支持 1–2）`, 'incompatible', undefined, 'VERSION_UNSUPPORTED')
    }
    return value as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if ((error as Error).name === 'AbortError') {
      if (!timedOut && externalSignal?.aborted) throw error
      throw new ApiError(`请求超时 (${timeout} ms)`, 'timeout')
    }
    throw new ApiError((error as Error).message || '网络请求失败', 'network')
  } finally {
    clearTimeout(timer)
    externalSignal?.removeEventListener('abort', cancel)
  }
}

export function fmtBytes(value: number | null | undefined): string {
  if (value == null) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let scaled = Math.abs(value)
  let unit = 0
  while (scaled >= 1024 && unit < units.length - 1) { scaled /= 1024; unit += 1 }
  return `${value < 0 ? '-' : ''}${scaled.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} ${units[unit]}`
}

export const fmtTime = (value: number | null | undefined) => value == null ? '无法确定' : new Date(value).toLocaleString()
export const fmtPercent = (value: number | null | undefined) => value == null ? '—' : `${value.toFixed(1)}%`
export const evidenceGap = (lastPersistedAt: number | null | undefined, trustedEndedAt: number | null | undefined) => lastPersistedAt == null || trustedEndedAt == null ? null : Math.max(0, trustedEndedAt - lastPersistedAt)
export const processIdentity = (pid: number, createTimeMs: number) => `${pid}:${createTimeMs}`
export const terminationLabel = (value: string) => value === 'active'
  ? '运行中'
  : value === 'clean_shutdown_observed'
    ? '观察到正常关机证据'
    : value === 'unclean_or_unknown'
      ? '未观察到正常收尾或原因未知'
      : `未知状态（${value}）`
