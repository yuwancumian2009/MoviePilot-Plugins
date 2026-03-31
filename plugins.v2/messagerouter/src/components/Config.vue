<script setup lang="ts">
import { computed, reactive, ref, watch, onMounted } from 'vue'

const props = defineProps({
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['save', 'close', 'switch'])

const config = reactive({
  enabled: false,
  block_system: false,
  plugin_mapping: '',
  route_rules: [] as any[],
  ...props.initialConfig,
})

const routeRules = ref<any[]>([])
const optionState = reactive({
  plugins: [] as any[],
  notification_types: [] as any[],
  wechat_apps: [] as any[],
})
const ruleForm = reactive({
  plugin: null as any,
  type: null as string | null,
  app: null as string | null,
})

watch(
  () => props.initialConfig,
  (val) => {
    Object.assign(config, val || {})
    routeRules.value = normalizeRules((val || {}).route_rules, (val || {}).plugin_mapping)
  },
  { deep: true },
)

const saving = ref(false)
const loadingConfig = ref(false)
const loadingOptions = ref(false)
const snackbar = reactive({ show: false, text: '', color: 'success' })

const canAddRule = computed(() => !!ruleForm.plugin)

function normalizeRules(rules: any, mappingText = '') {
  if (Array.isArray(rules) && rules.length) {
    return rules
      .map((item) => ({
        plugin: String(item?.plugin || '').trim(),
        type: String(item?.type || '').trim(),
        app: String(item?.app || '').trim(),
      }))
      .filter((item) => item.plugin)
  }

  return String(mappingText || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split(':')
      return {
        plugin: String(parts[0] || '').trim(),
        type: String(parts[1] || '').trim(),
        app: String(parts[2] || '').trim(),
      }
    })
    .filter((item) => item.plugin)
}

function syncConfigRules() {
  config.route_rules = routeRules.value.map((item) => ({ ...item }))
  config.plugin_mapping = routeRules.value
    .map((item) => `${item.plugin}:${item.type || ''}:${item.app || ''}`)
    .join('\n')
}

function addRule() {
  if (!ruleForm.plugin) {
    return
  }

  const pluginVal = typeof ruleForm.plugin === 'object' && ruleForm.plugin !== null
    ? (ruleForm.plugin.value || ruleForm.plugin.title || '')
    : String(ruleForm.plugin || '')

  const duplicateIndex = routeRules.value.findIndex(
    (item) => item.plugin === pluginVal,
  )

  const newRule = {
    plugin: pluginVal,
    type: ruleForm.type || '',
    app: ruleForm.app || '',
  }

  if (duplicateIndex >= 0) {
    routeRules.value.splice(duplicateIndex, 1, newRule)
  } else {
    routeRules.value.push(newRule)
  }

  syncConfigRules()
  ruleForm.plugin = null
  ruleForm.type = null
  ruleForm.app = null

  handleSave(false)
}

function removeRule(index: number) {
  routeRules.value.splice(index, 1)
  syncConfigRules()
  handleSave(false)
}

async function loadConfig() {
  loadingConfig.value = true
  try {
    const data = await props.api.get('plugin/MessageRouter/config')
    Object.assign(config, {
      enabled: false,
      block_system: false,
      plugin_mapping: '',
      route_rules: [],
      ...(data || {}),
    })
    routeRules.value = normalizeRules((data || {}).route_rules, (data || {}).plugin_mapping)
    syncConfigRules()
  } catch (e) {
    routeRules.value = normalizeRules(config.route_rules, config.plugin_mapping)
    syncConfigRules()
  } finally {
    loadingConfig.value = false
  }
}

async function loadOptions() {
  loadingOptions.value = true
  try {
    const data = await props.api.get('plugin/MessageRouter/options')
    optionState.plugins = data?.plugins || []
    optionState.notification_types = data?.notification_types || []
    optionState.wechat_apps = data?.wechat_apps || []
  } catch (e) {
    snackbar.text = '选项加载失败'
    snackbar.color = 'warning'
    snackbar.show = true
  } finally {
    loadingOptions.value = false
  }
}

onMounted(async () => {
  await loadConfig()
  await loadOptions()
})

