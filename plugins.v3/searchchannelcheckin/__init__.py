"""
搜索渠道签到

把「网盘订阅助手」里给搜索渠道签到的能力独立出来，只做签到，不搜资源、
不转存、不接管订阅。支持的渠道与签到模式与原插件一致：

- Dian115（普通 / 运气签到）
- HDHaven（普通 / 赌狗签到）
- HDHive（普通 / 赌狗签到，支持 WebAPI 账号密码与 OpenAPI 应用授权）
- 聚影（普通 / 幸运签到，签到后自动领奖池）
- 蜗牛（普通签到）

渠道客户端代码 vendor 自网盘订阅助手插件，保持原有登录、Turnstile 验证与
限速逻辑不变，因此签到成功率与行为跟原插件一致；本插件只是把编排、配置、
历史与页面独立出来，可单独启用、单独设置定时、单独看结果。

定时说明：cron 一天可触发多次，每次触发只补签「今天还没成功」的渠道，
已成功的渠道不会重复请求，所以默认给了一个每 2 小时一跑的 cron 当作自动重试。
"""
from __future__ import annotations

import datetime
import html as html_lib
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import pytz

from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas.types import NotificationType

from .vendor.search.dian115.client import Dian115Client, Dian115Error
from .vendor.search.hdhaven.client import HDHavenClient, HDHavenError
from .vendor.search.hdhive.open.client import HDHiveOpenAPIClient, HDHiveOpenAPIError
from .vendor.search.hdhive.web.client import HDHiveClient, HDHiveWebError
from .vendor.search.juying.client import JuyingClient, JuyingError
from .vendor.search.woniu.client import WoniuClient, WoniuError

#: 单渠道历史保留条数
HISTORY_LIMIT = 60
#: 页面历史展示条数
PAGE_HISTORY_LIMIT = 40
#: 默认 cron：8~22 点每 2 小时一次，靠「当日已成功则跳过」实现自动补签
DEFAULT_CRON = "0 8,10,12,14,16,18,20,22 * * *"

STATUS_SIGNED = "signed"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_DISABLED = "disabled"
STATUS_UNCONFIGURED = "unconfigured"

STATUS_TEXT = {
    STATUS_SIGNED: "今日已签到",
    STATUS_FAILED: "今日签到失败",
    STATUS_PENDING: "今日未签到",
    STATUS_DISABLED: "未启用",
    STATUS_UNCONFIGURED: "未配置账号",
}

STATUS_COLOR = {
    STATUS_SIGNED: "success",
    STATUS_FAILED: "error",
    STATUS_PENDING: "warning",
    STATUS_DISABLED: "default",
    STATUS_UNCONFIGURED: "default",
}

PAGE_CSS = """
<style>
.scc-card{border-radius:12px}
.scc-muted{opacity:.7;font-size:12px}
.scc-table{width:100%;border-collapse:collapse;font-size:13px}
.scc-table th{text-align:left;font-weight:600;padding:8px 10px;opacity:.7;
  border-bottom:1px solid rgba(128,128,128,.25);white-space:nowrap}
.scc-table td{padding:8px 10px;border-bottom:1px solid rgba(128,128,128,.12);
  vertical-align:top}
.scc-table tr:last-child td{border-bottom:none}
.scc-ok{color:#2e7d32;font-weight:600}
.scc-bad{color:#c62828;font-weight:600}
.scc-warn{color:#ef6c00;font-weight:600}
.scc-wrap{overflow-x:auto}
@media (max-width:600px){
  .scc-table th:nth-child(4),.scc-table td:nth-child(4){display:none}
}
</style>
"""


