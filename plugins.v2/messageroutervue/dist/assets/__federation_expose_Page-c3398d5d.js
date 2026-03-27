import { importShared } from './__federation_fn_import-054b33c3.js';

const {defineComponent:_defineComponent} = await importShared('vue');

const {createElementVNode:_createElementVNode,createTextVNode:_createTextVNode,resolveComponent:_resolveComponent,withCtx:_withCtx,createVNode:_createVNode,openBlock:_openBlock,createElementBlock:_createElementBlock,renderList:_renderList,Fragment:_Fragment,toDisplayString:_toDisplayString} = await importShared('vue');

const _hoisted_1 = { class: "pa-4" };
const _hoisted_2 = { class: "d-flex justify-space-between align-center mb-4" };
const _hoisted_3 = { key: 0 };
const _hoisted_4 = { key: 0 };
const {ref,onMounted} = await importShared('vue');

const _sfc_main = /* @__PURE__ */ _defineComponent({
  __name: "Page",
  props: {
    api: { type: Object, default: () => ({}) }
  },
  emits: ["action", "switch", "close"],
  setup(__props, { emit: __emit }) {
    const props = __props;
    const emit = __emit;
    const currentRules = ref({});
    const logs = ref([]);
    onMounted(async () => {
      try {
        const res = await props.api.get("plugin/MessageRouterVue/logs");
        if (res && res.success) {
          currentRules.value = res.data.rules || {};
          logs.value = res.data.logs || [];
        }
      } catch (error) {
        console.error("获取日志失败", error);
      }
    });
    return (_ctx, _cache) => {
      const _component_v_btn = _resolveComponent("v-btn");
      const _component_v_card_title = _resolveComponent("v-card-title");
      const _component_v_card_text = _resolveComponent("v-card-text");
      const _component_v_card = _resolveComponent("v-card");
      return _openBlock(), _createElementBlock("div", _hoisted_1, [
        _createElementVNode("div", _hoisted_2, [
          _cache[2] || (_cache[2] = _createElementVNode("div", { class: "text-h6" }, "运行状态监控", -1)),
          _createVNode(_component_v_btn, {
            color: "primary",
            variant: "outlined",
            onClick: _cache[0] || (_cache[0] = ($event) => emit("switch"))
          }, {
            default: _withCtx(() => [..._cache[1] || (_cache[1] = [
              _createTextVNode("前往修改配置", -1)
            ])]),
            _: 1
          })
        ]),
        _createVNode(_component_v_card, {
          class: "mb-4",
          variant: "outlined"
        }, {
          default: _withCtx(() => [
            _createVNode(_component_v_card_title, { class: "text-subtitle-1 font-weight-bold text-success" }, {
              default: _withCtx(() => [..._cache[3] || (_cache[3] = [
                _createTextVNode("【当前生效规则】", -1)
              ])]),
              _: 1
            }),
            _createVNode(_component_v_card_text, null, {
              default: _withCtx(() => [
                Object.keys(currentRules.value).length === 0 ? (_openBlock(), _createElementBlock("div", _hoisted_3, "暂无生效的路由规则")) : (_openBlock(true), _createElementBlock(_Fragment, { key: 1 }, _renderList(currentRules.value, (target, plugin) => {
                  return _openBlock(), _createElementBlock("div", {
                    key: plugin,
                    class: "mb-1"
                  }, " • 拦截插件：[" + _toDisplayString(plugin) + "] ➔ 路由目标：[" + _toDisplayString(target) + "] ", 1);
                }), 128))
              ]),
              _: 1
            })
          ]),
          _: 1
        }),
        _createVNode(_component_v_card, { variant: "outlined" }, {
          default: _withCtx(() => [
            _createVNode(_component_v_card_title, { class: "text-subtitle-1 font-weight-bold" }, {
              default: _withCtx(() => [..._cache[4] || (_cache[4] = [
                _createTextVNode("【实时监控日志 (最近50条)】", -1)
              ])]),
              _: 1
            }),
            _createVNode(_component_v_card_text, {
              class: "bg-grey-lighten-4 pa-3 rounded",
              style: { "font-family": "monospace", "max-height": "400px", "overflow-y": "auto" }
            }, {
              default: _withCtx(() => [
                logs.value.length === 0 ? (_openBlock(), _createElementBlock("div", _hoisted_4, "暂无日志，请尝试触发其他插件的消息通知。")) : (_openBlock(true), _createElementBlock(_Fragment, { key: 1 }, _renderList(logs.value, (log, idx) => {
                  return _openBlock(), _createElementBlock("div", {
                    key: idx,
                    class: "text-body-2 mb-1"
                  }, _toDisplayString(log), 1);
                }), 128))
              ]),
              _: 1
            })
          ]),
          _: 1
        })
      ]);
    };
  }
});

export { _sfc_main as default };
