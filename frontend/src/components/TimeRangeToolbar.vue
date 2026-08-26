<script setup lang="ts">
import {ref,watch} from 'vue'
import {fromLocalDateTime,toLocalDateTime} from '../analysis'

const props=defineProps<{from:number;to:number;startedAt:number;endedAt:number;snapshotCount:number;loading:boolean}>()
const emit=defineEmits<{
  apply:[from:number,to:number];preset:[value:'10m'|'1h'|'all'];shift:[direction:-1|1];end:[];locateSnapshot:[id:number];locateTime:[timestamp:number]
}>()
const fromInput=ref(toLocalDateTime(props.from))
const toInput=ref(toLocalDateTime(props.to))
const snapshotInput=ref('')
const timeInput=ref('')
const inputError=ref('')

watch(()=>[props.from,props.to],()=>{fromInput.value=toLocalDateTime(props.from);toInput.value=toLocalDateTime(props.to)})
function apply(){
  const from=fromLocalDateTime(fromInput.value);const to=fromLocalDateTime(toInput.value)
  if(from==null||to==null){inputError.value='请输入有效的开始和结束时间。';return}
  inputError.value='';emit('apply',from,to)
}
function locateSnapshot(){
  const id=Number(snapshotInput.value)
  if(!Number.isSafeInteger(id)||id<=0){inputError.value='快照 ID 必须是正整数。';return}
  inputError.value='';emit('locateSnapshot',id)
}
function locateTime(){
  const value=fromLocalDateTime(timeInput.value)
  if(value==null){inputError.value='请输入有效的目标时间。';return}
  inputError.value='';emit('locateTime',value)
}
</script>

<template>
  <section class="analysis-toolbar" aria-label="时间和快照定位">
    <div class="time-inputs">
      <label>开始时间<input v-model="fromInput" type="datetime-local" step="1" :min="toLocalDateTime(startedAt)" :max="toLocalDateTime(endedAt)"></label>
      <label>结束时间<input v-model="toInput" type="datetime-local" step="1" :min="toLocalDateTime(startedAt)" :max="toLocalDateTime(endedAt)"></label>
      <button :disabled="loading" @click="apply">应用时间范围</button>
    </div>
    <div class="toolbar-row compact-actions">
      <button :disabled="loading" @click="emit('preset','10m')">最后10分钟</button>
      <button :disabled="loading" @click="emit('preset','1h')">最后1小时</button>
      <button :disabled="loading" @click="emit('preset','all')">完整生命周期</button>
      <button aria-label="向前移动一个时间窗口" :disabled="loading||from<=startedAt" @click="emit('shift',-1)">← 前一窗口</button>
      <button aria-label="向后移动一个时间窗口" :disabled="loading||to>=endedAt" @click="emit('shift',1)">后一窗口 →</button>
      <button :disabled="loading" @click="emit('end')">回到结束时刻</button>
    </div>
    <div class="locator-grid">
      <label>快照 ID<input v-model="snapshotInput" inputmode="numeric" placeholder="例如 111725" @keyup.enter="locateSnapshot"></label>
      <button :disabled="loading" @click="locateSnapshot">定位快照</button>
      <label>目标时间<input v-model="timeInput" type="datetime-local" step="1"></label>
      <button :disabled="loading" @click="locateTime">查找此前快照</button>
    </div>
    <p class="window-summary">{{ toLocalDateTime(from).replace('T',' ') }} 至 {{ toLocalDateTime(to).replace('T',' ') }} · {{ Math.round((to-from)/60000) }} 分钟 · 已加载 {{ snapshotCount }} 条快照</p>
    <p v-if="inputError" class="warning" role="alert">{{ inputError }}</p>
  </section>
</template>
