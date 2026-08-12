<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ series: any[]; events?: any[] }>()
const palette = ['#315cd6', '#13795b', '#e59324', '#7c3aed', '#0891b2']
const bounds = computed(() => {
  const times = props.series.flatMap(item => item.points.map((point: any) => point[0]))
  return { min: Math.min(...times), max: Math.max(...times) }
})
const lines = computed(() => props.series.map((item, index) => {
  const values = item.points.map((point: any) => point[1]).filter((value: any) => typeof value === 'number')
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 0
  const span = max - min || 1
  const segments: string[] = []
  let current: string[] = []
  item.points.forEach((point: any) => {
    if (point[1] == null) {
      if (current.length) segments.push(current.join(' '))
      current = []
    } else {
      const x = bounds.value.max === bounds.value.min ? 50 : (point[0] - bounds.value.min) / (bounds.value.max - bounds.value.min) * 100
      current.push(`${x},${92 - (point[1] - min) / span * 80}`)
    }
  })
  if (current.length) segments.push(current.join(' '))
  return { ...item, segments, min, max, color: palette[index % palette.length] }
}))
const markers = computed(() => (props.events || []).map(event => ({
  ...event,
  x: bounds.value.max === bounds.value.min ? 50 : (event.occurred_at_ms - bounds.value.min) / (bounds.value.max - bounds.value.min) * 100,
})).filter(event => event.x >= 0 && event.x <= 100))
</script>

<template>
  <div class="chart card">
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="系统指标趋势，数据缺口处断线，竖线为事件">
      <template v-for="item in lines" :key="item.metric"><polyline v-for="(segment, index) in item.segments" :key="index" :points="segment" fill="none" :stroke="item.color" stroke-width="1.5" vector-effect="non-scaling-stroke" /></template>
      <line v-for="event in markers" :key="event.id" :x1="event.x" :x2="event.x" y1="4" y2="96" stroke="#c73e4d" stroke-width="1" stroke-dasharray="2 2" vector-effect="non-scaling-stroke"><title>{{ event.type }}</title></line>
    </svg>
    <div class="legend"><span v-for="item in lines" :key="item.metric"><i :style="{ background: item.color }"></i>{{ item.metric }}（{{ item.unit }}，{{ item.min }}–{{ item.max }}）</span><span v-if="markers.length"><i class="event-key"></i>事件（{{ markers.length }}）</span></div>
    <p v-if="!lines.length" class="muted">暂无趋势数据</p>
    <details v-if="lines.length"><summary>查看趋势文本摘要</summary><ul><li v-for="item in lines" :key="item.metric">{{ item.metric }}：{{ item.points.length }} 点，范围 {{ item.min }}–{{ item.max }} {{ item.unit }}</li></ul></details>
  </div>
</template>

<style scoped>.chart{min-height:280px}.chart svg{width:100%;height:215px;border-bottom:1px solid #dce3ef}.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px}.legend i{display:inline-block;width:10px;height:3px;margin-right:5px;vertical-align:middle}.legend .event-key{height:8px;width:2px;background:#c73e4d}</style>
