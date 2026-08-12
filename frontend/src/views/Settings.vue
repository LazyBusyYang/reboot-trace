<script setup lang="ts">
import { useAppStore } from '../store';

const store = useAppStore();
</script>

<template><section class="page"><h1>前端设置</h1><p class="muted">运行时配置来自 /config/runtime-config.json，页面不会保存凭证。</p><div class="grid"><div class="card metric"><span>浏览器时区</span><b>{{Intl.DateTimeFormat().resolvedOptions().timeZone}}</b></div><div class="card metric"><span>刷新间隔</span><b>{{store.config?.refreshIntervalMs}} ms</b></div><div class="card metric"><span>请求超时</span><b>{{store.config?.requestTimeoutMs}} ms</b></div></div><h2>已加载后端</h2><table><thead><tr><th>ID</th><th>名称</th><th>HTTPS Ingress</th></tr></thead><tbody><tr v-for="b in store.config?.backends" :key="b.id"><td>{{b.id}}</td><td>{{b.name}}</td><td>{{b.baseUrl}}</td></tr></tbody></table></section></template>
