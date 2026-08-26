<script setup lang="ts">
import {computed,onBeforeUnmount,ref,watch} from 'vue'
import {useRoute} from 'vue-router'
import {api,fmtBytes,fmtPercent,fmtTime} from '../api'
import {useAppStore} from '../store'
import type {ProcessHistoryPage} from '../types'

const route=useRoute();const store=useAppStore();const data=ref<ProcessHistoryPage>();const error=ref('');const loading=ref(false);let controller:AbortController|undefined
const backend=computed(()=>store.backendById(String(route.params.backendId)))
async function load(more=false){
  if(!backend.value)return;controller?.abort();controller=new AbortController();loading.value=true
  try{
    const cursor=more&&data.value?.next_cursor?`&cursor=${encodeURIComponent(data.value.next_cursor)}`:''
    const response=await api<ProcessHistoryPage>(backend.value,`/lifecycles/${route.params.bootId}/processes/${route.params.pid}?create_time_ms=${route.query.create_time_ms}&limit=200${cursor}`,store.config?.requestTimeoutMs,controller.signal)
    data.value=more&&data.value?{...response,items:[...data.value.items,...response.items]}:response;error.value=''
  }catch(reason){if((reason as Error).name!=='AbortError')error.value=(reason as Error).message}finally{loading.value=false}
}
watch([backend,()=>route.params.pid,()=>route.query.create_time_ms],()=>load(false),{immediate:true});onBeforeUnmount(()=>controller?.abort())
</script>

<template><section class="page"><p class="breadcrumb"><router-link :to="`/hosts/${route.params.backendId}/lifecycles/${route.params.bootId}`">返回生命周期</router-link> / 进程历史</p><h1>进程 {{ route.params.pid }}</h1><p class="muted">身份：<code>{{ route.params.pid }}:{{ route.query.create_time_ms }}</code></p><p v-if="error" class="warning" role="alert">{{ error }}</p><div class="table-scroll"><table><thead><tr><th>采样时间</th><th>用户</th><th>进程</th><th>CPU</th><th>RSS</th><th>Swap</th><th>读/写</th><th>状态</th><th>命令</th></tr></thead><tbody><tr v-for="item in data?.items" :key="item.snapshot_id"><td>{{ fmtTime(item.captured_at_ms) }}</td><td>{{ item.username??item.uid }}</td><td>{{ item.comm }}</td><td>{{ fmtPercent(item.cpu_percent) }}</td><td>{{ fmtBytes(item.rss_bytes) }}</td><td>{{ fmtBytes(item.swap_bytes) }}</td><td>{{ fmtBytes(item.read_bps) }}/s · {{ fmtBytes(item.write_bps) }}/s</td><td>{{ item.state??'—' }}</td><td><code>{{ item.cmdline_redacted }}</code></td></tr></tbody></table></div><button v-if="data?.next_cursor" :disabled="loading" @click="load(true)">{{ loading?'加载中…':'加载更多历史' }}</button></section></template>
