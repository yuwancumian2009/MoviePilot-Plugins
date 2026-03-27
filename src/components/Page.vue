<script setup lang="ts">
import { ref, onMounted } from 'vue'

const props = defineProps({
  api: { type: Object, default: () => ({}) }
})

const emit = defineEmits(['action', 'switch', 'close'])

const currentRules = ref<Record<string, string>>({})
const logs = ref<string[]>([])

onMounted(async () => {
  try {
    const res = await props.api.get('plugin/MessageRouterVue/logs')
    if (res && res.success) {
      currentRules.value = res.data.rules || {}
      logs.value = res.data.logs || []
    }
  } catch (error) {
    console.error('获取日志失败', error)
  }
})
</script>

<template>
  <div class="pa-4">
    <div class="d-flex justify-space-between align-center mb-4">
      <div class="text-h6">运行状态监控</div>
      <v-btn color="primary" variant="outlined" @click="emit('switch')">前往修改配置</v-btn>
    </div>
    
    <v-card class="mb-4" variant="outlined">
      <v-card-title class="text-subtitle-1 font-weight-bold text-success">【当前生效规则】</v-card-title>
      <v-card-text>
        <div v-if="Object.keys(currentRules).length === 0">暂无生效的路由规则</div>
        <div v-else v-for="(target, plugin) in currentRules" :key="plugin" class="mb-1">
          • 拦截插件：[{{ plugin }}] ➔ 路由目标：[{{ target }}]
        </div>
      </v-card-text>
    </v-card>

    <v-card variant="outlined">
      <v-card-title class="text-subtitle-1 font-weight-bold">【实时监控日志 (最近50条)】</v-card-title>
      <v-card-text class="bg-grey-lighten-4 pa-3 rounded" style="font-family: monospace; max-height: 400px; overflow-y: auto;">
        <div v-if="logs.length === 0">暂无日志，请尝试触发其他插件的消息通知。</div>
        <div v-else v-for="(log, idx) in logs" :key="idx" class="text-body-2 mb-1">{{ log }}</div>
      </v-card-text>
    </v-card>
  </div>
</template>
