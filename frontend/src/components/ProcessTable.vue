<script setup lang="ts">
import {computed,ref} from 'vue'
import {filterProcesses,PROCESS_STATES} from '../analysis'
import {fmtBytes,fmtPercent,processIdentity} from '../api'
import type {IdentityHint} from '../analysis'
import type {RankedProcess,ResourceDimension} from '../types'

const props=defineProps<{items:RankedProcess[];dimension:ResourceDimension;uid?:number;keyword:string;state:string;hints:IdentityHint[];nextCursor?:string|null;loading:boolean;historyBase:string}>()
const emit=defineEmits<{dimension:[value:ResourceDimension];keyword:[value:string];state:[value:string];more:[]}>()
const activeOnly=ref(false)
const expanded=ref(new Set<string>())
const copied=ref('')
const visible=computed(()=>filterProcesses(props.items,props.keyword,props.state,activeOnly.value))
function toggle(item:RankedProcess){const key=processIdentity(item.pid,item.create_time_ms);const next=new Set(expanded.value);next.has(key)?next.delete(key):next.add(key);expanded.value=next}
async function copy(value:string,label:string){await navigator.clipboard.writeText(value);copied.value=label;window.setTimeout(()=>{if(copied.value===label)copied.value=''},1500)}
</script>

<template>
  <div class="section-heading"><h2>进程排行</h2><span class="muted">当前展示已加载排行中的 {{ visible.length }} 条</span></div>
  <div v-if="uid!=null&&hints.length" class="identity-hints">
    <b>可能的用户目录（推测）</b>
    <p>这些线索仅来自当前已加载进程的脱敏命令，不能替代账号或登录审计。</p>
    <ul><li v-for="hint in hints" :key="hint.name"><code>{{ hint.name }}</code> · {{ hint.count }} 个进程 · <code>{{ hint.paths.join('、') }}</code></li></ul>
  </div>
  <div class="tabs" role="tablist"><button v-for="item in ['cpu','rss','swap','read','write']" :key="item" :class="{active:dimension===item}" role="tab" :aria-selected="dimension===item" @click="emit('dimension',item as ResourceDimension)">{{ item.toUpperCase() }}</button></div>
  <div class="process-filters">
    <label>搜索进程<input :value="keyword" placeholder="PID、PPID、进程名或命令" @input="emit('keyword',($event.target as HTMLInputElement).value)"></label>
    <label>状态<select :value="state" @change="emit('state',($event.target as HTMLSelectElement).value)"><option value="">全部</option><option v-for="item in PROCESS_STATES" :key="item" :value="item">{{ item }}</option></select></label>
    <label class="check-label"><input v-model="activeOnly" type="checkbox">仅显示有资源活动</label>
  </div>
  <div class="table-scroll"><table>
    <thead><tr><th>#</th><th>用户</th><th>进程身份</th><th>进程</th><th>CPU</th><th>RSS</th><th>Swap</th><th>读/写</th><th>线程</th><th>状态</th><th>命令</th></tr></thead>
    <tbody><tr v-for="process in visible" :key="processIdentity(process.pid,process.create_time_ms)">
      <td>{{ process.rank }}</td><td>{{ process.username??process.uid }}</td>
      <td><router-link :to="`${historyBase}/processes/${process.pid}?create_time_ms=${process.create_time_ms}`"><code>{{ process.pid }}:{{ process.create_time_ms }}</code></router-link><small>PPID {{ process.ppid??'—' }}</small></td>
      <td>{{ process.comm }}</td><td>{{ fmtPercent(process.cpu_percent) }}</td><td>{{ fmtBytes(process.rss_bytes) }}</td><td>{{ fmtBytes(process.swap_bytes) }}</td><td>{{ fmtBytes(process.read_bps) }}/s · {{ fmtBytes(process.write_bps) }}/s</td><td>{{ process.threads??'—' }}</td><td>{{ process.state??'—' }}</td>
      <td class="command-cell"><button class="link-button" @click="toggle(process)">{{ expanded.has(processIdentity(process.pid,process.create_time_ms))?'收起':'展开' }}</button><code :title="process.cmdline_redacted">{{ expanded.has(processIdentity(process.pid,process.create_time_ms))?process.cmdline_redacted:process.cmdline_redacted.slice(0,80) }}{{ !expanded.has(processIdentity(process.pid,process.create_time_ms))&&process.cmdline_redacted.length>80?'…':'' }}</code><div class="command-actions"><button @click="copy(process.cmdline_redacted,`cmd-${process.pid}`)">{{ copied===`cmd-${process.pid}`?'已复制':'复制命令' }}</button><button @click="copy(process.cmdline_hash,`hash-${process.pid}`)">{{ copied===`hash-${process.pid}`?'已复制':'复制哈希' }}</button></div></td>
    </tr></tbody>
  </table></div>
  <p v-if="!visible.length" class="muted">当前筛选条件下没有进程。</p>
  <button v-if="nextCursor" :disabled="loading" @click="emit('more')">{{ loading?'加载中…':'加载更多进程' }}</button>
</template>
