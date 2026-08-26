<script setup lang="ts">
import {fmtTime} from '../api'
import type {SnapshotSummary} from '../types'

defineProps<{items:SnapshotSummary[];selected?:number;nextCursor?:string|null;loading:boolean}>()
const emit=defineEmits<{select:[id:number];more:[]}>()
const selectable=(item:SnapshotSummary)=>!['downsampled','summary_only'].includes(item.detail_level)
</script>

<template>
  <div class="table-scroll">
    <table>
      <thead><tr><th>ID</th><th>采样时间</th><th>耗时</th><th>漂移</th><th>详情</th><th>持久化状态</th><th></th></tr></thead>
      <tbody><tr v-for="snapshot in items" :key="snapshot.id" :class="{selected:selected===snapshot.id}">
        <td><code>{{ snapshot.id }}</code></td><td>{{ fmtTime(snapshot.captured_at_ms) }}</td><td>{{ snapshot.duration_ms }} ms</td><td>{{ snapshot.captured_at_ms-snapshot.scheduled_at_ms }} ms</td><td>{{ snapshot.detail_level }}</td><td>{{ snapshot.persistence_state }}</td>
        <td><button :disabled="!selectable(snapshot)||loading" @click="emit('select',snapshot.id)">查看明细</button></td>
      </tr></tbody>
    </table>
  </div>
  <button v-if="nextCursor" :disabled="loading" @click="emit('more')">{{ loading?'加载中…':'加载更多快照' }}</button>
</template>
