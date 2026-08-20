<script setup lang="ts">
import {computed,ref} from 'vue'
import {fmtBytes,fmtPercent} from '../api'
import {sortUsers} from '../analysis'
import type {UserSample,UserSortKey} from '../types'

const props=defineProps<{items:UserSample[];selectedUid?:number}>()
const emit=defineEmits<{select:[uid:number|undefined]}>()
const sortKey=ref<UserSortKey>('cpu_percent')
const sorted=computed(()=>sortUsers(props.items,sortKey.value))
const columns:Array<{key:UserSortKey;label:string}>=[
  {key:'cpu_percent',label:'CPU'},{key:'rss_bytes',label:'RSS'},{key:'swap_bytes',label:'Swap'},{key:'read_bps',label:'读取'},{key:'write_bps',label:'写入'},{key:'process_count',label:'进程'},{key:'thread_count',label:'线程'},
]
</script>

<template>
  <div class="section-heading"><h2>所选快照用户汇总</h2><button v-if="selectedUid!=null" @click="emit('select',undefined)">清除用户筛选</button></div>
  <div class="table-scroll"><table>
    <thead><tr><th>用户</th><th v-for="column in columns" :key="column.key"><button class="sort-button" :aria-pressed="sortKey===column.key" @click="sortKey=column.key">{{ column.label }}{{ sortKey===column.key?' ↓':'' }}</button></th></tr></thead>
    <tbody><tr v-for="user in sorted" :key="user.uid" :class="{selected:selectedUid===user.uid}" tabindex="0" @click="emit('select',user.uid)" @keyup.enter="emit('select',user.uid)">
      <td><b>{{ user.username??`UID ${user.uid}` }}</b><small v-if="user.username==null" class="muted">未解析用户名</small></td>
      <td>{{ fmtPercent(user.cpu_percent) }}</td><td>{{ fmtBytes(user.rss_bytes) }}</td><td>{{ fmtBytes(user.swap_bytes) }}</td><td>{{ fmtBytes(user.read_bps) }}/s</td><td>{{ fmtBytes(user.write_bps) }}/s</td><td>{{ user.process_count }}</td><td>{{ user.thread_count }}</td>
    </tr></tbody>
  </table></div>
</template>
