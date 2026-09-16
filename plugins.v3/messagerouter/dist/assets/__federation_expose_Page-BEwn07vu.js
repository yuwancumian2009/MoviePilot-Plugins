import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import { _ as _export_sfc } from './_plugin-vue_export-helper-pcqpp-6-.js';

const {defineComponent:_defineComponent} = await importShared('vue');

const {resolveComponent:_resolveComponent,createVNode:_createVNode,createElementVNode:_createElementVNode,withCtx:_withCtx,toDisplayString:_toDisplayString,createTextVNode:_createTextVNode,openBlock:_openBlock,createElementBlock:_createElementBlock,createCommentVNode:_createCommentVNode,renderList:_renderList,Fragment:_Fragment,normalizeClass:_normalizeClass} = await importShared('vue');

const _hoisted_1 = { class: "mr-page" };
const _hoisted_2 = { class: "mr-topbar" };
const _hoisted_3 = { class: "mr-topbar__left" };
const _hoisted_4 = { class: "mr-topbar__icon" };
const _hoisted_5 = { class: "mr-topbar__right" };
const _hoisted_6 = { class: "mr-results" };
const _hoisted_7 = { class: "mr-result-card mr-result-card--status" };
const _hoisted_8 = { class: "mr-result-card__value" };
const _hoisted_9 = { class: "mr-result-card__unit" };
const _hoisted_10 = { class: "mr-result-card mr-result-card--rules" };
const _hoisted_11 = { class: "mr-result-card__value" };
const _hoisted_12 = { class: "mr-result-card__unit" };
const _hoisted_13 = { class: "mr-result-card mr-result-card--wechat" };
const _hoisted_14 = { class: "mr-result-card__value" };
const _hoisted_15 = { class: "mr-card" };
const _hoisted_16 = { class: "mr-card__header" };
const _hoisted_17 = { class: "mr-card__title d-flex align-center" };
const _hoisted_18 = { class: "mr-card__badge" };
const _hoisted_19 = {
  key: 0,
  class: "mr-empty-state"
};
const _hoisted_20 = {
  key: 1,
  class: "mr-app-list"
};
const _hoisted_21 = { class: "mr-app-list__name" };
const _hoisted_22 = { class: "mr-app-list__meta" };
const _hoisted_23 = { class: "mr-card mr-card--panel" };
const _hoisted_24 = { class: "mr-card__header" };
const _hoisted_25 = { class: "mr-card__title d-flex align-center" };
const _hoisted_26 = { class: "mr-card__badge" };
const _hoisted_27 = { class: "mr-table-wrap" };
const _hoisted_28 = { class: "mr-table" };
const _hoisted_29 = { key: 0 };
const _hoisted_30 = { class: "mr-table__plugin" };
const _hoisted_31 = { class: "mr-table__action" };
const _hoisted_32 = { class: "mr-card mr-card--panel" };
const _hoisted_33 = { class: "mr-card__header" };
const _hoisted_34 = { class: "mr-card__title d-flex align-center" };
const _hoisted_35 = { class: "mr-card__badge" };
const _hoisted_36 = { class: "mr-log-box" };
const _hoisted_37 = {
  key: 0,
  class: "mr-log-empty"
};
const {computed,onMounted,reactive} = await importShared('vue');