def _as_bool(value: Any, default: bool = False) -> bool:
    """把表单里可能是字符串的布尔值统一成 bool。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用"}


def _as_int(value: Any, default: int = 0, minimum: Optional[int] = None,
            maximum: Optional[int] = None) -> int:
    """把表单值转成整数并夹到区间内。"""
    try:
        result = int(float(str(value).strip()))
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_float(value: Any, default: float = 1.0, minimum: Optional[float] = None,
              maximum: Optional[float] = None) -> float:
    """把表单值转成浮点数并夹到区间内。"""
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_str(value: Any, default: str = "") -> str:
    """把表单值转成去空白的字符串。"""
    if value is None:
        return default
    return str(value).strip()


class SearchChannelCheckin(_PluginBase):
    """搜索渠道签到插件。"""

    # 插件元信息
    plugin_name = "搜索渠道签到"
    plugin_desc = "给 Dian115 / HDHaven / HDHive / 聚影 / 蜗牛 等搜索渠道每天自动签到。"
    plugin_icon = "mdi-calendar-check-outline"
    plugin_version = "2.0.3"
    plugin_author = "yuwancumian2009"
    author_url = "https://github.com/yuwancumian2009"
    plugin_config_prefix = "searchchannelcheckin_"
    plugin_order = 26
    auth_level = 1

    #: 渠道静态定义（表单与展示共用）
    CHANNELS: Dict[str, Dict[str, Any]] = {
        "dian115": {
            "name": "Dian115",
            "icon": "mdi-cloud-search",
            "hint": "运气签到可得 3～10 倍奖励，也有 21% 概率扣除 1 倍普通签到积分。",
            "modes": [("normal", "普通签到"), ("lucky", "运气签到")],
            "default_mode": "normal",
            "required": ["email", "password"],
            "fields": [
                {"key": "base_url", "label": "服务地址", "default": "https://m.dian115.com", "cols": 12},
                {"key": "email", "label": "登录邮箱", "cols": 6},
                {"key": "password", "label": "登录密码", "type": "password", "cols": 6},
                {"key": "lottery_enabled", "label": "启用幸运转盘", "type": "switch", "default": False, "cols": 4},
                {"key": "lottery_count", "label": "每日转盘目标次数", "type": "number", "default": 1,
                 "min": 1, "max": 20, "suffix": "次", "cols": 4},
            ],
        },
        "hdhaven": {
            "name": "HDHaven",
            "icon": "mdi-movie-open-star-outline",
            "hint": "赌狗签到有几率暴击获得多倍积分，也有几率扣除积分。",
            "modes": [("normal", "普通签到"), ("gambler", "赌狗签到")],
            "default_mode": "normal",
            "required": ["username", "password"],
            "fields": [
                {"key": "base_url", "label": "服务地址", "default": "https://hdhaven.com", "cols": 12},
                {"key": "username", "label": "用户名", "cols": 6},
                {"key": "password", "label": "密码", "type": "password", "cols": 6},
            ],
        },
        "hdhive": {
            "name": "HDHive",
            "icon": "mdi-hexagon-multiple-outline",
            "hint": "赌狗模式将奖励乘以 -1～3 的随机倍数，最坏扣除 3 积分。"
                    "WebAPI 用账号密码（走 Turnstile），OpenAPI 用应用 Secret + 用户授权。",
            "modes": [("normal", "普通签到"), ("gambler", "赌狗签到")],
            "default_mode": "normal",
            "required": ["query_mode"],
            "fields": [
                {"key": "base_url", "label": "站点地址", "default": "https://re0.me", "cols": 12},
                {"key": "query_mode", "label": "接入方式", "type": "select", "default": "web", "cols": 6,
                 "items": [{"title": "WebAPI（账号密码）", "value": "web"},
                           {"title": "OpenAPI（应用授权）", "value": "api"}]},
                {"key": "username", "label": "WebAPI 账号", "cols": 6},
                {"key": "password", "label": "WebAPI 密码", "type": "password", "cols": 6},
                {"key": "api_key", "label": "OpenAPI 应用 Secret", "cols": 6},
                {"key": "client_id", "label": "OpenAPI Client ID", "cols": 6},
                {"key": "access_token", "label": "OpenAPI Access Token", "cols": 6},
                {"key": "refresh_token", "label": "OpenAPI Refresh Token", "cols": 6},
                {"key": "token_expires_at", "label": "Token 过期时间戳", "type": "number", "default": 0, "cols": 6},
            ],
        },
        "juying": {
            "name": "聚影",
            "icon": "mdi-movie-check-outline",
            "hint": "每日二选一：最高 +50 / 最多 -5，签到后自动领奖池。",
            "modes": [("normal", "普通签到"), ("lucky", "幸运签到")],
            "default_mode": "normal",
            "required": ["username", "password"],
            "fields": [
                {"key": "base_url", "label": "服务地址", "default": "https://www.jying.top", "cols": 12},
                {"key": "username", "label": "网页登录账号", "cols": 6},
                {"key": "password", "label": "网页登录密码", "type": "password", "cols": 6},
            ],
        },
        "woniu": {
            "name": "蜗牛",
            "icon": "mdi-snail",
            "hint": "蜗牛（www.wn4k.com）每日签到打卡，仅普通签到。",
            "modes": [("normal", "普通签到")],
            "default_mode": "normal",
            "required": ["username", "password"],
            "fields": [
                {"key": "base_url", "label": "服务地址", "default": "https://www.wn4k.com", "cols": 12},
                {"key": "username", "label": "网页登录账号", "cols": 6},
                {"key": "password", "label": "网页登录密码", "type": "password", "cols": 6},
            ],
        },
    }

    def __init__(self):
        super().__init__()
        self._enabled: bool = False
        self._notify: bool = True
        self._cron: str = DEFAULT_CRON
        self._proxy: str = ""
        self._configs: Dict[str, Dict[str, Any]] = {}
        self._running: set = set()
        self._run_lock = threading.Lock()
        self._last_run_at: str = ""
        self._last_run_summary: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        """加载配置。任何字段缺失都退回默认值，保证插件不会因为半份配置起不来。"""
        config = config or {}
        # 兼容两种调用：启动时从 DB 读到的是扁平 dict；
        # 前端保存时 MoviePilot 传的是 {"config": {...}}。
        if isinstance(config, dict) and isinstance(config.get("config"), dict):
            config = config["config"]
        self._enabled = _as_bool(config.get("enabled"))
        self._notify = _as_bool(config.get("notify"), True)
        self._cron = _as_str(config.get("cron"), DEFAULT_CRON) or DEFAULT_CRON
        self._proxy = _as_str(config.get("proxy"), "") or self._default_proxy()
        self._last_run_at = _as_str(self.get_data("last_run_at"), "")
        self._last_run_summary = _as_str(self.get_data("last_run_summary"), "")

        configs: Dict[str, Dict[str, Any]] = {}
        for key, spec in self.CHANNELS.items():
            values: Dict[str, Any] = {
                "enabled": _as_bool(config.get(f"{key}_enabled"), False),
                "mode": _as_str(config.get(f"{key}_mode"), spec["default_mode"]) or spec["default_mode"],
            }
            for field in spec["fields"]:
                name = field["key"]
                raw = config.get(f"{key}_{name}")
                ftype = field.get("type")
                if ftype == "switch":
                    values[name] = _as_bool(raw, field.get("default", False))
                elif ftype == "number":
                    values[name] = _as_int(raw, field.get("default", 0),
                                           field.get("min"), field.get("max"))
                elif ftype == "select":
                    values[name] = _as_str(raw, field.get("default", "")) or field.get("default", "")
                else:
                    values[name] = _as_str(raw, field.get("default", ""))
            if values["mode"] not in [mode[0] for mode in spec["modes"]]:
                values["mode"] = spec["default_mode"]
            configs[key] = values
        self._configs = configs

    def get_state(self) -> bool:
        """插件开关状态（影响插件页是否显示已启用）。"""
        return self._enabled

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置表单：基础设置 + 每个渠道的开关、模式与账号。"""
        rows: List[dict] = []

        def row(cols: List[dict]) -> dict:
            return {"component": "VRow", "content": cols}

        def col(span: int, content: List[dict], md: int = 0) -> dict:
            props: Dict[str, Any] = {"cols": span}
            if md:
                props["md"] = md
            return {"component": "VCol", "props": props, "content": content}

        # ---- 基础设置 ----
        rows.append(row([
            col(12, [{
                "component": "VCardTitle",
                "props": {"class": "text-subtitle-1 font-weight-bold pb-0"},
                "text": "基础设置",
            }]),
        ]))
        rows.append(row([
            col(12, [{
                "component": "VSwitch",
                "props": {"model": "enabled", "label": "启用插件", "color": "primary"},
            }], md=4),
            col(12, [{
                "component": "VSwitch",
                "props": {"model": "notify", "label": "签到后发送通知", "color": "primary"},
            }], md=4),
            col(12, [{
                "component": "VTextField",
                "props": {
                    "model": "cron",
                    "label": "签到定时（cron）",
                    "placeholder": DEFAULT_CRON,
                    "hint": "一天可触发多次：每次只补签今天还没成功的渠道，已成功的不会重复请求，"
                            "所以默认每 2 小时一跑就等于自动重试。",
                    "persistent-hint": True,
                },
            }], md=4),
        ]))
        rows.append(row([
            col(12, [{
                "component": "VTextField",
                "props": {
                    "model": "proxy",
                    "label": "代理（可选）",
                    "placeholder": "http://192.168.1.2:7890",
                    "hint": "留空则使用 MoviePilot 系统代理设置。渠道站点需要代理才能访问时再填。",
                    "persistent-hint": True,
                },
            }]),
        ]))

        # ---- 各渠道 ----
        for key, spec in self.CHANNELS.items():
            rows.append({
                "component": "VDivider",
                "props": {"class": "my-3"},
            })
            rows.append(row([
                col(12, [{
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-1 font-weight-bold pb-0"},
                    "text": spec["name"],
                }], md=4),
                col(12, [{
                    "component": "VSwitch",
                    "props": {"model": f"{key}_enabled", "label": "启用签到", "color": "primary"},
                }], md=4),
                col(12, [{
                    "component": "VSelect",
                    "props": {
                        "model": f"{key}_mode",
                        "label": "签到模式",
                        "items": [{"title": label, "value": value} for value, label in spec["modes"]],
                        "density": "comfortable",
                    },
                }], md=4),
            ]))
            if spec.get("hint"):
                rows.append(row([
                    col(12, [{
                        "component": "div",
                        "props": {"class": "text-caption text-medium-emphasis mb-1"},
                        "html": html_lib.escape(spec["hint"]),
                    }]),
                ]))

            pending: List[dict] = []
            for field in spec["fields"]:
                model = f"{key}_{field['key']}"
                if field.get("type") == "switch":
                    pending.append(col(field.get("cols", 6), [{
                        "component": "VSwitch",
                        "props": {"model": model, "label": field["label"], "color": "primary"},
                    }]))
                    continue
                if field.get("type") == "select":
                    pending.append(col(field.get("cols", 6), [{
                        "component": "VSelect",
                        "props": {
                            "model": model,
                            "label": field["label"],
                            "items": field.get("items", []),
                            "density": "comfortable",
                        },
                    }]))
                    continue
                props: Dict[str, Any] = {
                    "model": model,
                    "label": field["label"],
                    "placeholder": field.get("default", "") if field.get("default") not in (None, "") else "",
                    "density": "comfortable",
                }
                if field.get("type") == "password":
                    props["type"] = "password"
                elif field.get("type") == "number":
                    props["type"] = "number"
                if field.get("suffix"):
                    props["suffix"] = field["suffix"]
                pending.append(col(field.get("cols", 6), [{
                    "component": "VTextField",
                    "props": props,
                }]))
            # 每行最多 12 栅格
            buffer: List[dict] = []
            used = 0
            for cell in pending:
                span = int(cell["props"].get("cols") or 12)
                if used + span > 12 and buffer:
                    rows.append(row(buffer))
                    buffer, used = [], 0
                buffer.append(cell)
                used += span
            if buffer:
                rows.append(row(buffer))

        default_config = {
            "enabled": False,
            "notify": True,
            "cron": DEFAULT_CRON,
            "proxy": "",
        }
        for key, spec in self.CHANNELS.items():
            default_config[f"{key}_enabled"] = False
            default_config[f"{key}_mode"] = spec["default_mode"]
            for field in spec["fields"]:
                default_config[f"{key}_{field['key']}"] = field.get("default", "")

        return [{
            "component": "VForm",
            "content": rows,
        }], default_config

    # ------------------------------------------------------------------
    # 定时服务
    # ------------------------------------------------------------------
    def get_service(self) -> List[Dict[str, Any]]:
        """注册签到定时任务：只要插件启用且有渠道启用就注册。"""
        if not self._enabled:
            return []
        if not any(cfg.get("enabled") for cfg in self._configs.values()):
            return []
        cron = self._cron or DEFAULT_CRON
        if not self._cron_is_valid(cron):
            logger.warning(f"搜索渠道签到：cron 表达式无效（{cron}），本次不注册定时任务")
            return []
        return [{
            "id": "SearchChannelCheckin",
            "name": "搜索渠道签到",
            "trigger": "cron",
            "func": self.scheduled_checkin,
            "kwargs": self._cron_kwargs(cron),
        }]

    def scheduled_checkin(self):
        """定时入口：只补签今天还没成功的渠道。"""
        if not self._enabled:
            return
        keys = [key for key, cfg in self._configs.items() if cfg.get("enabled")]
        if not keys:
            return
        logger.info(f"搜索渠道签到：定时任务开始，渠道={keys}")
        self._run_channels(keys, source="定时", skip_signed=True)

    @staticmethod
    def _cron_kwargs(expr: str) -> Dict[str, str]:
        """5 段式 cron 转成 CronTrigger 的字段（MP 直接把 kwargs 透传给触发器）。"""
        minute, hour, day, month, day_of_week = str(expr).split()
        return {
            "minute": minute,
            "hour": hour,
            "day": day,
            "month": month,
            "day_of_week": day_of_week,
        }

    @staticmethod
    def _cron_is_valid(expr: str) -> bool:
        """粗校验 cron：5 段式，值为数字/星号/区间/列表。"""
        parts = str(expr or "").split()
        if len(parts) != 5:
            return False
        allowed = set("0123456789*,-/")
        return all(part and set(part) <= allowed for part in parts)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """插件 API：页面按钮与外部脚本共用。"""
        return [
            {
                "path": "/run",
                "endpoint": self.api_run_all,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "全部签到",
                "description": "立即给所有已启用渠道签到（已在今天成功的也会重新执行）",
            },
            {
                "path": "/run/{channel}",
                "endpoint": self.api_run_one,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "单渠道签到",
                "description": "立即给指定渠道签到",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "签到状态",
                "description": "返回各渠道今日状态与最近历史",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "签到历史",
                "description": "返回指定渠道的历史记录",
            },
        ]

    def api_run_all(self) -> Dict[str, Any]:
        """API：全部渠道立即签到（异步执行，避免前端超时）。"""
        keys = [key for key, cfg in self._configs.items() if cfg.get("enabled")]
        if not keys:
            return {"success": False, "message": "没有已启用的签到渠道"}
        if not self._run_lock.acquire(blocking=False):
            return {"success": False, "message": "已有签到任务在跑，请稍后再试"}
        try:
            thread = threading.Thread(
                target=self._run_all_locked,
                args=(keys, "手动"),
                daemon=True,
                name="SearchChannelCheckin-all",
            )
            thread.start()
        except Exception:
            self._run_lock.release()
            raise
        return {"success": True, "message": f"已开始签到：{', '.join(keys)}"}

    def api_run_one(self, channel: str) -> Dict[str, Any]:
        """API：单渠道立即签到（同步执行，方便看结果）。"""
        key = _as_str(channel).lower()
        if key not in self.CHANNELS:
            return {"success": False, "message": f"未知渠道：{channel}"}
        if key in self._running:
            return {"success": False, "message": f"{self.CHANNELS[key]['name']} 正在签到中"}
        record = self._run_one(key, source="手动", skip_signed=False)
        return {"success": bool(record.get("success")), "message": record.get("message"), "data": record}

    def api_status(self) -> Dict[str, Any]:
        """API：概览 JSON，方便排查与外部监控。"""
        overview = self.overview()
        return {
            "success": True,
            "enabled": self._enabled,
            "cron": self._cron,
            "last_run_at": self._last_run_at,
            "last_run_summary": self._last_run_summary,
            "channels": overview,
        }

    def api_history(self, channel: Optional[str] = None, limit: int = PAGE_HISTORY_LIMIT) -> Dict[str, Any]:
        """API：历史记录。"""
        key = _as_str(channel).lower()
        if key and key not in self.CHANNELS:
            return {"success": False, "message": f"未知渠道：{channel}"}
        if key:
            records = self._history(key)
        else:
            records = self._all_history()
        records = sorted(records, key=lambda item: item.get("timestamp", ""), reverse=True)
        return {"success": True, "data": records[:max(1, min(int(limit or PAGE_HISTORY_LIMIT), 200))]}

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------
    def get_page(self) -> List[dict]:
        """插件页：今日状态卡片 + 立即签到按钮 + 历史记录。"""
        pid = self.__class__.__name__
        if not self._enabled:
            return [{
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "插件未启用，请先在插件配置中启用并保存。",
                },
            }]

        overview = self.overview()
        content: List[dict] = [{"component": "div", "html": PAGE_CSS}]

        signed = sum(1 for item in overview if item["status"] == STATUS_SIGNED)
        active = [item for item in overview if item["status"] not in (STATUS_DISABLED,)]
        head = f"今日已签到 {signed}/{len(active)} 个已启用渠道"
        if self._last_run_at:
            head += f"｜最近执行 {self._last_run_at}"
        if self._last_run_summary:
            head += f"｜{self._last_run_summary}"
        head_type = "success" if active and signed >= len(active) else "info"
        if any(item["status"] == STATUS_FAILED for item in overview):
            head_type = "warning"
        content.append({
            "component": "VAlert",
            "props": {"type": head_type, "variant": "tonal", "text": head, "class": "mb-3"},
        })

        def button(text: str, color: str, icon: str, path: str, variant: str = "tonal") -> dict:
            return {
                "component": "VBtn",
                "props": {
                    "color": color,
                    "variant": variant,
                    "size": "small",
                    "prependIcon": icon,
                    "class": "mr-2 mb-2",
                },
                "text": text,
                "events": {"click": {"api": f"/plugin/{pid}{path}", "method": "GET"}},
            }

        content.append({
            "component": "VRow",
            "props": {"class": "mb-1"},
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [button("全部签到", "primary", "mdi-calendar-check", "/run")],
            }],
        })

        cards: List[dict] = []
        for item in overview:
            key = item["key"]
            body: List[dict] = [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-1 d-flex align-center justify-space-between"},
                    "content": [
                        {
                            "component": "div",
                            "html": (
                                f"<span class='font-weight-bold'>{html_lib.escape(item['name'])}</span>"
                                f"<span class='ml-2 text-caption'>{html_lib.escape(item['mode_label'])}</span>"
                            ),
                        },
                        {
                            "component": "VChip",
                            "props": {
                                "size": "small",
                                "variant": "tonal",
                                "color": STATUS_COLOR.get(item["status"], "default"),
                            },
                            "text": STATUS_TEXT.get(item["status"], "-"),
                        },
                    ],
                },
            ]
            detail_rows: List[str] = []
            if item["status"] != STATUS_UNCONFIGURED:
                detail_rows.append(f"积分：{item['points'] if item['points'] is not None else '-'}")
            if item.get("last_time"):
                detail_rows.append(f"上次：{html_lib.escape(item['last_time'])}")
            if item.get("last_message"):
                detail_rows.append(f"结果：{html_lib.escape(item['last_message'])}")
            if item["status"] == STATUS_UNCONFIGURED:
                detail_rows.append(f"未配置：{html_lib.escape(item['hint'])}")
            body.append({
                "component": "VCardText",
                "props": {"class": "text-caption scc-muted"},
                "content": [{
                    "component": "div",
                    "html": "<br>".join(detail_rows) or "尚无记录",
                }],
            })
            if item["status"] not in (STATUS_DISABLED, STATUS_UNCONFIGURED):
                body.append({
                    "component": "VCardActions",
                    "props": {"class": "pt-0"},
                    "content": [button("签到", "primary", "mdi-play", f"/run/{key}", variant="text")],
                })
            cards.append({
                "component": "VCol",
                "props": {"cols": 12, "sm": 6, "md": 4},
                "content": [{
                    "component": "VCard",
                    "props": {"variant": "tonal", "class": "h-100 scc-card"},
                    "content": body,
                }],
            })
        content.append({"component": "VRow", "content": cards})

        # ---- 历史 ----
        records = sorted(self._all_history(), key=lambda item: item.get("timestamp", ""),
                         reverse=True)[:PAGE_HISTORY_LIMIT]
        content.append({
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mt-4 scc-card"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-1"},
                    "text": f"签到历史（最近 {len(records)} 条）",
                },
                {
                    "component": "VCardText",
                    "content": [{"component": "div", "html": self._history_html(records)}],
                },
            ],
        })
        return content

    def _history_html(self, records: List[Dict[str, Any]]) -> str:
        """历史记录表格（移动端隐藏说明列）。"""
        if not records:
            return "<div class='scc-muted'>还没有签到记录。</div>"
        head = ("<tr><th>时间</th><th>渠道</th><th>模式</th>"
                "<th>结果</th><th>积分</th><th>说明</th></tr>")
        rows: List[str] = []
        for record in records:
            success = bool(record.get("success"))
            already = bool(record.get("already_checked_in"))
            cls = "scc-ok" if success and not already else ("scc-warn" if already else "scc-bad")
            status = html_lib.escape(str(record.get("status") or ("成功" if success else "失败")))
            if success and already:
                status = "今日已签到"
            change = record.get("points_change")
            if change in (None, ""):
                points = "-"
            else:
                try:
                    delta = int(change)
                    points = f"{delta:+d}" if delta else "0"
                except (TypeError, ValueError):
                    points = html_lib.escape(str(change))
            rows.append(
                "<tr>"
                f"<td>{html_lib.escape(str(record.get('time') or ''))}</td>"
                f"<td>{html_lib.escape(str(record.get('channel_name') or record.get('channel') or ''))}</td>"
                f"<td>{html_lib.escape(str(record.get('mode_label') or record.get('mode') or ''))}</td>"
                f"<td class='{cls}'>{status}</td>"
                f"<td>{points}</td>"
                f"<td>{html_lib.escape(str(record.get('message') or ''))}</td>"
                "</tr>"
            )
        return f"<div class='scc-wrap'><table class='scc-table'>{head}{''.join(rows)}</table></div>"

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def _run_all_locked(self, keys: List[str], source: str):
        """持锁执行全量签到，结束后释放锁并发通知。"""
        try:
            self._run_channels(keys, source=source, skip_signed=False)
        except Exception as error:
            logger.error(f"搜索渠道签到：全量执行异常：{error} - {traceback.format_exc()}")
        finally:
            self._run_lock.release()

    def _run_channels(self, keys: List[str], source: str = "定时",
                      skip_signed: bool = True) -> List[Dict[str, Any]]:
        """按渠道顺序执行签到，单个渠道失败不影响其它渠道。"""
        results: List[Dict[str, Any]] = []
        for key in keys:
            if skip_signed and self._today_status(key) == STATUS_SIGNED:
                logger.info(f"搜索渠道签到：{self.CHANNELS[key]['name']} 今日已签到，跳过")
                continue
            results.append(self._run_one(key, source=source, skip_signed=skip_signed))
        if results:
            self._finish_run(results, source)
        return results

    def _run_one(self, key: str, source: str = "手动",
                 skip_signed: bool = False) -> Dict[str, Any]:
        """执行单个渠道签到并落历史。"""
        spec = self.CHANNELS[key]
        cfg = self._configs.get(key, {})
        mode = cfg.get("mode") or spec["default_mode"]
        mode_label = dict(spec["modes"]).get(mode, mode)
        started = time.time()

        if key in self._running:
            return {"success": False, "status": "跳过",
                    "message": f"{spec['name']} 正在执行，已跳过"}
        if not self._channel_ready(key):
            record = self._build_record(
                key, mode, mode_label, started,
                {"success": False, "status": "未配置", "message": self._missing_hint(key)},
                source=source,
            )
            self._append_history(key, record)
            return record
        if skip_signed and self._today_status(key) == STATUS_SIGNED:
            return {"success": True, "status": "今日已签到", "message": "已跳过"}

        self._running.add(key)
        client = None
        try:
            client = self._build_client(key)
            if client is None:
                raise RuntimeError(self._missing_hint(key))
            logger.info(f"搜索渠道签到：开始 {spec['name']}（{mode_label}）")
            result = client.checkin(mode)
            record = self._build_record(key, mode, mode_label, started, result, source=source)
            logger.info(
                f"搜索渠道签到：{spec['name']} 完成，success={record['success']}，"
                f"message={record['message']}"
            )
        except Exception as error:
            message = str(error) or type(error).__name__
            logger.warning(f"搜索渠道签到：{spec['name']} 失败：{error}")
            record = self._build_record(
                key, mode, mode_label, started,
                {"success": False, "status": "签到失败", "message": message},
                source=source,
            )
        finally:
            self._running.discard(key)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._append_history(key, record)
        return record

    def _build_record(self, key: str, mode: str, mode_label: str, started: float,
                      result: Dict[str, Any], source: str) -> Dict[str, Any]:
        """把各渠道返回值统一成一条历史记录。"""
        now = self._now()
        result = result or {}
        success = _as_bool(result.get("success"), False)
        status_text = _as_str(result.get("status"), "")
        message = _as_str(result.get("message"), "")
        already = _as_bool(result.get("already_checked_in"), False)
        if not already and success and ("已签到" in status_text or "已签到" in message):
            already = True

        points_before = result.get("points_before")
        points_after = result.get("points_after")
        change = result.get("points_change")
        if change in (None, "") and points_before is not None and points_after is not None:
            try:
                change = int(points_after) - int(points_before)
            except (TypeError, ValueError):
                change = None
        if change in (None, ""):
            change = result.get("signin_points")

        return {
            "channel": key,
            "channel_name": self.CHANNELS[key]["name"],
            "mode": mode,
            "mode_label": mode_label,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%m-%d %H:%M:%S"),
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
            "success": success,
            "already_checked_in": already,
            "status": status_text or ("签到成功" if success else "签到失败"),
            "message": message,
            "points_change": change,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": result.get("signin_days"),
            "error_code": _as_str(result.get("error_code"), ""),
            "duration": round(time.time() - started, 1),
        }

    def _finish_run(self, results: List[Dict[str, Any]], source: str):
        """记录本次执行摘要并按配置发通知。"""
        self._last_run_at = self._now().strftime("%Y-%m-%d %H:%M:%S")
        ok = sum(1 for item in results if item.get("success") and not item.get("already_checked_in"))
        already = sum(1 for item in results if item.get("already_checked_in"))
        failed = [item for item in results if not item.get("success")]
        self._last_run_summary = (
            f"成功 {ok}｜已签 {already}｜失败 {len(failed)}"
        )
        self.save_data("last_run_at", self._last_run_at)
        self.save_data("last_run_summary", self._last_run_summary)
        if self._notify:
            self._notify_results(results, source)

    def _notify_results(self, results: List[Dict[str, Any]], source: str):
        """发送签到结果通知，失败项单独列出。"""
        if not results:
            return
        lines = [f"【搜索渠道签到】{source} {self._last_run_at}"]
        for item in results:
            if item.get("already_checked_in"):
                mark = "✅"
                tail = "今日已签到"
            elif item.get("success"):
                mark = "✅"
                tail = item.get("message") or "签到成功"
            else:
                mark = "❌"
                tail = item.get("message") or "签到失败"
            change = item.get("points_change")
            try:
                change_text = f"（{int(change):+d}）" if change not in (None, "") else ""
            except (TypeError, ValueError):
                change_text = ""
            lines.append(f"{mark} {item.get('channel_name')}：{tail}{change_text}")
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="搜索渠道签到",
                text="\n".join(lines),
            )
        except Exception as error:
            logger.warning(f"搜索渠道签到：发送通知失败：{error}")

    # ------------------------------------------------------------------
    # 渠道客户端
    # ------------------------------------------------------------------
    def _build_client(self, key: str) -> Any:
        """按渠道构造客户端；凭据不全时返回 None。"""
        cfg = self._configs.get(key, {})
        proxy = self._proxy or None
        common = {
            "proxy": proxy,
            "get_data_func": self.get_data,
            "save_data_func": self.save_data,
        }
        if key == "dian115":
            if not cfg.get("email") or not cfg.get("password"):
                return None
            return Dian115Client(
                email=cfg["email"],
                password=cfg["password"],
                base_url=cfg.get("base_url") or "https://m.dian115.com",
                proxy=proxy,
                request_interval=_as_float(cfg.get("request_interval"), 1.0, 0.2, 10),
                unlocks_per_minute=6,
                timeout=_as_int(cfg.get("timeout"), 60, 5, 120),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
                lottery_enabled=_as_bool(cfg.get("lottery_enabled"), False),
                lottery_count=_as_int(cfg.get("lottery_count"), 0, 0, 20),
            )
        if key == "hdhaven":
            if not cfg.get("username") or not cfg.get("password"):
                return None
            return HDHavenClient(
                username=cfg["username"],
                password=cfg["password"],
                base_url=cfg.get("base_url") or "https://hdhaven.com",
                request_interval=2.0,
                **common,
            )
        if key == "hdhive":
            if _as_str(cfg.get("query_mode"), "web") == "api":
                token = self.get_data("hdhive_token") or {}
                api_key = cfg.get("api_key")
                client_id = cfg.get("client_id")
                if not api_key or not client_id:
                    return None
                return HDHiveOpenAPIClient(
                    app_secret=api_key,
                    client_id=client_id,
                    access_token=_as_str(token.get("access_token")) or _as_str(cfg.get("access_token")),
                    refresh_token=_as_str(token.get("refresh_token")) or _as_str(cfg.get("refresh_token")),
                    token_expires_at=_as_float(token.get("token_expires_at"), 0),
                    base_url=cfg.get("base_url") or "https://re0.me",
                    proxy=proxy,
                    timeout=_as_int(cfg.get("timeout"), 60, 5, 120),
                    request_interval=5.0,
                    on_token_update=lambda data: self.save_data("hdhive_token", data),
                )
            if not cfg.get("username") or not cfg.get("password"):
                return None
            return HDHiveClient(
                username=cfg["username"],
                password=cfg["password"],
                proxy=proxy,
                request_interval=5.0,
                timeout=_as_int(cfg.get("timeout"), 60, 5, 120),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
        if key == "juying":
            if not cfg.get("username") or not cfg.get("password"):
                return None
            return JuyingClient(
                username=cfg["username"],
                password=cfg["password"],
                base_url=cfg.get("base_url") or "https://www.jying.top",
                request_timeout=_as_int(cfg.get("timeout"), 60, 5, 120),
                request_interval=1.0,
                **common,
            )
        if key == "woniu":
            if not cfg.get("username") or not cfg.get("password"):
                return None
            return WoniuClient(
                username=cfg["username"],
                password=cfg["password"],
                base_url=cfg.get("base_url") or "https://www.wn4k.com",
                request_timeout=_as_int(cfg.get("timeout"), 60, 5, 120),
                request_interval=1.0,
                **common,
            )
        return None

    def _channel_ready(self, key: str) -> bool:
        """渠道凭据是否齐全（与客户端构造条件一致）。"""
        cfg = self._configs.get(key, {})
        if key == "hdhive":
            if _as_str(cfg.get("query_mode"), "web") == "api":
                return bool(cfg.get("api_key") and cfg.get("client_id"))
            return bool(cfg.get("username") and cfg.get("password"))
        spec = self.CHANNELS[key]
        return all(bool(cfg.get(name)) for name in spec["required"] if name != "query_mode")

    def _missing_hint(self, key: str) -> str:
        """未配置时给用户看的提示。"""
        cfg = self._configs.get(key, {})
        if key == "hdhive" and _as_str(cfg.get("query_mode"), "web") == "api":
            return "请填写 OpenAPI 应用 Secret 与 Client ID"
        spec = self.CHANNELS[key]
        labels = {field["key"]: field["label"] for field in spec["fields"]}
        missing = [labels.get(name, name) for name in spec["required"]
                   if name != "query_mode" and not cfg.get(name)]
        return "请填写" + "、".join(missing) if missing else "请先补全账号配置"

    # ------------------------------------------------------------------
    # 状态与历史
    # ------------------------------------------------------------------
    def overview(self) -> List[Dict[str, Any]]:
        """各渠道今日状态，供页面与 API 使用。"""
        items: List[Dict[str, Any]] = []
        for key, spec in self.CHANNELS.items():
            cfg = self._configs.get(key, {})
            history = self._history(key)
            today = self._now().strftime("%Y-%m-%d")
            today_records = [item for item in history if item.get("date") == today]
            last = history[-1] if history else None
            if not cfg.get("enabled"):
                status = STATUS_DISABLED
            elif not self._channel_ready(key):
                status = STATUS_UNCONFIGURED
            elif any(item.get("success") for item in today_records):
                status = STATUS_SIGNED
            elif today_records:
                status = STATUS_FAILED
            else:
                status = STATUS_PENDING
            mode = cfg.get("mode") or spec["default_mode"]
            mode_label = dict(spec["modes"]).get(mode, mode)
            points = None
            for record in reversed(history):
                if record.get("points_after") is not None:
                    points = record.get("points_after")
                    break
            items.append({
                "key": key,
                "name": spec["name"],
                "enabled": bool(cfg.get("enabled")),
                "ready": self._channel_ready(key),
                "mode": mode,
                "mode_label": mode_label,
                "status": status,
                "points": points,
                "hint": self._missing_hint(key) if status == STATUS_UNCONFIGURED else "",
                "last_time": (last or {}).get("time", ""),
                "last_message": (last or {}).get("message", ""),
                "today_records": len(today_records),
            })
        return items

    def _today_status(self, key: str) -> str:
        """单渠道今日状态。"""
        for item in self.overview():
            if item["key"] == key:
                return item["status"]
        return STATUS_PENDING

    def _history(self, key: str) -> List[Dict[str, Any]]:
        """读取单渠道历史。"""
        stored = self.get_data(f"history_{key}")
        if not isinstance(stored, list):
            return []
        return [item for item in stored if isinstance(item, dict)][-HISTORY_LIMIT:]

    def _all_history(self) -> List[Dict[str, Any]]:
        """合并全部渠道历史。"""
        records: List[Dict[str, Any]] = []
        for key in self.CHANNELS:
            records.extend(self._history(key))
        return records

    def _append_history(self, key: str, record: Dict[str, Any]):
        """写入历史（单渠道最多保留 HISTORY_LIMIT 条）。"""
        history = self._history(key)
        history.append(record)
        self.save_data(f"history_{key}", history[-HISTORY_LIMIT:])

    def stop_service(self):
        """停用插件时释放运行标记。"""
        self._running.clear()
        try:
            super().stop_service()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> datetime.datetime:
        """带时区的当前时间。"""
        try:
            return datetime.datetime.now(pytz.timezone(settings.TZ))
        except Exception:
            return datetime.datetime.now()

    @staticmethod
    def _default_proxy() -> str:
        """MP 系统代理设置转成字符串；没配就是空。"""
        host = getattr(settings, "PROXY_HOST", "") or ""
        if not host:
            return ""
        port = getattr(settings, "PROXY_PORT", None)
        if port and ":" not in str(host):
            return f"http://{host}:{port}"
        return str(host)