async function handleSave(isManual = false) {
  saving.value = true
  try {
    syncConfigRules()
    if (isManual === true) {
      emit('save', { ...config, route_rules: routeRules.value.map((item) => ({ ...item })) })
    }
    const result = await props.api.post('plugin/MessageRouter/config', {
      ...config,
      route_rules: routeRules.value.map((item) => ({ ...item })),
    }).catch(() => null)

    if (result?.config) {
      Object.assign(config, result.config)
      routeRules.value = normalizeRules(result.config.route_rules, result.config.plugin_mapping)
      syncConfigRules()
    }

    snackbar.text = '配置已保存'
    snackbar.color = 'success'
    snackbar.show = true
  } catch (e) {
    snackbar.text = '保存失败'
    snackbar.color = 'error'
    snackbar.show = true
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="mr-config">
    <!-- 顶部标题栏 -->
    <div class="mr-topbar">
      <div class="mr-topbar__left">
        <div class="mr-topbar__icon">
          <v-icon icon="mdi-tune-variant" size="24" />
        </div>
        <div>
          <div class="mr-topbar__title">插件 · 配置</div>
          <div class="mr-topbar__sub">Message Router Plugin</div>
        </div>
      </div>
      <div class="mr-topbar__right">
        <v-btn-group variant="tonal" density="compact" class="elevation-0">
          <v-btn color="primary" @click="emit('switch', 'Page')" size="small" min-width="40" class="px-0 px-sm-3">
            <v-icon icon="mdi-view-dashboard" size="18" class="mr-sm-1"></v-icon>
            <span class="btn-text d-none d-sm-inline">状态页</span>
          </v-btn>
          <v-btn color="primary" @click="() => handleSave(true)" :loading="saving" size="small" min-width="40" class="px-0 px-sm-3">
            <v-icon icon="mdi-content-save" size="18" class="mr-sm-1"></v-icon>
            <span class="btn-text d-none d-sm-inline">保存</span>
          </v-btn>
          <v-btn color="primary" @click="emit('close')" size="small" min-width="40" class="px-0 px-sm-3">
            <v-icon icon="mdi-close" size="18"></v-icon>
            <span class="btn-text d-none d-sm-inline">关闭</span>
          </v-btn>
        </v-btn-group>
      </div>
    </div>

    <!-- 基础设置卡片 -->
    <div class="mr-card">
      <div class="mr-card__header">
        <span class="mr-card__title d-flex align-center">
          <v-icon icon="mdi-tune-vertical" size="18" color="#8b5cf6" class="mr-1"></v-icon>基础设置
        </span>
      </div>

      <v-row class="mt-1 mb-1">
        <v-col cols="12" sm="6" class="d-flex align-center justify-space-between py-1">
          <span class="mr-row__text">
            <v-icon icon="mdi-power-plug" size="20" :color="config.enabled ? '#a78bfa' : 'grey'" class="mr-2"></v-icon>
            启用高级路由与企微直推
          </span>
          <label class="switch" style="--switch-checked-bg: #a78bfa;">
            <input v-model="config.enabled" type="checkbox">
            <div class="slider">
              <div class="circle">
                <svg class="cross" xml:space="preserve" style="enable-background:new 0 0 512 512" viewBox="0 0 365.696 365.696" y="0" x="0" height="6" width="6" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" xmlns="http://www.w3.org/2000/svg"><g><path data-original="#000000" fill="currentColor" d="M243.188 182.86 356.32 69.726c12.5-12.5 12.5-32.766 0-45.247L341.238 9.398c-12.504-12.503-32.77-12.503-45.25 0L182.86 122.528 69.727 9.374c-12.5-12.5-32.766-12.5-45.247 0L9.375 24.457c-12.5 12.504-12.5 32.77 0 45.25l113.152 113.152L9.398 295.99c-12.503 12.503-12.503 32.769 0 45.25L24.48 356.32c12.5 12.5 32.766 12.5 45.247 0l113.132-113.132L295.99 356.32c12.503 12.5 32.769 12.5 45.25 0l15.081-15.082c12.5-12.504 12.5-32.77 0-45.25zm0 0"></path></g></svg>
                <svg class="checkmark" xml:space="preserve" style="enable-background:new 0 0 512 512" viewBox="0 0 24 24" y="0" x="0" height="10" width="10" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" xmlns="http://www.w3.org/2000/svg"><g><path data-original="#000000" fill="currentColor" d="M9.707 19.121a.997.997 0 0 1-1.414 0l-5.646-5.647a1.5 1.5 0 0 1 0-2.121l.707-.707a1.5 1.5 0 0 1 2.121 0L9 14.171l9.525-9.525a1.5 1.5 0 0 1 2.121 0l.707.707a1.5 1.5 0 0 1 0 2.121z"></path></g></svg>
              </div>
            </div>
          </label>
        </v-col>
        <v-col cols="12" sm="6" class="d-flex align-center justify-space-between py-1">
          <span class="mr-row__text">
            <v-icon icon="mdi-broadcast-off" size="20" :color="config.block_system ? 'info' : 'grey'" class="mr-2"></v-icon>
            直推后阻断系统默认广播
          </span>
          <label class="switch" style="--switch-checked-bg: rgb(var(--v-theme-info));">
            <input v-model="config.block_system" type="checkbox">
            <div class="slider">
              <div class="circle">
                <svg class="cross" xml:space="preserve" style="enable-background:new 0 0 512 512" viewBox="0 0 365.696 365.696" y="0" x="0" height="6" width="6" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" xmlns="http://www.w3.org/2000/svg"><g><path data-original="#000000" fill="currentColor" d="M243.188 182.86 356.32 69.726c12.5-12.5 12.5-32.766 0-45.247L341.238 9.398c-12.504-12.503-32.77-12.503-45.25 0L182.86 122.528 69.727 9.374c-12.5-12.5-32.766-12.5-45.247 0L9.375 24.457c-12.5 12.504-12.5 32.77 0 45.25l113.152 113.152L9.398 295.99c-12.503 12.503-12.503 32.769 0 45.25L24.48 356.32c12.5 12.5 32.766 12.5 45.247 0l113.132-113.132L295.99 356.32c12.503 12.5 32.769 12.5 45.25 0l15.081-15.082c12.5-12.504 12.5-32.77 0-45.25zm0 0"></path></g></svg>
                <svg class="checkmark" xml:space="preserve" style="enable-background:new 0 0 512 512" viewBox="0 0 24 24" y="0" x="0" height="10" width="10" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" xmlns="http://www.w3.org/2000/svg"><g><path data-original="#000000" fill="currentColor" d="M9.707 19.121a.997.997 0 0 1-1.414 0l-5.646-5.647a1.5 1.5 0 0 1 0-2.121l.707-.707a1.5 1.5 0 0 1 2.121 0L9 14.171l9.525-9.525a1.5 1.5 0 0 1 2.121 0l.707.707a1.5 1.5 0 0 1 0 2.121z"></path></g></svg>
              </div>
            </div>
          </label>
        </v-col>
      </v-row>

      <div class="mr-divider" />

      <div class="mr-field" style="margin-top: 12px; margin-bottom: 8px;">
        <div class="mr-field__header mb-1">
          <div class="mr-field__title-block">
            <div class="mr-field__title-main">
              <v-icon icon="mdi-source-branch" size="18" color="info" class="mr-field__title-icon"></v-icon>
              <div class="mr-field__title-text">
                <label class="mr-field__label">高级消息路由映射规则</label>
                <div class="mr-field__hint mr-field__hint--compact">选择或输入插件名/关键字、目标消息类型和企微应用后添加。</div>
              </div>
            </div>
          </div>
          <v-btn color="primary" prepend-icon="mdi-plus" rounded="lg" :disabled="!canAddRule" @click="addRule">添加规则</v-btn>
        </div>

        <v-row dense class="mt-1">
          <v-col cols="12" md="4">
            <v-combobox
              v-model="ruleForm.plugin"
              :items="optionState.plugins"
              item-title="title"
              item-value="value"
              label="插件名或标题关键字"
              density="compact"
              variant="outlined"
              hide-details="auto"
              class="mr-input"
              :loading="loadingOptions || loadingConfig"
              :menu-props="{ contentClass: 'mr-select-menu' }"
              :return-object="false"
            />
          </v-col>
          <v-col cols="12" md="4">
            <v-select
              v-model="ruleForm.type"
              :items="optionState.notification_types"
              item-title="title"
              item-value="value"
              label="目标消息类型"
              density="compact"
              variant="outlined"
              hide-details="auto"
              class="mr-input"
              :loading="loadingOptions || loadingConfig"
              :menu-props="{ contentClass: 'mr-select-menu' }"
            />
          </v-col>
          <v-col cols="12" md="4">
            <v-select
              v-model="ruleForm.app"
              :items="optionState.wechat_apps"
              item-title="title"
              item-value="value"
              label="系统微信通知名称"
              density="compact"
              variant="outlined"
              hide-details="auto"
              class="mr-input"
              :loading="loadingOptions || loadingConfig"
              :menu-props="{ contentClass: 'mr-select-menu' }"
            />
          </v-col>
        </v-row>

        <div v-if="!routeRules.length" class="mr-empty-state mt-2">
          <v-icon icon="mdi-information-outline" size="16" color="info" class="mr-1"></v-icon>
          暂无规则。请选择条件后点击“添加规则”。
        </div>

        <div v-else class="mr-table-wrap mt-3">
          <v-table density="comfortable">
            <thead>
              <tr>
                <th>插件或关键字</th>
                <th class="mr-col-center">目标消息类型</th>
                <th class="mr-col-center">系统微信通知</th>
                <th class="mr-col-center">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(rule, index) in routeRules" :key="`${rule.plugin}-${index}`">
                <td>{{ rule.plugin }}</td>
                <td class="mr-col-center">
                  <span class="mr-cell-inline">{{ rule.type || '不修改' }}</span>
                </td>
                <td class="mr-col-center">
                  <span class="mr-cell-inline">{{ rule.app || '不直推' }}</span>
                </td>
                <td class="mr-col-center">
                  <span class="mr-cell-inline">
                    <v-btn color="error" variant="text" size="small" icon="mdi-delete-outline" @click="removeRule(index)" />
                  </span>
                </td>
              </tr>
            </tbody>
          </v-table>
        </div>
      </div>

    </div>

    <!-- 插件配置使用文档与说明 -->
    <div class="mr-card">
      <div class="mr-card__header">
        <span class="mr-card__title d-flex align-center">
          <v-icon icon="mdi-book-open-page-variant-outline" size="18" color="#0ea5e9" class="mr-1"></v-icon>使用说明
        </span>
      </div>
      
      <div class="mr-desc-content" style="color: rgba(var(--v-theme-on-surface), 0.78);">
        <div class="mb-2"><strong>🎯 核心目标：</strong>可深度接管系统底层的通知中心与事件枢纽，任意改变特定插件发出的通知行为。</div>
        
        <v-divider class="my-2"></v-divider>
        
        <div class="mb-1"><strong>📚 基础概念：</strong></div>
        <ul class="pl-5 mb-2">
          <li class="mb-1"><strong>✨ 伪装消息类型：</strong>将通知强制伪装为别的消息类型（以触发其他分支逻辑）。</li>
          <li class="mb-1"><strong>🚀 独立通道直推：</strong>绕过系统原生广播，走指定的分应用企业微信通道独立推送，实现手机端的精细化应用分流。</li>
        </ul>
        
        <v-divider class="my-2"></v-divider>

        <div class="mb-1"><strong>👣 操作技巧：</strong></div>
        <ol class="pl-5 mb-2">
          <li class="mb-1"><strong>模糊匹配：</strong>下拉框没找到需要的源？手动打字输入该类通知里的<b>文本关键字</b>（如输入“豆瓣”），即可直接拦截匹配！</li>
          <li class="mb-1"><strong>静音合并：</strong>如果只希望把某个杂乱的插件通知合并到“整理入库”分类里，直接把“目标类型”选为整理入库，然后“系统微信通知名称”不选即为“<b>不直推</b>”。</li>
        </ol>

        <div class="mr-alert-rules mt-3 text-caption">
          <strong>💡 阻断机制解析（直推后阻断）：</strong><br>
          开启后，只要该通知命中了你的“企微通道直推”或者“类型转换”，插件就会在底层第一现场粉碎它残留在上游的原始数据包。这样绝对防止通知被默认管道再次捕捉从而引发重复群发。
        </div>
      </div>
    </div>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="2500" location="top">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<style scoped>
.mr-config {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", sans-serif;
  -webkit-font-smoothing: antialiased;
  color: rgba(var(--v-theme-on-surface), 0.85);
  min-height: 400px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  border-radius: 8px;
}
.mr-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 8px;
}
.mr-topbar__left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.mr-topbar__right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.mr-topbar__icon {
  width: 42px;
  height: 42px;
  border-radius: 11px;
  background: rgba(var(--v-theme-primary), 0.12);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  color: rgb(var(--v-theme-primary));
  flex-shrink: 0;
}
.mr-topbar__title {
  font-size: 16px;
  font-weight: 600;
  letter-spacing: -0.3px;
  color: rgba(var(--v-theme-on-surface), 0.85);
}
.mr-topbar__sub {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.55);
  margin-top: 2px;
}
.mr-card {
  background: rgba(var(--v-theme-on-surface), 0.03);
  backdrop-filter: blur(20px) saturate(150%);
  border-radius: 14px;
  border: 0.5px solid rgba(var(--v-theme-on-surface), 0.08);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.05);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.mr-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.mr-card__title {
  font-size: 13px;
  font-weight: 600;
  color: rgba(var(--v-theme-on-surface), 0.85);
}
.mr-desc-content {
  font-size: 13px;
  line-height: 1.6;
  color: rgba(var(--v-theme-on-surface), 0.7);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mr-desc-list {
  padding-left: 20px;
  margin-top: 2px;
}
.mr-desc-list li {
  margin-bottom: 4px;
}
.mr-row__text {
  font-size: 14px;
  color: rgba(var(--v-theme-on-surface), 0.85);
  display: flex;
  align-items: center;
  gap: 6px;
}
.mr-divider {
  height: 0.5px;
  background: rgba(var(--v-theme-on-surface), 0.08);
  margin: 0 -4px;
}
.mr-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mr-field__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.mr-field__title-block {
  min-height: 36px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: flex-start;
  gap: 2px;
}
.mr-field__title-main {
  display: flex;
  align-items: center;
  gap: 6px;
}
.mr-field__title-icon {
  flex-shrink: 0;
}
.mr-field__title-text {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 2px;
}
.mr-field__label {
  font-size: 13px;
  color: rgba(var(--v-theme-on-surface), 0.7);
}
.mr-field__hint {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.45);
}
.mr-field__hint--compact {
  line-height: 1.2;
  text-align: left;
}
.mr-input :deep(.v-field) {
  background: rgba(var(--v-theme-on-surface), 0.03) !important;
  border-radius: 8px !important;
}
.mr-input :deep(.v-field__outline) {
  --v-field-border-opacity: 0.15;
}
.mr-table-wrap {
  border: 0.5px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 10px;
  overflow: hidden;
  background: rgba(var(--v-theme-on-surface), 0.02);
}
.mr-table-wrap :deep(th),
.mr-table-wrap :deep(td) {
  vertical-align: middle;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}