const _sfc_main = /* @__PURE__ */ _defineComponent({
  __name: "Page",
  props: {
    api: { type: Object, default: () => ({}) }
  },
  emits: ["action", "switch", "close"],
  setup(__props, { emit: __emit }) {
    const props = __props;
    const emit = __emit;
    const loading = reactive({ overview: false });
    const overview = reactive({
      enabled: false,
      block_system: false,
      rule_count: 0,
      wechat_app_count: 0,
      hook_count: 0,
      rules: [],
      logs: [],
      wechat_apps: {}
    });
    const appEntries = computed(() => Object.entries(overview.wechat_apps || {}));
    async function fetchOverview() {
      loading.overview = true;
      try {
        const res = await props.api.get("plugin/MessageRouter/overview");
        Object.assign(overview, res || {});
      } catch (e) {
        console.warn("fetchOverview error", e);
      } finally {
        loading.overview = false;
      }
    }
    onMounted(fetchOverview);
    return (_ctx, _cache) => {
      const _component_v_icon = _resolveComponent("v-icon");
      const _component_v_btn = _resolveComponent("v-btn");
      const _component_v_btn_group = _resolveComponent("v-btn-group");
      const _component_v_col = _resolveComponent("v-col");
      const _component_v_row = _resolveComponent("v-row");
      return _openBlock(), _createElementBlock("div", _hoisted_1, [
        _createElementVNode("div", _hoisted_2, [
          _createElementVNode("div", _hoisted_3, [
            _createElementVNode("div", _hoisted_4, [
              _createVNode(_component_v_icon, {
                icon: "mdi-transit-connection-variant",
                size: "24"
              })
            ]),
            _cache[2] || (_cache[2] = _createElementVNode("div", null, [
              _createElementVNode("div", { class: "mr-topbar__title" }, "插件消息重定向"),
              _createElementVNode("div", { class: "mr-topbar__sub" }, "实时规则、企微通道与拦截日志概览")
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
                  onClick: fetchOverview,
                  loading: loading.overview,
                  size: "small",
                  "min-width": "40",
                  class: "px-0 px-sm-3"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-refresh",
                      size: "18",
                      class: "mr-sm-1"
                    }),
                    _cache[3] || (_cache[3] = _createElementVNode("span", { class: "btn-text d-none d-sm-inline" }, "刷新", -1))
                  ]),
                  _: 1
                }, 8, ["loading"]),
                _createVNode(_component_v_btn, {
                  color: "primary",
                  onClick: _cache[0] || (_cache[0] = ($event) => emit("switch", "Config")),
                  size: "small",
                  "min-width": "40",
                  class: "px-0 px-sm-3"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-cog",
                      size: "18",
                      class: "mr-sm-1"
                    }),
                    _cache[4] || (_cache[4] = _createElementVNode("span", { class: "btn-text d-none d-sm-inline" }, "配置", -1))
                  ]),
                  _: 1
                }),
                _createVNode(_component_v_btn, {
                  color: "primary",
                  onClick: _cache[1] || (_cache[1] = ($event) => emit("close")),
                  size: "small",
                  "min-width": "40",
                  class: "px-0 px-sm-3"
                }, {
                  default: _withCtx(() => [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-close",
                      size: "18"
                    }),
                    _cache[5] || (_cache[5] = _createElementVNode("span", { class: "btn-text d-none d-sm-inline" }, "关闭", -1))
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
            _cache[6] || (_cache[6] = _createElementVNode("div", { class: "mr-result-card__label" }, "插件状态", -1)),
            _createElementVNode("div", _hoisted_8, _toDisplayString(overview.enabled ? "已启用" : "未启用"), 1),
            _createElementVNode("div", _hoisted_9, "阻断播报：" + _toDisplayString(overview.block_system ? "开启" : "关闭"), 1)
          ]),
          _createElementVNode("div", _hoisted_10, [
            _cache[7] || (_cache[7] = _createElementVNode("div", { class: "mr-result-card__label" }, "生效规则", -1)),
            _createElementVNode("div", _hoisted_11, _toDisplayString(overview.rule_count), 1),
            _createElementVNode("div", _hoisted_12, "已挂载 Hook：" + _toDisplayString(overview.hook_count) + " 个", 1)
          ]),
          _createElementVNode("div", _hoisted_13, [
            _cache[8] || (_cache[8] = _createElementVNode("div", { class: "mr-result-card__label" }, "企微通知通道", -1)),
            _createElementVNode("div", _hoisted_14, _toDisplayString(overview.wechat_app_count), 1),
            _cache[9] || (_cache[9] = _createElementVNode("div", { class: "mr-result-card__unit" }, "已读取系统微信配置", -1))
          ])
        ]),
        _createVNode(_component_v_row, { class: "mr-panel-row" }, {
          default: _withCtx(() => [
            _createVNode(_component_v_col, {
              cols: "12",
              md: "5",
              class: "mr-panel-col d-flex flex-column",
              style: { "gap": "16px" }
            }, {
              default: _withCtx(() => [
                _createElementVNode("div", _hoisted_15, [
                  _createElementVNode("div", _hoisted_16, [
                    _createElementVNode("span", _hoisted_17, [
                      _createVNode(_component_v_icon, {
                        icon: "mdi-wechat",
                        size: "18",
                        color: "#10b981",
                        class: "mr-1"
                      }),
                      _cache[10] || (_cache[10] = _createTextVNode(" 系统微信通知 ", -1))
                    ]),
                    _createElementVNode("span", _hoisted_18, _toDisplayString(appEntries.value.length) + " 个", 1)
                  ]),
                  !appEntries.value.length ? (_openBlock(), _createElementBlock("div", _hoisted_19, [
                    _createVNode(_component_v_icon, {
                      icon: "mdi-wechat",
                      size: "16",
                      color: "success",
                      class: "mr-1"
                    }),
                    _cache[11] || (_cache[11] = _createTextVNode(" 未获取到系统微信通知配置 ", -1))
                  ])) : (_openBlock(), _createElementBlock("div", _hoisted_20, [
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(appEntries.value, ([name, app]) => {
                      return _openBlock(), _createElementBlock("div", {
                        key: name,
                        class: "mr-app-list__item"
                      }, [
                        _createElementVNode("span", _hoisted_21, _toDisplayString(name), 1),
                        _createElementVNode("span", _hoisted_22, "AgentID: " + _toDisplayString(app.appid || "-"), 1)
                      ]);
                    }), 128))
                  ]))
                ]),
                _createElementVNode("div", _hoisted_23, [
                  _createElementVNode("div", _hoisted_24, [
                    _createElementVNode("span", _hoisted_25, [
                      _createVNode(_component_v_icon, {
                        icon: "mdi-shield-check",
                        size: "18",
                        color: "info",
                        class: "mr-1"
                      }),
                      _cache[12] || (_cache[12] = _createTextVNode(" 当前规则 ", -1))
                    ]),
                    _createElementVNode("span", _hoisted_26, _toDisplayString(overview.rule_count) + " 条", 1)
                  ]),
                  _createElementVNode("div", _hoisted_27, [
                    _createElementVNode("table", _hoisted_28, [
                      _cache[14] || (_cache[14] = _createElementVNode("thead", null, [
                        _createElementVNode("tr", null, [
                          _createElementVNode("th", null, "插件或关键字"),
                          _createElementVNode("th", { class: "mr-table__action" }, "动作说明")
                        ])
                      ], -1)),
                      _createElementVNode("tbody", null, [
                        !overview.rules.length ? (_openBlock(), _createElementBlock("tr", _hoisted_29, [..._cache[13] || (_cache[13] = [
                          _createElementVNode("td", {
                            colspan: "2",
                            class: "mr-empty-row"
                          }, "暂无规则", -1)
                        ])])) : _createCommentVNode("", true),
                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(overview.rules, (rule, index) => {
                          return _openBlock(), _createElementBlock("tr", {
                            key: `${rule.plugin}-${index}`,
                            class: _normalizeClass({ "mr-table__row--alt": index % 2 === 1 })
                          }, [
                            _createElementVNode("td", _hoisted_30, _toDisplayString(rule.plugin), 1),
                            _createElementVNode("td", _hoisted_31, _toDisplayString(rule.description), 1)
                          ], 2);
                        }), 128))
                      ])
                    ])
                  ])
                ])
              ]),
              _: 1
            }),
            _createVNode(_component_v_col, {
              cols: "12",
              md: "7",
              class: "mr-panel-col"
            }, {
              default: _withCtx(() => [
                _createElementVNode("div", _hoisted_32, [
                  _createElementVNode("div", _hoisted_33, [
                    _createElementVNode("span", _hoisted_34, [
                      _createVNode(_component_v_icon, {
                        icon: "mdi-console",
                        size: "18",
                        color: "#8b5cf6",
                        class: "mr-1"
                      }),
                      _cache[15] || (_cache[15] = _createTextVNode(" 实时路由监控日志 ", -1))
                    ]),
                    _createElementVNode("span", _hoisted_35, _toDisplayString(overview.logs.length) + " 条", 1)
                  ]),
                  _createElementVNode("div", _hoisted_36, [
                    !overview.logs.length ? (_openBlock(), _createElementBlock("div", _hoisted_37, "暂无日志")) : _createCommentVNode("", true),
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(overview.logs, (log, index) => {
                      return _openBlock(), _createElementBlock("div", {
                        key: index,
                        class: "mr-log-line"
                      }, _toDisplayString(log), 1);
                    }), 128))
                  ])
                ])
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

const Page = /* @__PURE__ */ _export_sfc(_sfc_main, [["__scopeId", "data-v-9ade1f41"]]);

export { Page as default };
