import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper-pcqpp-6-.js';

const {defineComponent:_defineComponent} = await importShared('vue');

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,withCtx:_withCtx,createTextVNode:_createTextVNode,vModelCheckbox:_vModelCheckbox,withDirectives:_withDirectives,openBlock:_openBlock,createElementBlock:_createElementBlock,renderList:_renderList,Fragment:_Fragment,toDisplayString:_toDisplayString,createStaticVNode:_createStaticVNode} = await importShared('vue');

const _hoisted_1 = { class: "mr-config" };
const _hoisted_2 = { class: "mr-topbar" };
const _hoisted_3 = { class: "mr-topbar__left" };
const _hoisted_4 = { class: "mr-topbar__icon" };
const _hoisted_5 = { class: "mr-topbar__right" };
const _hoisted_6 = { class: "mr-card" };
const _hoisted_7 = { class: "mr-card__header" };
const _hoisted_8 = { class: "mr-card__title d-flex align-center" };
const _hoisted_9 = { class: "mr-row__text" };
const _hoisted_10 = {
  class: "switch",
  style: { "--switch-checked-bg": "#a78bfa" }
};
const _hoisted_11 = { class: "slider" };
const _hoisted_12 = { class: "circle" };
const _hoisted_13 = {
  class: "cross",
  "xml:space": "preserve",
  style: { "enable-background": "new 0 0 512 512" },
  viewBox: "0 0 365.696 365.696",
  y: "0",
  x: "0",
  height: "6",
  width: "6",
  "xmlns:xlink": "http://www.w3.org/1999/xlink",
  version: "1.1",
  xmlns: "http://www.w3.org/2000/svg"
};
const _hoisted_14 = {
  class: "checkmark",
  "xml:space": "preserve",
  style: { "enable-background": "new 0 0 512 512" },
  viewBox: "0 0 24 24",
  y: "0",
  x: "0",
  height: "10",
  width: "10",
  "xmlns:xlink": "http://www.w3.org/1999/xlink",
  version: "1.1",
  xmlns: "http://www.w3.org/2000/svg"
};
const _hoisted_15 = { class: "mr-row__text" };
const _hoisted_16 = {
  class: "switch",
  style: { "--switch-checked-bg": "rgb(var(--v-theme-info))" }
};
const _hoisted_17 = { class: "slider" };
const _hoisted_18 = { class: "circle" };
const _hoisted_19 = {
  class: "cross",
  "xml:space": "preserve",
  style: { "enable-background": "new 0 0 512 512" },
  viewBox: "0 0 365.696 365.696",
  y: "0",
  x: "0",
  height: "6",
  width: "6",
  "xmlns:xlink": "http://www.w3.org/1999/xlink",
  version: "1.1",
  xmlns: "http://www.w3.org/2000/svg"
};
const _hoisted_20 = {
  class: "checkmark",
  "xml:space": "preserve",
  style: { "enable-background": "new 0 0 512 512" },
  viewBox: "0 0 24 24",
  y: "0",
  x: "0",
  height: "10",
  width: "10",
  "xmlns:xlink": "http://www.w3.org/1999/xlink",
  version: "1.1",
  xmlns: "http://www.w3.org/2000/svg"
};
const _hoisted_21 = {
  class: "mr-field",
  style: { "margin-top": "12px", "margin-bottom": "8px" }
};
const _hoisted_22 = { class: "mr-field__header mb-1" };
const _hoisted_23 = { class: "mr-field__title-block" };
const _hoisted_24 = { class: "mr-field__title-main" };
const _hoisted_25 = {
  key: 0,
  class: "mr-empty-state mt-2"
};
const _hoisted_26 = {
  key: 1,
  class: "mr-table-wrap mt-3"
};
const _hoisted_27 = { class: "mr-col-center" };
const _hoisted_28 = { class: "mr-cell-inline" };
const _hoisted_29 = { class: "mr-col-center" };
const _hoisted_30 = { class: "mr-cell-inline" };
const _hoisted_31 = { class: "mr-col-center" };
const _hoisted_32 = { class: "mr-cell-inline" };
const _hoisted_33 = { class: "mr-card" };
const _hoisted_34 = { class: "mr-card__header" };
const _hoisted_35 = { class: "mr-card__title d-flex align-center" };
const _hoisted_36 = {
  class: "mr-desc-content",
  style: { "color": "rgba(var(--v-theme-on-surface), 0.78)" }
};
const {computed,reactive,ref,watch,onMounted} = await importShared('vue');