.mr-table-wrap :deep(td) {
  height: 40px;
}
.mr-table-wrap :deep(th:nth-child(2)),
.mr-table-wrap :deep(th:nth-child(3)),
.mr-table-wrap :deep(th:nth-child(4)),
.mr-table-wrap :deep(td:nth-child(2)),
.mr-table-wrap :deep(td:nth-child(3)),
.mr-table-wrap :deep(td:nth-child(4)) {
  text-align: center !important;
}
.mr-col-center {
  text-align: center;
}
.mr-cell-inline {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 40px;
}
.mr-empty-state {
  font-size: 13px;
  line-height: 1.7;
  color: rgba(var(--v-theme-on-surface), 0.65);
  background: rgba(var(--v-theme-info), 0.08);
  border: 1px dashed rgba(var(--v-theme-info), 0.22);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
}
.mr-alert-rules {
  background: rgba(59, 130, 246, 0.12);
  border: 0.5px solid rgba(59, 130, 246, 0.3);
  border-radius: 14px;
  padding: 16px 14px;
  line-height: 1.6;
  color: #3b82f6;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.2), 0 2px 12px rgba(var(--v-theme-on-surface), 0.1);
  backdrop-filter: blur(20px) saturate(150%);
}
.switch {
  --switch-width: 36px;
  --switch-height: 20px;
  --switch-bg: rgba(var(--v-theme-on-surface), 0.22);
  --switch-checked-bg: rgb(var(--v-theme-primary));
  --switch-offset: calc((var(--switch-height) - var(--circle-diameter)) / 2);
  --switch-transition: all .2s cubic-bezier(0.27, 0.2, 0.25, 1.51);
  --circle-diameter: 16px;
  --circle-bg: #fff;
  --circle-shadow: 1px 1px 2px rgba(0, 0, 0, 0.2);
  --circle-checked-shadow: -1px 1px 2px rgba(0, 0, 0, 0.2);
  --circle-transition: var(--switch-transition);
  --icon-transition: all .2s cubic-bezier(0.27, 0.2, 0.25, 1.51);
  --icon-cross-color: rgba(0, 0, 0, 0.4);
  --icon-cross-size: 6px;
  --icon-checkmark-color: var(--switch-checked-bg);
  --icon-checkmark-size: 10px;
  --effect-width: calc(var(--circle-diameter) / 2);
  --effect-height: calc(var(--effect-width) / 2 - 1px);
  --effect-bg: var(--circle-bg);
  --effect-border-radius: 1px;
  --effect-transition: all .2s ease-in-out;
  display: inline-block;
  margin-left: 10px;
  user-select: none;
}
.switch input { display: none; }
.switch svg { transition: var(--icon-transition); position: absolute; height: auto; }
.switch .checkmark { width: var(--icon-checkmark-size); color: var(--icon-checkmark-color); transform: scale(0); }
.switch .cross { width: var(--icon-cross-size); color: var(--icon-cross-color); }
.slider { box-sizing: border-box; width: var(--switch-width); height: var(--switch-height); background: var(--switch-bg); border-radius: 999px; display: flex; align-items: center; position: relative; transition: var(--switch-transition); cursor: pointer; }
.circle { width: var(--circle-diameter); height: var(--circle-diameter); background: var(--circle-bg); border-radius: inherit; box-shadow: var(--circle-shadow); display: flex; align-items: center; justify-content: center; transition: var(--circle-transition); z-index: 1; position: absolute; left: var(--switch-offset); }
.slider::before { content: ""; position: absolute; width: var(--effect-width); height: var(--effect-height); left: calc(var(--switch-offset) + (var(--effect-width) / 2)); background: var(--effect-bg); border-radius: var(--effect-border-radius); transition: var(--effect-transition); }
.switch input:checked+.slider { background: var(--switch-checked-bg); }
.switch input:checked+.slider .checkmark { transform: scale(1); }
.switch input:checked+.slider .cross { transform: scale(0); }
.switch input:checked+.slider::before { left: calc(100% - var(--effect-width) - (var(--effect-width) / 2) - var(--switch-offset)); }
.switch input:checked+.slider .circle { left: calc(100% - var(--circle-diameter) - var(--switch-offset)); box-shadow: var(--circle-checked-shadow); }
.switch input:disabled+.slider { opacity: 0.5; cursor: not-allowed; }

@media (max-width: 600px) {
  .mr-field__header {
    flex-wrap: wrap;
  }
}
</style>

<style>
/* 针对该插件独属的下拉菜单进行越权排版修复 */

.mr-select-menu {
  /* 魔法机制：利用 CSS 的优先度让 max-width=0 被原内联写入的 min-width 覆盖，使得宽度精准定死为 min-width（即输入框宽度） */
  max-width: 0 !important;
  margin-top: 4px;
}

/* 覆盖深色模式下过于生硬刺眼的纯黑底色，采用深蓝灰调 */
.v-theme--dark .mr-select-menu,
.v-theme--dark .mr-select-menu .v-list,
.v-theme--dark .mr-select-menu .v-list-item {
  background-color: #2a2a35 !important;
}

/* Vuetify 3 中正确的选项文本容器是 .v-list-item-title */
/* 配合外层 maxWidth 为实际宽度的限制，对其做单行裁切省略 */
.mr-select-menu .v-list-item-title {
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  font-size: 13px !important;
  line-height: normal !important;
  display: block !important;
}
</style>
