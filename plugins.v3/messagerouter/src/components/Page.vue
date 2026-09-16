<script setup lang="ts">
import { computed, onMounted, reactive } from 'vue'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['action', 'switch', 'close'])

const loading = reactive({ overview: false })
const overview = reactive({
  enabled: false,
  block_system: false,
  rule_count: 0,
  wechat_app_count: 0,
  hook_count: 0,
  rules: [] as Array<any>,
  logs: [] as Array<string>,
  wechat_apps: {} as Record<string, any>,
})

const appEntries = computed(() => Object.entries(overview.wechat_apps || {}))

async function fetchOverview() {
  loading.overview = true
  try {
    const res = await props.api.get('plugin/MessageRouter/overview')
    Object.assign(overview, res || {})
  } catch (e) {
    console.warn('fetchOverview error', e)
  } finally {
    loading.overview = false
  }
}

onMounted(fetchOverview)
</script>

<template>
  <div class="mr-page">
    <div class="mr-topbar">
      <div class="mr-topbar__left">
        <div class="mr-topbar__icon">
          <v-icon icon="mdi-transit-connection-variant" size="24" />
        </div>
        <div>
          <div class="mr-topbar__title">插件消息重定向</div>
          <div class="mr-topbar__sub">实时规则、企微通道与拦截日志概览</div>
        </div>
      </div>
      <div class="mr-topbar__right">
        <v-btn-group variant="tonal" density="compact" class="elevation-0">
          <v-btn color="primary" @click="fetchOverview" :loading="loading.overview" size="small" min-width="40" class="px-0 px-sm-3">
            <v-icon icon="mdi-refresh" size="18" class="mr-sm-1"></v-icon>
            <span class="btn-text d-none d-sm-inline">刷新</span>
          </v-btn>
          <v-btn color="primary" @click="emit('switch', 'Config')" size="small" min-width="40" class="px-0 px-sm-3">
            <v-icon icon="mdi-cog" size="18" class="mr-sm-1"></v-icon>
            <span class="btn-text d-none d-sm-inline">配置</span>
          </v-btn>
          <v-btn color="primary" @click="emit('close')" size="small" min-width="40" class="px-0 px-sm-3">
            <v-icon icon="mdi-close" size="18"></v-icon>
            <span class="btn-text d-none d-sm-inline">关闭</span>
          </v-btn>
        </v-btn-group>
      </div>
    </div>

    <div class="mr-results">
      <div class="mr-result-card mr-result-card--status">
        <div class="mr-result-card__label">插件状态</div>
        <div class="mr-result-card__value">{{ overview.enabled ? '已启用' : '未启用' }}</div>
        <div class="mr-result-card__unit">阻断播报：{{ overview.block_system ? '开启' : '关闭' }}</div>
      </div>
      <div class="mr-result-card mr-result-card--rules">
        <div class="mr-result-card__label">生效规则</div>
        <div class="mr-result-card__value">{{ overview.rule_count }}</div>
        <div class="mr-result-card__unit">已挂载 Hook：{{ overview.hook_count }} 个</div>
      </div>
      <div class="mr-result-card mr-result-card--wechat">
        <div class="mr-result-card__label">企微通知通道</div>
        <div class="mr-result-card__value">{{ overview.wechat_app_count }}</div>
        <div class="mr-result-card__unit">已读取系统微信配置</div>
      </div>
    </div>

    <v-row class="mr-panel-row">

      <v-col cols="12" md="5" class="mr-panel-col d-flex flex-column" style="gap: 16px;">
        <!-- 系统微信通知卡片 -->
        <div class="mr-card">
          <div class="mr-card__header">
            <span class="mr-card__title d-flex align-center">
              <v-icon icon="mdi-wechat" size="18" color="#10b981" class="mr-1" />
              系统微信通知
            </span>
            <span class="mr-card__badge">{{ appEntries.length }} 个</span>
          </div>

          <div v-if="!appEntries.length" class="mr-empty-state">
            <v-icon icon="mdi-wechat" size="16" color="success" class="mr-1"></v-icon>
            未获取到系统微信通知配置
          </div>

          <div v-else class="mr-app-list">
            <div v-for="[name, app] in appEntries" :key="name" class="mr-app-list__item">
              <span class="mr-app-list__name">{{ name }}</span>
              <span class="mr-app-list__meta">AgentID: {{ app.appid || '-' }}</span>
            </div>
          </div>
        </div>

        <!-- 当前规则卡片 -->
        <div class="mr-card mr-card--panel">
          <div class="mr-card__header">
            <span class="mr-card__title d-flex align-center">
              <v-icon icon="mdi-shield-check" size="18" color="info" class="mr-1" />
              当前规则
            </span>
            <span class="mr-card__badge">{{ overview.rule_count }} 条</span>
          </div>

          <div class="mr-table-wrap">
            <table class="mr-table">
              <thead>
                <tr>
                  <th>插件或关键字</th>
                  <th class="mr-table__action">动作说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!overview.rules.length">
                  <td colspan="2" class="mr-empty-row">暂无规则</td>
                </tr>
                <tr
                  v-for="(rule, index) in overview.rules"
                  :key="`${rule.plugin}-${index}`"
                  :class="{ 'mr-table__row--alt': index % 2 === 1 }"
                >
                  <td class="mr-table__plugin">{{ rule.plugin }}</td>
                  <td class="mr-table__action">{{ rule.description }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </v-col>

      <v-col cols="12" md="7" class="mr-panel-col">
        <!-- 日志卡片 -->
        <div class="mr-card mr-card--panel">
          <div class="mr-card__header">
            <span class="mr-card__title d-flex align-center">
              <v-icon icon="mdi-console" size="18" color="#8b5cf6" class="mr-1" />
              实时路由监控日志
            </span>
            <span class="mr-card__badge">{{ overview.logs.length }} 条</span>
          </div>

          <div class="mr-log-box">
            <div v-if="!overview.logs.length" class="mr-log-empty">暂无日志</div>
            <div v-for="(log, index) in overview.logs" :key="index" class="mr-log-line">{{ log }}</div>
          </div>
        </div>
      </v-col>
    </v-row>
  </div>
</template>

<style scoped>
.mr-page {
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

.mr-results {
  display: flex;
  align-items: stretch;
  gap: 16px;
  width: 100%;
}

.mr-result-card {
  flex: 1;
  min-width: 0;
  border-radius: 14px;
  padding: 16px 14px;
  backdrop-filter: blur(20px) saturate(150%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  box-shadow:
    inset 0 1px 0 rgba(255,255,255,0.2),
    0 2px 12px rgba(var(--v-theme-on-surface), 0.1);
}

.mr-result-card--status {
  background: rgba(139, 92, 246, 0.12);
  border: 0.5px solid rgba(139, 92, 246, 0.3);
}

.mr-result-card--rules {
  background: rgba(59, 130, 246, 0.12);
  border: 0.5px solid rgba(59, 130, 246, 0.3);
}

.mr-result-card--wechat {
  background: rgba(16, 185, 129, 0.12);
  border: 0.5px solid rgba(16, 185, 129, 0.3);
}

.mr-result-card__label {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.6);
  letter-spacing: 0.5px;
}

.mr-result-card__value {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -1px;
  line-height: 1;
}

.mr-result-card--status .mr-result-card__value { color: #8b5cf6; }
.mr-result-card--rules .mr-result-card__value { color: #3b82f6; }
.mr-result-card--wechat .mr-result-card__value { color: #10b981; }

.mr-result-card__unit {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.mr-card {
  background: rgba(var(--v-theme-on-surface), 0.03);
  backdrop-filter: blur(20px) saturate(150%);
  border-radius: 14px;
  border: 0.5px solid rgba(var(--v-theme-on-surface), 0.08);
  box-shadow: 0 2px 10px rgba(0,0,0,0.05);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.mr-card--panel {
  height: 100%;
}

.mr-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.mr-card__title {
  font-size: 13px;
  font-weight: 600;
  color: rgba(var(--v-theme-on-surface), 0.85);
}

.mr-card__badge {
  font-size: 11px;
  background: rgba(var(--v-theme-on-surface), 0.08);
  color: rgba(var(--v-theme-on-surface), 0.6);
  padding: 2px 8px;
  border-radius: 20px;
}

.mr-panel-row {
  margin: -8px;
}

.mr-panel-col {
  padding: 8px !important;
}

.mr-table-wrap {
  overflow-x: auto;
}

.mr-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.mr-table th {
  text-align: center;
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-weight: 500;
  padding: 8px;
  border-bottom: 0.5px solid rgba(var(--v-theme-on-surface), 0.08);
  white-space: nowrap;
}

.mr-table th:first-child,
.mr-table td:first-child {
  text-align: left;
}

.mr-table td {
  padding: 10px 8px;
  color: rgba(var(--v-theme-on-surface), 0.85);
  border-bottom: 0.5px solid rgba(var(--v-theme-on-surface), 0.04);
  white-space: nowrap;
}

.mr-table__row--alt td {
  background: rgba(var(--v-theme-on-surface), 0.02);
}

.mr-table__plugin {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 11px;
}

.mr-table__action {
  text-align: center;
}

.mr-empty-row {
  text-align: center !important;
  color: rgba(var(--v-theme-on-surface), 0.5);
  padding: 18px 8px !important;
}

.mr-empty-state {
  font-size: 13px;
  line-height: 1.7;
  color: rgba(var(--v-theme-on-surface), 0.65);
  background: rgba(var(--v-theme-success), 0.08);
  border: 1px dashed rgba(var(--v-theme-success), 0.22);
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
}

.mr-app-list__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 0.5px solid rgba(var(--v-theme-on-surface), 0.06);
}

.mr-app-list__item:first-child {
  padding-top: 0;
}

.mr-app-list__item:last-child {
  padding-bottom: 0;
  border-bottom: 0;
}

.mr-app-list__name {
  font-size: 13px;
  font-weight: 600;
  color: rgba(var(--v-theme-on-surface), 0.85);
}

.mr-app-list__meta {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.55);
  word-break: break-all;
  text-align: right;
}

.mr-log-box {
  flex-grow: 1;
  max-height: 560px;
  overflow: auto;
  padding: 12px;
  border-radius: 12px;
  background: rgba(var(--v-theme-on-surface), 0.03);
  border: 0.5px solid rgba(var(--v-theme-on-surface), 0.08);
  color: rgba(var(--v-theme-on-surface), 0.78);
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
}

.mr-log-empty {
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.mr-log-line + .mr-log-line {
  margin-top: 6px;
}
.mr-log-line {
  padding: 2px 0;
}

@media (max-width: 600px) {
  .mr-results {
    gap: 8px;
  }
  .mr-result-card {
    padding: 12px 6px;
    border-radius: 10px;
  }
  .mr-result-card__value {
    font-size: 22px;
  }
  .mr-result-card__label, .mr-result-card__unit {
    font-size: 10px;
  }
}
</style>
