<script setup lang="ts">
import { ref, onMounted } from 'vue'

const props = defineProps({
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) }
})

const emit = defineEmits(['save', 'close', 'switch'])

const config = ref({
  enabled: props.initialConfig?.enabled || false,
  rules: Array.isArray(props.initialConfig?.rules) ? props.initialConfig.rules : []
})

const pluginOptions = ref([])
const typeOptions = ["资源下载", "整理入库", "订阅", "站点", "媒体服务器", "手动处理", "插件", "其它"]

onMounted(async () => {
  try {
    const res = await props.api.get('plugin/MessageRouterVue/plugins')
    if (res && res.success) {
      pluginOptions.value = res.data
    }
  } catch (error) {
    console.error('获取插件列表失败', error)
  }
  
  if (config.value.rules.length === 0) {
    addRule()
  }
})

const addRule = () => { config.value.rules.push({ plugin: '', target: '' }) }
const removeRule = (index: number) => { config.value.rules.splice(index, 1) }

const saveConfig = () => {
  config.value.rules = config.value.rules.filter((r: any) => r.plugin && r.target)
  emit('save', config.value) 
}

const notifyClose = () => { emit('close') }
</script>

<template>
  <div class="plugin-config pa-4">
    <v-switch v-model="config.enabled" color="primary" label="启用插件消息路由"></v-switch>
    <v-divider class="my-4"></v-divider>
    
    <div class="text-h6 mb-3">配置路由规则</div>
    
    <v-row v-for="(rule, index) in config.rules" :key="index" align="center" dense>
      <v-col cols="12" md="5">
        <v-select v-model="rule.plugin" :items="pluginOptions" label="监听来源插件" placeholder="请选择" variant="outlined" density="comfortable" hide-details></v-select>
      </v-col>
      <v-col cols="12" md="1" class="text-center">
        <v-icon size="large" color="grey">mdi-arrow-right-thick</v-icon>
      </v-col>
      <v-col cols="12" md="5">
        <v-select v-model="rule.target" :items="typeOptions" label="目标消息类型" placeholder="请选择" variant="outlined" density="comfortable" hide-details></v-select>
      </v-col>
      <v-col cols="12" md="1" class="text-center">
        <v-btn icon color="error" variant="text" @click="removeRule(index)"><v-icon>mdi-delete</v-icon></v-btn>
      </v-col>
    </v-row>

    <v-btn color="info" variant="tonal" class="mt-4 mb-6" prepend-icon="mdi-plus" @click="addRule">添加一条新规则</v-btn>
    <v-divider class="mb-4"></v-divider>

    <div class="d-flex justify-end gap-2">
      <v-btn variant="outlined" @click="notifyClose">取消</v-btn>
      <v-btn color="primary" @click="saveConfig">保存配置</v-btn>
    </div>
  </div>
</template>

<style scoped>
.gap-2 { gap: 8px; }
</style>