const _sfc_main = /* @__PURE__ */ _defineComponent({
  __name: "Config",
  props: {
    initialConfig: { type: Object, default: () => ({}) },
    api: { type: Object, default: () => ({}) }
  },
  emits: ["save", "close", "switch"],
  setup(__props, { emit: __emit }) {
    const props = __props;
    const emit = __emit;
    const config = reactive({
      enabled: false,
      block_system: false,
      plugin_mapping: "",
      route_rules: [],
      ...props.initialConfig
    });
    const routeRules = ref([]);
    const optionState = reactive({
      plugins: [],
      notification_types: [],
      wechat_apps: []
    });
    const ruleForm = reactive({
      plugin: null,
      type: null,
      app: null
    });
    watch(
      () => props.initialConfig,
      (val) => {
        Object.assign(config, val || {});
        routeRules.value = normalizeRules((val || {}).route_rules, (val || {}).plugin_mapping);
      },
      { deep: true }
    );
    const saving = ref(false);
    const loadingConfig = ref(false);
    const loadingOptions = ref(false);
    const snackbar = reactive({ show: false, text: "", color: "success" });
    const canAddRule = computed(() => !!ruleForm.plugin);
    function normalizeRules(rules, mappingText = "") {
      if (Array.isArray(rules) && rules.length) {
        return rules.map((item) => ({
          plugin: String(item?.plugin || "").trim(),
          type: String(item?.type || "").trim(),
          app: String(item?.app || "").trim()
        })).filter((item) => item.plugin);
      }
      return String(mappingText || "").split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
        const parts = line.split(":");
        return {
          plugin: String(parts[0] || "").trim(),
          type: String(parts[1] || "").trim(),
          app: String(parts[2] || "").trim()
        };
      }).filter((item) => item.plugin);
    }
    function syncConfigRules() {
      config.route_rules = routeRules.value.map((item) => ({ ...item }));
      config.plugin_mapping = routeRules.value.map((item) => `${item.plugin}:${item.type || ""}:${item.app || ""}`).join("\n");
    }
    function addRule() {
      if (!ruleForm.plugin) {
        return;
      }
      const pluginVal = typeof ruleForm.plugin === "object" && ruleForm.plugin !== null ? ruleForm.plugin.value || ruleForm.plugin.title || "" : String(ruleForm.plugin || "");
      const duplicateIndex = routeRules.value.findIndex(
        (item) => item.plugin === pluginVal
      );
      const newRule = {
        plugin: pluginVal,
        type: ruleForm.type || "",
        app: ruleForm.app || ""
      };
      if (duplicateIndex >= 0) {
        routeRules.value.splice(duplicateIndex, 1, newRule);
      } else {
        routeRules.value.push(newRule);
      }
      syncConfigRules();
      ruleForm.plugin = null;
      ruleForm.type = null;
      ruleForm.app = null;
      handleSave(false);
    }
    function removeRule(index) {
      routeRules.value.splice(index, 1);
      syncConfigRules();
      handleSave(false);
    }
    async function loadConfig() {
      loadingConfig.value = true;
      try {
        const data = await props.api.get("plugin/MessageRouter/config");
        Object.assign(config, {
          enabled: false,
          block_system: false,
          plugin_mapping: "",
          route_rules: [],
          ...data || {}
        });
        routeRules.value = normalizeRules((data || {}).route_rules, (data || {}).plugin_mapping);
        syncConfigRules();
      } catch (e) {
        routeRules.value = normalizeRules(config.route_rules, config.plugin_mapping);
        syncConfigRules();
      } finally {
        loadingConfig.value = false;
      }
    }
    async function loadOptions() {
      loadingOptions.value = true;
      try {
        const data = await props.api.get("plugin/MessageRouter/options");
        optionState.plugins = data?.plugins || [];
        optionState.notification_types = data?.notification_types || [];
        optionState.wechat_apps = data?.wechat_apps || [];
      } catch (e) {
        snackbar.text = "选项加载失败";
        snackbar.color = "warning";
        snackbar.show = true;
      } finally {
        loadingOptions.value = false;
      }
    }
    onMounted(async () => {
      await loadConfig();
      await loadOptions();
    });
    async function handleSave(isManual = false) {
      saving.value = true;
      try {
        syncConfigRules();
        if (isManual === true) {
          emit("save", { ...config, route_rules: routeRules.value.map((item) => ({ ...item })) });
        }
        const result = await props.api.post("plugin/MessageRouter/config", {
          ...config,
          route_rules: routeRules.value.map((item) => ({ ...item }))
        }).catch(() => null);
        if (result?.config) {
          Object.assign(config, result.config);
          routeRules.value = normalizeRules(result.config.route_rules, result.config.plugin_mapping);
          syncConfigRules();
        }
        snackbar.text = "配置已保存";
        snackbar.color = "success";
        snackbar.show = true;
      } catch (e) {
        snackbar.text = "保存失败";
        snackbar.color = "error";
        snackbar.show = true;
      } finally {
        saving.value = false;
      }
    }
    return (_ctx, _cache) => {
      const _component_v_icon = _resolveComponent("v-icon");
      const _component_v_btn = _resolveComponent("v-btn");
      const _component_v_btn_group = _resolveComponent("v-btn-group");
      const _component_v_col = _resolveComponent("v-col");
      const _component_v_row = _resolveComponent("v-row");
      const _component_v_combobox = _resolveComponent("v-combobox");
      const _component_v_select = _resolveComponent("v-select");
      const _component_v_table = _resolveComponent("v-table");
      const _component_v_divider = _resolveComponent("v-divider");
      const _component_v_snackbar = _resolveComponent("v-snackbar");
      return _openBlock(), _createElementBlock("div", _hoisted_1, [
        _createElementVNode("div", _hoisted_2, [
          _createElementVNode("div", _hoisted_3, [
            _createElementVNode("div", _hoisted_4, [
              _createVNode(_component_v_icon, {
                icon: "mdi-tune-variant",
                size: "24"
              })
            ]),
            _cache[9] || (_cache[9] = _createElementVNode("div", null, [
              _createElementVNode("div", { class: "mr-topbar__title" }, "插件 · 配置"),
              _createElementVNode("div", { class: "mr-topbar__sub" }, "Message Router Plugin")
            ], -1))
          ]),
          _createElementVNode("div", _hoisted_5, [
            _createVNode(_component_v_btn_group, {
              variant: "tonal",
              density: "compact",
              class: "elevation-0"
            }, {
              default: _withCtx(() => [
                _createVNode(_component_v_btn, {
                  color: "primary",
                  onClick: _cache[0] || (_cache[0] = ($event) => emit("switch", "Page")),
                  size: "small",
                  "min-width": "40",
                  class: "px-0 px-sm-3"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-view-dashboard",
                      size: "18",
                      class: "mr-sm-1"
                    }),
                    _cache[10] || (_cache[10] = _createElementVNode("span", { class: "btn-text d-none d-sm-inline" }, "状态页", -1))
                  ]),
                  _: 1
                }),
                _createVNode(_component_v_btn, {
                  color: "primary",
                  onClick: _cache[1] || (_cache[1] = () => handleSave(true)),
                  loading: saving.value,
                  size: "small",
                  "min-width": "40",
                  class: "px-0 px-sm-3"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-content-save",
                      size: "18",
                      class: "mr-sm-1"
                    }),
                    _cache[11] || (_cache[11] = _createElementVNode("span", { class: "btn-text d-none d-sm-inline" }, "保存", -1))
                  ]),
                  _: 1
                }, 8, ["loading"]),
                _createVNode(_component_v_btn, {
                  color: "primary",
                  onClick: _cache[2] || (_cache[2] = ($event) => emit("close")),
                  size: "small",
                  "min-width": "40",
                  class: "px-0 px-sm-3"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-close",
                      size: "18"
                    }),
                    _cache[12] || (_cache[12] = _createElementVNode("span", { class: "btn-text d-none d-sm-inline" }, "关闭", -1))
                  ]),
                  _: 1
                })
              ]),
              _: 1
            })
          ])
        ]),
        _createElementVNode("div", _hoisted_6, [
          _createElementVNode("div", _hoisted_7, [
            _createElementVNode("span", _hoisted_8, [
              _createVNode(_component_v_icon, {
                icon: "mdi-tune-vertical",
                size: "18",
                color: "#8b5cf6",
                class: "mr-1"
              }),
              _cache[13] || (_cache[13] = _createTextVNode("基础设置 ", -1))
            ])
          ]),
          _createVNode(_component_v_row, { class: "mt-1 mb-1" }, {
            default: _withCtx(() => [
              _createVNode(_component_v_col, {
                cols: "12",
                sm: "6",
                class: "d-flex align-center justify-space-between py-1"
              }, {
                default: _withCtx(() => [
                  _createElementVNode("span", _hoisted_9, [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-power-plug",
                      size: "20",
                      color: config.enabled ? "#a78bfa" : "grey",
                      class: "mr-2"
                    }, null, 8, ["color"]),
                    _cache[14] || (_cache[14] = _createTextVNode(" 启用高级路由与企微直推 ", -1))
                  ]),
                  _createElementVNode("label", _hoisted_10, [
                    _withDirectives(_createElementVNode("input", {
                      "onUpdate:modelValue": _cache[3] || (_cache[3] = ($event) => config.enabled = $event),
                      type: "checkbox"
                    }, null, 512), [
                      [_vModelCheckbox, config.enabled]
                    ]),
                    _createElementVNode("div", _hoisted_11, [
                      _createElementVNode("div", _hoisted_12, [
                        (_openBlock(), _createElementBlock("svg", _hoisted_13, [..._cache[15] || (_cache[15] = [
                          _createElementVNode("g", null, [
                            _createElementVNode("path", {
                              "data-original": "#000000",
                              fill: "currentColor",
                              d: "M243.188 182.86 356.32 69.726c12.5-12.5 12.5-32.766 0-45.247L341.238 9.398c-12.504-12.503-32.77-12.503-45.25 0L182.86 122.528 69.727 9.374c-12.5-12.5-32.766-12.5-45.247 0L9.375 24.457c-12.5 12.504-12.5 32.77 0 45.25l113.152 113.152L9.398 295.99c-12.503 12.503-12.503 32.769 0 45.25L24.48 356.32c12.5 12.5 32.766 12.5 45.247 0l113.132-113.132L295.99 356.32c12.503 12.5 32.769 12.5 45.25 0l15.081-15.082c12.5-12.504 12.5-32.77 0-45.25zm0 0"
                            })
                          ], -1)
                        ])])),
                        (_openBlock(), _createElementBlock("svg", _hoisted_14, [..._cache[16] || (_cache[16] = [
                          _createElementVNode("g", null, [
                            _createElementVNode("path", {
                              "data-original": "#000000",
                              fill: "currentColor",
                              d: "M9.707 19.121a.997.997 0 0 1-1.414 0l-5.646-5.647a1.5 1.5 0 0 1 0-2.121l.707-.707a1.5 1.5 0 0 1 2.121 0L9 14.171l9.525-9.525a1.5 1.5 0 0 1 2.121 0l.707.707a1.5 1.5 0 0 1 0 2.121z"
                            })
                          ], -1)
                        ])]))
                      ])
                    ])
                  ])
                ]),
                _: 1
              }),
              _createVNode(_component_v_col, {
                cols: "12",
                sm: "6",
                class: "d-flex align-center justify-space-between py-1"
              }, {
                default: _withCtx(() => [
                  _createElementVNode("span", _hoisted_15, [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-broadcast-off",
                      size: "20",
                      color: config.block_system ? "info" : "grey",
                      class: "mr-2"
                    }, null, 8, ["color"]),
                    _cache[17] || (_cache[17] = _createTextVNode(" 直推后阻断系统默认广播 ", -1))
                  ]),
                  _createElementVNode("label", _hoisted_16, [
                    _withDirectives(_createElementVNode("input", {
                      "onUpdate:modelValue": _cache[4] || (_cache[4] = ($event) => config.block_system = $event),
                      type: "checkbox"
                    }, null, 512), [
                      [_vModelCheckbox, config.block_system]
                    ]),
                    _createElementVNode("div", _hoisted_17, [
                      _createElementVNode("div", _hoisted_18, [
                        (_openBlock(), _createElementBlock("svg", _hoisted_19, [..._cache[18] || (_cache[18] = [
                          _createElementVNode("g", null, [
                            _createElementVNode("path", {
                              "data-original": "#000000",
                              fill: "currentColor",
                              d: "M243.188 182.86 356.32 69.726c12.5-12.5 12.5-32.766 0-45.247L341.238 9.398c-12.504-12.503-32.77-12.503-45.25 0L182.86 122.528 69.727 9.374c-12.5-12.5-32.766-12.5-45.247 0L9.375 24.457c-12.5 12.504-12.5 32.77 0 45.25l113.152 113.152L9.398 295.99c-12.503 12.503-12.503 32.769 0 45.25L24.48 356.32c12.5 12.5 32.766 12.5 45.247 0l113.132-113.132L295.99 356.32c12.503 12.5 32.769 12.5 45.25 0l15.081-15.082c12.5-12.504 12.5-32.77 0-45.25zm0 0"
                            })
                          ], -1)
                        ])])),
                        (_openBlock(), _createElementBlock("svg", _hoisted_20, [..._cache[19] || (_cache[19] = [
                          _createElementVNode("g", null, [
                            _createElementVNode("path", {
                              "data-original": "#000000",
                              fill: "currentColor",
                              d: "M9.707 19.121a.997.997 0 0 1-1.414 0l-5.646-5.647a1.5 1.5 0 0 1 0-2.121l.707-.707a1.5 1.5 0 0 1 2.121 0L9 14.171l9.525-9.525a1.5 1.5 0 0 1 2.121 0l.707.707a1.5 1.5 0 0 1 0 2.121z"
                            })
                          ], -1)
                        ])]))
                      ])
                    ])
                  ])
                ]),
                _: 1
              })
            ]),
            _: 1
          }),
          _cache[24] || (_cache[24] = _createElementVNode("div", { class: "mr-divider" }, null, -1)),
          _createElementVNode("div", _hoisted_21, [
            _createElementVNode("div", _hoisted_22, [
              _createElementVNode("div", _hoisted_23, [
                _createElementVNode("div", _hoisted_24, [
                  _createVNode(_component_v_icon, {
                    icon: "mdi-source-branch",
                    size: "18",
                    color: "info",
                    class: "mr-field__title-icon"
                  }),
                  _cache[20] || (_cache[20] = _createElementVNode("div", { class: "mr-field__title-text" }, [
                    _createElementVNode("label", { class: "mr-field__label" }, "高级消息路由映射规则"),
                    _createElementVNode("div", { class: "mr-field__hint mr-field__hint--compact" }, "选择或输入插件名/关键字、目标消息类型和企微应用后添加。")
                  ], -1))
                ])
              ]),
              _createVNode(_component_v_btn, {
                color: "primary",
                "prepend-icon": "mdi-plus",
                rounded: "lg",
                disabled: !canAddRule.value,
                onClick: addRule
              }, {
                default: _withCtx(() => [..._cache[21] || (_cache[21] = [
                  _createTextVNode("添加规则", -1)
                ])]),
                _: 1
              }, 8, ["disabled"])
            ]),
            _createVNode(_component_v_row, {
              dense: "",
              class: "mt-1"
            }, {
              default: _withCtx(() => [
                _createVNode(_component_v_col, {
                  cols: "12",
                  md: "4"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_combobox, {
                      modelValue: ruleForm.plugin,
                      "onUpdate:modelValue": _cache[5] || (_cache[5] = ($event) => ruleForm.plugin = $event),
                      items: optionState.plugins,
                      "item-title": "title",
                      "item-value": "value",
                      label: "插件名或标题关键字",
                      density: "compact",
                      variant: "outlined",
                      "hide-details": "auto",
                      class: "mr-input",
                      loading: loadingOptions.value || loadingConfig.value,
                      "menu-props": { contentClass: "mr-select-menu" },
                      "return-object": false
                    }, null, 8, ["modelValue", "items", "loading"])
                  ]),
                  _: 1
                }),
                _createVNode(_component_v_col, {
                  cols: "12",
                  md: "4"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_select, {
                      modelValue: ruleForm.type,
                      "onUpdate:modelValue": _cache[6] || (_cache[6] = ($event) => ruleForm.type = $event),
                      items: optionState.notification_types,
                      "item-title": "title",
                      "item-value": "value",
                      label: "目标消息类型",
                      density: "compact",
                      variant: "outlined",
                      "hide-details": "auto",
                      class: "mr-input",
                      loading: loadingOptions.value || loadingConfig.value,
                      "menu-props": { contentClass: "mr-select-menu" }
                    }, null, 8, ["modelValue", "items", "loading"])
                  ]),
                  _: 1
                }),
                _createVNode(_component_v_col, {
                  cols: "12",
                  md: "4"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_select, {
                      modelValue: ruleForm.app,
                      "onUpdate:modelValue": _cache[7] || (_cache[7] = ($event) => ruleForm.app = $event),
                      items: optionState.wechat_apps,
                      "item-title": "title",
                      "item-value": "value",
                      label: "系统微信通知名称",
                      density: "compact",
                      variant: "outlined",
                      "hide-details": "auto",
                      class: "mr-input",
                      loading: loadingOptions.value || loadingConfig.value,
                      "menu-props": { contentClass: "mr-select-menu" }
                    }, null, 8, ["modelValue", "items", "loading"])
                  ]),
                  _: 1
                })
              ]),
              _: 1
            }),
            !routeRules.value.length ? (_openBlock(), _createElementBlock("div", _hoisted_25, [
              _createVNode(_component_v_icon, {
                icon: "mdi-information-outline",
                size: "16",
                color: "info",
                class: "mr-1"
              }),
              _cache[22] || (_cache[22] = _createTextVNode(" 暂无规则。请选择条件后点击“添加规则”。 ", -1))
            ])) : (_openBlock(), _createElementBlock("div", _hoisted_26, [
              _createVNode(_component_v_table, { density: "comfortable" }, {
                default: _withCtx(() => [
                  _cache[23] || (_cache[23] = _createElementVNode("thead", null, [
                    _createElementVNode("tr", null, [
                      _createElementVNode("th", null, "插件或关键字"),
                      _createElementVNode("th", { class: "mr-col-center" }, "目标消息类型"),
                      _createElementVNode("th", { class: "mr-col-center" }, "系统微信通知"),
                      _createElementVNode("th", { class: "mr-col-center" }, "操作")
                    ])
                  ], -1)),
                  _createElementVNode("tbody", null, [
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(routeRules.value, (rule, index) => {
                      return _openBlock(), _createElementBlock("tr", {
                        key: `${rule.plugin}-${index}`
                      }, [
                        _createElementVNode("td", null, _toDisplayString(rule.plugin), 1),
                        _createElementVNode("td", _hoisted_27, [
                          _createElementVNode("span", _hoisted_28, _toDisplayString(rule.type || "不修改"), 1)
                        ]),
                        _createElementVNode("td", _hoisted_29, [
                          _createElementVNode("span", _hoisted_30, _toDisplayString(rule.app || "不直推"), 1)
                        ]),
                        _createElementVNode("td", _hoisted_31, [
                          _createElementVNode("span", _hoisted_32, [
                            _createVNode(_component_v_btn, {
                              color: "error",
                              variant: "text",
                              size: "small",
                              icon: "mdi-delete-outline",
                              onClick: ($event) => removeRule(index)
                            }, null, 8, ["onClick"])
                          ])
                        ])
                      ]);
                    }), 128))
                  ])
                ]),
                _: 1
              })
            ]))
          ])
        ]),
        _createElementVNode("div", _hoisted_33, [
          _createElementVNode("div", _hoisted_34, [
            _createElementVNode("span", _hoisted_35, [
              _createVNode(_component_v_icon, {
                icon: "mdi-book-open-page-variant-outline",
                size: "18",
                color: "#0ea5e9",
                class: "mr-1"
              }),
              _cache[25] || (_cache[25] = _createTextVNode("使用说明 ", -1))
            ])
          ]),
          _createElementVNode("div", _hoisted_36, [
            _cache[26] || (_cache[26] = _createElementVNode("div", { class: "mb-2" }, [
              _createElementVNode("strong", null, "🎯 核心目标："),
              _createTextVNode("可深度接管系统底层的通知中心与事件枢纽，任意改变特定插件发出的通知行为。")
            ], -1)),
            _createVNode(_component_v_divider, { class: "my-2" }),
            _cache[27] || (_cache[27] = _createElementVNode("div", { class: "mb-1" }, [
              _createElementVNode("strong", null, "📚 基础概念：")
            ], -1)),
            _cache[28] || (_cache[28] = _createElementVNode("ul", { class: "pl-5 mb-2" }, [
              _createElementVNode("li", { class: "mb-1" }, [
                _createElementVNode("strong", null, "✨ 伪装消息类型："),
                _createTextVNode("将通知强制伪装为别的消息类型（以触发其他分支逻辑）。")
              ]),
              _createElementVNode("li", { class: "mb-1" }, [
                _createElementVNode("strong", null, "🚀 独立通道直推："),
                _createTextVNode("绕过系统原生广播，走指定的分应用企业微信通道独立推送，实现手机端的精细化应用分流。")
              ])
            ], -1)),
            _createVNode(_component_v_divider, { class: "my-2" }),
            _cache[29] || (_cache[29] = _createStaticVNode('<div class="mb-1" data-v-3095b875><strong data-v-3095b875>👣 操作技巧：</strong></div><ol class="pl-5 mb-2" data-v-3095b875><li class="mb-1" data-v-3095b875><strong data-v-3095b875>模糊匹配：</strong>下拉框没找到需要的源？手动打字输入该类通知里的<b data-v-3095b875>文本关键字</b>（如输入“豆瓣”），即可直接拦截匹配！</li><li class="mb-1" data-v-3095b875><strong data-v-3095b875>静音合并：</strong>如果只希望把某个杂乱的插件通知合并到“整理入库”分类里，直接把“目标类型”选为整理入库，然后“系统微信通知名称”不选即为“<b data-v-3095b875>不直推</b>”。</li></ol><div class="mr-alert-rules mt-3 text-caption" data-v-3095b875><strong data-v-3095b875>💡 阻断机制解析（直推后阻断）：</strong><br data-v-3095b875> 开启后，只要该通知命中了你的“企微通道直推”或者“类型转换”，插件就会在底层第一现场粉碎它残留在上游的原始数据包。这样绝对防止通知被默认管道再次捕捉从而引发重复群发。 </div>', 3))
          ])
        ]),
        _createVNode(_component_v_snackbar, {
          modelValue: snackbar.show,
          "onUpdate:modelValue": _cache[8] || (_cache[8] = ($event) => snackbar.show = $event),
          color: snackbar.color,
          timeout: "2500",
          location: "top"
        }, {
          default: _withCtx(() => [
            _createTextVNode(_toDisplayString(snackbar.text), 1)
          ]),
          _: 1
        }, 8, ["modelValue", "color"])
      ]);
    };
  }
});

const Config = /* @__PURE__ */ _export_sfc(_sfc_main, [["__scopeId", "data-v-3095b875"]]);

export { Config as default };
