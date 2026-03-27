import { importShared } from './__federation_fn_import-054b33c3.js';

const {defineComponent:_defineComponent} = await importShared('vue');

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,renderList:_renderList,Fragment:_Fragment,openBlock:_openBlock,createElementBlock:_createElementBlock,withCtx:_withCtx,createTextVNode:_createTextVNode,createBlock:_createBlock} = await importShared('vue');

const _hoisted_1 = { class: "plugin-config pa-4" };
const _hoisted_2 = { class: "d-flex justify-end gap-2" };
const {ref,onMounted} = await importShared('vue');

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
    const config = ref({
      enabled: props.initialConfig?.enabled || false,
      rules: Array.isArray(props.initialConfig?.rules) ? props.initialConfig.rules : []
    });
    const pluginOptions = ref([]);
    const typeOptions = ["资源下载", "整理入库", "订阅", "站点", "媒体服务器", "手动处理", "插件", "其它"];
    onMounted(async () => {
      try {
        const res = await props.api.get("plugin/MessageRouterVue/plugins");
        if (res && res.success) {
          pluginOptions.value = res.data;
        }
      } catch (error) {
        console.error("获取插件列表失败", error);
      }
      if (config.value.rules.length === 0) {
        addRule();
      }
    });
    const addRule = () => {
      config.value.rules.push({ plugin: "", target: "" });
    };
    const removeRule = (index) => {
      config.value.rules.splice(index, 1);
    };
    const saveConfig = () => {
      config.value.rules = config.value.rules.filter((r) => r.plugin && r.target);
      emit("save", config.value);
    };
    const notifyClose = () => {
      emit("close");
    };
    return (_ctx, _cache) => {
      const _component_v_switch = _resolveComponent("v-switch");
      const _component_v_divider = _resolveComponent("v-divider");
      const _component_v_select = _resolveComponent("v-select");
      const _component_v_col = _resolveComponent("v-col");
      const _component_v_icon = _resolveComponent("v-icon");
      const _component_v_btn = _resolveComponent("v-btn");
      const _component_v_row = _resolveComponent("v-row");
      return _openBlock(), _createElementBlock("div", _hoisted_1, [
        _createVNode(_component_v_switch, {
          modelValue: config.value.enabled,
          "onUpdate:modelValue": _cache[0] || (_cache[0] = ($event) => config.value.enabled = $event),
          color: "primary",
          label: "启用插件消息路由"
        }, null, 8, ["modelValue"]),
        _createVNode(_component_v_divider, { class: "my-4" }),
        _cache[6] || (_cache[6] = _createElementVNode("div", { class: "text-h6 mb-3" }, "配置路由规则", -1)),
        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(config.value.rules, (rule, index) => {
          return _openBlock(), _createBlock(_component_v_row, {
            key: index,
            align: "center",
            dense: ""
          }, {
            default: _withCtx(() => [
              _createVNode(_component_v_col, {
                cols: "12",
                md: "5"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_select, {
                    modelValue: rule.plugin,
                    "onUpdate:modelValue": ($event) => rule.plugin = $event,
                    items: pluginOptions.value,
                    label: "监听来源插件",
                    placeholder: "请选择",
                    variant: "outlined",
                    density: "comfortable",
                    "hide-details": ""
                  }, null, 8, ["modelValue", "onUpdate:modelValue", "items"])
                ]),
                _: 2
              }, 1024),
              _createVNode(_component_v_col, {
                cols: "12",
                md: "1",
                class: "text-center"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_icon, {
                    size: "large",
                    color: "grey"
                  }, {
                    default: _withCtx(() => [..._cache[1] || (_cache[1] = [
                      _createTextVNode("mdi-arrow-right-thick", -1)
                    ])]),
                    _: 1
                  })
                ]),
                _: 1
              }),
              _createVNode(_component_v_col, {
                cols: "12",
                md: "5"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_select, {
                    modelValue: rule.target,
                    "onUpdate:modelValue": ($event) => rule.target = $event,
                    items: typeOptions,
                    label: "目标消息类型",
                    placeholder: "请选择",
                    variant: "outlined",
                    density: "comfortable",
                    "hide-details": ""
                  }, null, 8, ["modelValue", "onUpdate:modelValue"])
                ]),
                _: 2
              }, 1024),
              _createVNode(_component_v_col, {
                cols: "12",
                md: "1",
                class: "text-center"
              }, {
                default: _withCtx(() => [
                  _createVNode(_component_v_btn, {
                    icon: "",
                    color: "error",
                    variant: "text",
                    onClick: ($event) => removeRule(index)
                  }, {
                    default: _withCtx(() => [
                      _createVNode(_component_v_icon, null, {
                        default: _withCtx(() => [..._cache[2] || (_cache[2] = [
                          _createTextVNode("mdi-delete", -1)
                        ])]),
                        _: 1
                      })
                    ]),
                    _: 1
                  }, 8, ["onClick"])
                ]),
                _: 2
              }, 1024)
            ]),
            _: 2
          }, 1024);
        }), 128)),
        _createVNode(_component_v_btn, {
          color: "info",
          variant: "tonal",
          class: "mt-4 mb-6",
          "prepend-icon": "mdi-plus",
          onClick: addRule
        }, {
          default: _withCtx(() => [..._cache[3] || (_cache[3] = [
            _createTextVNode("添加一条新规则", -1)
          ])]),
          _: 1
        }),
        _createVNode(_component_v_divider, { class: "mb-4" }),
        _createElementVNode("div", _hoisted_2, [
          _createVNode(_component_v_btn, {
            variant: "outlined",
            onClick: notifyClose
          }, {
            default: _withCtx(() => [..._cache[4] || (_cache[4] = [
              _createTextVNode("取消", -1)
            ])]),
            _: 1
          }),
          _createVNode(_component_v_btn, {
            color: "primary",
            onClick: saveConfig
          }, {
            default: _withCtx(() => [..._cache[5] || (_cache[5] = [
              _createTextVNode("保存配置", -1)
            ])]),
            _: 1
          })
        ])
      ]);
    };
  }
});

const Config_vue_vue_type_style_index_0_scoped_46380f61_lang = '';

const _export_sfc = (sfc, props) => {
  const target = sfc.__vccOpts || sfc;
  for (const [key, val] of props) {
    target[key] = val;
  }
  return target;
};

const Config = /* @__PURE__ */ _export_sfc(_sfc_main, [["__scopeId", "data-v-46380f61"]]);

export { Config as default };
