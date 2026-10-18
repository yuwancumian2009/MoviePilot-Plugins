import hashlib
import html as html_lib
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger

from app.helper.cookiecloud import CookieCloudHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

# 同一时间只允许一次检测（定时任务与页面按钮可能并发）
_run_lock = threading.Lock()

DEFAULT_CRON = "0 */6 * * *"
DEFAULT_TARGETS = "DoubanFolio,DoubanDoulist"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 用于保活的固定条目（肖申克的救赎），只读请求
KEEPALIVE_SUBJECT = "1292052"

PAGE_CSS = """
<style>
.dck{color:rgba(var(--v-theme-on-surface),.92);font-size:13px}
.dck-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:4px 0 14px}
@media(max-width:700px){.dck-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.dck-card{border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));
  border-radius:10px;padding:10px 12px;background:rgb(var(--v-theme-surface));min-width:0}
.dck-k{font-size:11px;letter-spacing:.04em;color:rgba(var(--v-theme-on-surface),.72);margin-bottom:4px}
.dck-v{font-size:15px;font-weight:600;line-height:1.35;word-break:break-all}
.dck-sec{margin:18px 0 8px;font-size:13px;font-weight:700}
.dck-scroll{overflow-x:auto;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:10px}
table.dck-tbl{width:100%;border-collapse:collapse;font-size:12.5px;min-width:560px}
table.dck-tbl th{text-align:left;padding:8px 10px;font-weight:600;white-space:nowrap;
  background:rgba(var(--v-theme-on-surface),.06)}
table.dck-tbl td{padding:8px 10px;vertical-align:top;word-break:break-all;
  border-top:1px solid rgba(var(--v-border-color),calc(var(--v-border-opacity)*.6))}
table.dck-tbl tr:nth-child(even) td{background:rgba(var(--v-theme-on-surface),.02)}
.dck-ok{color:rgb(var(--v-theme-success));font-weight:600}
.dck-bad{color:rgb(var(--v-theme-error));font-weight:600}
.dck-mut{color:rgba(var(--v-theme-on-surface),.72)}
.dck-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
.dck-tag{display:inline-block;padding:0 6px;border-radius:6px;font-size:11px;line-height:18px;
  background:rgba(var(--v-theme-on-surface),.08);margin:0 6px 4px 0}
</style>
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _esc(text: Any) -> str:
    return html_lib.escape(str(text if text is not None else ""), quote=True)


class DoubanCookieKeeper(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣Cookie保活"
    # 插件描述
    plugin_desc = "自动从 CookieCloud 拉取豆瓣 Cookie，检测作废/失效，低频保活并同步给豆瓣相关插件，失效即时通知"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xijin285/MoviePilot-Plugins/refs/heads/main/icons/douban.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "yuwancumian2009"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubancookiekeeper_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    # 配置
    _enabled: bool = False
    _notify: bool = True
    _use_cookiecloud: bool = True
    _keepalive: bool = True
    _auto_reload: bool = True
    _manual_cookie: str = ""
    _cron: str = DEFAULT_CRON
    _targets: str = DEFAULT_TARGETS
    _alert_interval: int = 6
    _onlyonce: bool = False

    # 运行态
    _running: bool = False
    _running_kind: str = ""
    _started_at: str = ""
    _last_cc_msg: str = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config: Dict[str, Any] = {}

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: dict = None):
        self._stop_service()
        cfg = config or {}
        # 前端保存时可能包一层 {"config": {...}}
        if isinstance(cfg, dict) and isinstance(cfg.get("config"), dict):
            cfg = cfg["config"]
        self._config = dict(cfg or {})

        self._enabled = bool(cfg.get("enabled", False))
        self._notify = bool(cfg.get("notify", True))
        self._use_cookiecloud = bool(cfg.get("use_cookiecloud", True))
        self._keepalive = bool(cfg.get("keepalive", True))
        self._auto_reload = bool(cfg.get("auto_reload", True))
        self._manual_cookie = str(cfg.get("cookie") or "").strip()
        self._cron = str(cfg.get("cron") or DEFAULT_CRON).strip()
        self._targets = str(cfg.get("sync_targets") or DEFAULT_TARGETS).strip()
        try:
            self._alert_interval = max(1, int(cfg.get("alert_interval") or 6))
        except (TypeError, ValueError):
            self._alert_interval = 6

        if self._cron:
            try:
                CronTrigger.from_crontab(self._cron)
            except Exception as err:
                logger.warn(f"豆瓣Cookie保活：定时表达式无效（{self._cron}），回退默认值：{err}")
                self._cron = DEFAULT_CRON

        self._onlyonce = bool(cfg.get("onlyonce", False))
        if self._onlyonce:
            self._config["onlyonce"] = False
            self.update_config(self._config)
            logger.info("豆瓣Cookie保活：保存后立即检测一次")
            threading.Timer(3, lambda: self._spawn("run")).start()

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        self._stop_service()

    def _stop_service(self):
        # 本插件不使用 APScheduler 实例，任务由 get_service 交给 MP 调度器托管
        pass

    # ------------------------------------------------------------------ 调度
    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [{
            "id": "DoubanCookieKeeper",
            "name": "豆瓣Cookie保活检查",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self._scheduled_run,
            "kwargs": {},
        }]

    def _scheduled_run(self):
        self._run_check(kind="cron")

    # ------------------------------------------------------------------ 表单/页面
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def row(cols: List[dict]) -> dict:
            return {"component": "VRow", "content": cols}

        def col(span: int, content: List[dict], md: int = 0) -> dict:
            props: Dict[str, Any] = {"cols": span}
            if md:
                props["md"] = md
            return {"component": "VCol", "props": props, "content": content}

        title_props = {"class": "text-subtitle-1 font-weight-bold pb-0"}

        rows: List[dict] = [
            row([col(12, [{"component": "VCardTitle", "props": title_props, "text": "基础设置"}])]),
            row([
                col(12, [{"component": "VSwitch",
                         "props": {"model": "enabled", "label": "启用插件", "color": "primary"}}], md=3),
                col(12, [{"component": "VSwitch",
                         "props": {"model": "notify", "label": "失效/恢复时通知", "color": "primary"}}], md=3),
                col(12, [{"component": "VSwitch",
                         "props": {"model": "use_cookiecloud", "label": "允许从 CookieCloud 拉取",
                                   "color": "primary", "hint": "浏览器装 CookieCloud 扩展后，豆瓣 Cookie 会自动同步到这里",
                                   "persistent-hint": True}}], md=3),
                col(12, [{"component": "VSwitch",
                         "props": {"model": "keepalive", "label": "低频保活请求", "color": "primary",
                                   "hint": "每轮访问豆瓣首页与一个条目页，维持活跃并捕获续期 Cookie",
                                   "persistent-hint": True}}], md=3),
                col(12, [{"component": "VSwitch",
                         "props": {"model": "auto_reload", "label": "同步后重载目标插件", "color": "primary",
                                   "hint": "让目标插件立即使用新 Cookie（不重载则要等 MP 重启）",
                                   "persistent-hint": True}}], md=3),
            ]),
            row([
                col(12, [{"component": "VTextField",
                         "props": {"model": "cron", "label": "检测/保活定时（cron）",
                                   "placeholder": DEFAULT_CRON,
                                   "hint": "默认每 6 小时一次。不要低于每小时一次，频繁访问容易触发豆瓣风控。",
                                   "persistent-hint": True}}], md=4),
                col(12, [{"component": "VTextField",
                         "props": {"model": "sync_targets", "label": "同步目标插件（逗号分隔）",
                                   "placeholder": DEFAULT_TARGETS,
                                   "hint": "这些插件的配置里若有 cookie 字段，会被写入同一份有效 Cookie",
                                   "persistent-hint": True}}], md=4),
                col(12, [{"component": "VTextField",
                         "props": {"model": "alert_interval", "label": "失效重复提醒间隔（小时）",
                                   "type": "number", "min": 1}}], md=4),
            ]),
            row([col(12, [{"component": "VCardTitle", "props": title_props, "text": "手动 Cookie（可选）"}])]),
            row([
                col(12, [{"component": "VTextarea",
                         "props": {"model": "cookie", "label": "豆瓣 Cookie",
                                   "rows": 3, "auto-grow": True, "placeholder":
                                   "dbcl2=...; bid=...; ck=...; frodotk_db=...",
                                   "hint": "留空则只用 CookieCloud / 已缓存的 Cookie。粘贴后保存即可，"
                                           "插件会先校验再同步，失效的 Cookie 不会被写进其它插件。",
                                   "persistent-hint": True}}]),
            ]),
            row([
                col(12, [{"component": "VSwitch",
                         "props": {"model": "onlyonce", "label": "保存后立即检测一次", "color": "primary"}}], md=4),
            ]),
        ]

        defaults = {
            "enabled": self._enabled,
            "notify": self._notify,
            "use_cookiecloud": self._use_cookiecloud,
            "keepalive": self._keepalive,
            "auto_reload": self._auto_reload,
            "cookie": self._manual_cookie,
            "cron": self._cron,
            "sync_targets": self._targets,
            "alert_interval": self._alert_interval,
            "onlyonce": False,
        }
        return rows, defaults

    def get_page(self) -> List[dict]:
        if not self._enabled:
            return [{
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal",
                          "text": "插件未启用，请先在插件配置中启用并保存。"},
            }]

        pid = self.__class__.__name__
        state = self.get_data("state") or {}
        cookie = self.get_data("cookie") or ""
        if not isinstance(state, dict):
            state = {}
        history = state.get("history") or []
        targets = state.get("targets") or {}
        valid = state.get("valid")
        checked_at = state.get("checked_at") or "尚未检测"

        content: List[dict] = [{"component": "div", "html": PAGE_CSS}]

        if self._running:
            content.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mb-3",
                          "text": f"正在执行：{self._running_kind or '检测'}（开始于 {self._started_at}），完成后刷新本页查看结果。"},
            })

        if valid is True:
            head_type, head = "success", f"豆瓣 Cookie 有效｜来源：{self._source_label(state.get('source'))}｜最近检测：{checked_at}"
        elif valid is False:
            head_type = "error"
            head = f"豆瓣 Cookie 已失效｜{state.get('reason') or '原因未知'}｜最近检测：{checked_at}"
        else:
            head_type, head = "info", "尚未检测。点击下方「立即检测」或等待定时任务运行。"
        content.append({"component": "VAlert",
                        "props": {"type": head_type, "variant": "tonal", "text": head, "class": "mb-3"}})

        def button(text: str, color: str, icon: str, path: str) -> dict:
            return {
                "component": "VBtn",
                "props": {"color": color, "variant": "tonal", "size": "small",
                          "prependIcon": icon, "class": "mr-2 mb-2"},
                "text": text,
                "events": {"click": {"api": f"/plugin/{pid}{path}", "method": "GET"}},
            }

        content.append({"component": "VRow", "props": {"class": "mb-2"}, "content": [{
            "component": "VCol", "props": {"cols": 12}, "content": [[
                button("立即检测并同步", "primary", "mdi-shield-check", "/check"),
                button("仅从 CookieCloud 刷新", "secondary", "mdi-cloud-download", "/pull"),
                button("重新同步到目标插件", "info", "mdi-sync", "/sync"),
            ]],
        }]})

        uid = state.get("uid") or ""
        nickname = state.get("nickname") or ""
        fp = state.get("cookie_fp") or ""
        cc_msg = state.get("cc_msg") or self._last_cc_msg or "—"
        cards = [
            ("豆瓣账号", (nickname + ("（" + uid + "）" if uid else "")) if nickname else (uid or "—")),
            ("Cookie 来源", self._source_label(state.get("source"))),
            ("Cookie 指纹", fp or "—"),
            ("Cookie 长度", str(state.get("cookie_len") or (len(cookie) if cookie else 0))),
            ("最近检测", checked_at),
            ("CookieCloud", cc_msg),
            ("目标插件", f"{len([1 for v in targets.values() if v.get('synced')])}/{len(targets) or 0} 已同步"),
            ("豆瓣信号", state.get("signal") or "—"),
        ]
        card_html = "".join(
            f'<div class="dck-card"><div class="dck-k">{_esc(k)}</div><div class="dck-v">{_esc(v)}</div></div>'
            for k, v in cards
        )
        content.append({"component": "div", "html": f'<div class="dck"><div class="dck-cards">{card_html}</div></div>'})

        # 目标插件表
        if targets:
            rows_html = []
            for name, item in targets.items():
                status = ('<span class="dck-ok">已同步</span>' if item.get("synced")
                          else f'<span class="dck-bad">未同步</span>')
                reloaded = "已重载" if item.get("reloaded") else "—"
                rows_html.append(
                    "<tr>"
                    f"<td class='dck-mono'>{_esc(name)}</td>"
                    f"<td>{status}</td>"
                    f"<td>{_esc(reloaded)}</td>"
                    f"<td>{_esc(item.get('note') or '')}</td>"
                    f"<td class='dck-mut'>{_esc(item.get('at') or '')}</td>"
                    "</tr>"
                )
            content.append({"component": "div",
                            "html": '<div class="dck"><div class="dck-sec">目标插件同步状态</div>'
                                    '<div class="dck-scroll"><table class="dck-tbl"><thead><tr>'
                                    '<th>插件</th><th>状态</th><th>重载</th><th>说明</th><th>时间</th>'
                                    '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table></div></div>'})

        # 历史表
        if history:
            rows_html = []
            for item in list(reversed(history))[:30]:
                ok = item.get("valid")
                badge = ('<span class="dck-ok">有效</span>' if ok
                         else '<span class="dck-bad">失效</span>')
                rows_html.append(
                    "<tr>"
                    f"<td class='dck-mut'>{_esc(item.get('at'))}</td>"
                    f"<td>{badge}</td>"
                    f"<td>{_esc(self._source_label(item.get('source')))}</td>"
                    f"<td>{_esc(item.get('reason') or '')}</td>"
                    f"<td class='dck-mono'>{_esc(item.get('uid') or '')}</td>"
                    "</tr>"
                )
            content.append({"component": "div",
                            "html": '<div class="dck"><div class="dck-sec">最近检测记录</div>'
                                    '<div class="dck-scroll"><table class="dck-tbl"><thead><tr>'
                                    '<th>时间</th><th>结果</th><th>来源</th><th>说明</th><th>UID</th>'
                                    '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table></div></div>'})

        # 说明
        content.append({
            "component": "VAlert",
            "props": {"type": "info", "variant": "tonal", "class": "mt-4",
                      "text": "更新 Cookie 的正确姿势：在电脑浏览器登录 douban.com 并通过验证 → "
                              "CookieCloud 扩展自动上传 → 本插件下一轮（或点「仅从 CookieCloud 刷新」）自动取用，"
                              "并同步给片单/豆列等插件。浏览器登录过期后，服务端会把 dbcl2 置为 deleted，"
                              "此时任何保活请求都救不回来，必须重新登录一次。"},
        })

        # 最近一次探测细节
        details = state.get("details") or {}
        if details:
            content.append({
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mt-2",
                          "text": "最近一次探测：" + "；".join(f"{k}={v}" for k, v in details.items())},
            })

        return content

    # ------------------------------------------------------------------ API
    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "auth": "bear", "summary": "豆瓣 Cookie 状态"},
            {"path": "/check", "endpoint": self.api_check, "methods": ["GET", "POST"],
             "auth": "bear", "summary": "立即检测并同步"},
            {"path": "/pull", "endpoint": self.api_pull, "methods": ["GET", "POST"],
             "auth": "bear", "summary": "从 CookieCloud 刷新"},
            {"path": "/sync", "endpoint": self.api_sync, "methods": ["GET", "POST"],
             "auth": "bear", "summary": "重新同步到目标插件"},
        ]

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def api_status(self) -> Dict[str, Any]:
        state = self.get_data("state") or {}
        return {"success": True, "data": {
            "running": self._running,
            "valid": state.get("valid"),
            "checked_at": state.get("checked_at"),
            "source": state.get("source"),
            "uid": state.get("uid"),
            "cookie_fp": state.get("cookie_fp"),
            "reason": state.get("reason"),
            "targets": state.get("targets") or {},
            "version": self.plugin_version,
            "cron": self._cron,
        }}

    def api_check(self) -> Dict[str, Any]:
        return self._spawn("检测并同步", kind="run")

    def api_pull(self) -> Dict[str, Any]:
        return self._spawn("从 CookieCloud 刷新", kind="pull")

    def api_sync(self) -> Dict[str, Any]:
        return self._spawn("重新同步到目标插件", kind="sync")

    # ------------------------------------------------------------------ 执行
    def _spawn(self, label: str, kind: str) -> Dict[str, Any]:
        if self._running:
            return {"success": True, "message": f"已有任务在运行：{self._running_kind}"}
        self._running = True
        self._running_kind = label
        self._started_at = _now()
        threading.Thread(target=self._run_check, kwargs={"kind": kind}, daemon=True).start()
        return {"success": True, "message": f"已开始{label}，稍后刷新页面查看结果"}

    def _run_check(self, kind: str = "run"):
        if not _run_lock.acquire(blocking=False):
            self._running = False
            logger.info("豆瓣Cookie保活：上一轮尚未结束，本次跳过")
            return
        try:
            self._running = True
            if not self._running_kind:
                self._running_kind = {"pull": "从 CookieCloud 刷新", "sync": "同步"}.get(kind, "检测并同步")
                self._started_at = _now()
            self._do_run(kind=kind)
        except Exception as err:
            logger.error(f"豆瓣Cookie保活：执行异常 {type(err).__name__}: {err}")
        finally:
            self._running = False
            self._running_kind = ""
            _run_lock.release()

    def _do_run(self, kind: str = "run"):
        state = self.get_data("state") or {}
        if not isinstance(state, dict):
            state = {}

        # 只做同步：复用已验证的缓存
        if kind == "sync":
            cached = self.get_data("cookie") or ""
            if not cached:
                logger.warn("豆瓣Cookie保活：没有可用 Cookie，无法同步")
                return
            targets = self._sync_targets(cached)
            state["targets"] = targets
            state["checked_at"] = _now()
            state["history"] = self._push_history(state, {
                "at": _now(), "valid": state.get("valid"), "source": state.get("source") or "cache",
                "reason": "手动重新同步", "uid": state.get("uid") or "",
            })
            self.save_data("state", state)
            return

        candidates = self._candidates(force_pull=(kind == "pull"))
        results: List[Dict[str, Any]] = []
        best: Optional[Dict[str, Any]] = None
        for source, ck in candidates:
            res = self._probe(ck)
            res["source"] = source
            results.append(res)
            logger.info(f"豆瓣Cookie保活：验证 {source} → {res.get('reason')}")
            if res.get("valid"):
                best = res
                break

        prev_valid = state.get("valid")
        now = _now()

        if best:
            cookie = best.get("cookie") or ""
            changed = self._fp(cookie) != (state.get("cookie_fp") or "")
            self.save_data("cookie", cookie)
            targets = state.get("targets") or {}
            if changed or not targets:
                targets = self._sync_targets(cookie)

            merged = dict(state)
            merged.update({
                "valid": True,
                "checked_at": now,
                "source": best.get("source"),
                "uid": best.get("uid") or "",
                "nickname": best.get("nickname") or "",
                "cookie_fp": self._fp(cookie),
                "cookie_len": len(cookie),
                "reason": best.get("reason") or "登录态有效",
                "signal": best.get("signal") or "",
                "details": best.get("details") or {},
                "cc_msg": self._last_cc_msg or merged.get("cc_msg") or "",
                "targets": targets,
            })
            merged["history"] = self._push_history(state, {
                "at": now, "valid": True, "source": best.get("source"),
                "reason": ("Cookie 已更新" if changed else "Cookie 未变化"),
                "uid": best.get("uid") or "",
            })
            self.save_data("state", merged)

            if self._notify and prev_valid is False:
                self._notify_user(
                    title="豆瓣 Cookie 已恢复",
                    text=(f"来源：{self._source_label(best.get('source'))}"
                          f"{('｜UID：' + best.get('uid')) if best.get('uid') else ''}\n"
                          f"已同步目标插件：{', '.join([k for k, v in targets.items() if v.get('synced')]) or '无'}\n"
                          f"Cookie 指纹：{self._fp(cookie)}"),
                )
            logger.info(f"豆瓣Cookie保活：Cookie 有效（来源 {best.get('source')}，changed={changed}）")
            return

        primary = results[0] if results else {
            "reason": "没有可用的 Cookie（未配置手动 Cookie，CookieCloud 也没有豆瓣数据）",
            "source": "none", "uid": "", "details": {},
        }
        merged = dict(state)
        merged.update({
            "valid": False,
            "checked_at": now,
            "source": primary.get("source"),
            "uid": primary.get("uid") or state.get("uid") or "",
            "reason": primary.get("reason") or "Cookie 无效",
            "signal": primary.get("signal") or "",
            "details": primary.get("details") or {},
            "cc_msg": self._last_cc_msg or merged.get("cc_msg") or "",
        })
        merged["history"] = self._push_history(state, {
            "at": now, "valid": False, "source": primary.get("source"),
            "reason": merged["reason"], "uid": merged.get("uid") or "",
        })
        self.save_data("state", merged)

        if self._notify and self._should_alert(state, prev_valid):
            self._notify_user(
                title="豆瓣 Cookie 已失效",
                text=(f"原因：{merged['reason']}\n"
                      f"来源：{self._source_label(primary.get('source'))}"
                      f"{('（UID ' + str(merged.get('uid')) + '）') if merged.get('uid') else ''}\n"
                      "处理：电脑浏览器重新登录 douban.com 并通过验证，等待 CookieCloud 扩展上传；"
                      "本插件下一轮会自动取用并同步。也可在插件配置里粘贴新 Cookie。"),
            )
        logger.warn(f"豆瓣Cookie保活：Cookie 无效 - {merged['reason']}")

    # ------------------------------------------------------------------ Cookie 来源
    def _candidates(self, force_pull: bool = False) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        self._last_cc_msg = ""
        # 显式要求从 CookieCloud 刷新时，把它排在手动/缓存之前
        if force_pull and self._use_cookiecloud:
            cc, msg = self._cookiecloud_cookie(force=True)
            if cc:
                out.append(("cookiecloud", cc))
            self._last_cc_msg = msg
        if self._manual_cookie:
            out.append(("manual", self._manual_cookie))
        if self._use_cookiecloud and not force_pull:
            cc, msg = self._cookiecloud_cookie(force=False)
            if cc:
                out.append(("cookiecloud", cc))
            self._last_cc_msg = msg
        cached = self.get_data("cookie") or ""
        if isinstance(cached, str) and cached.strip():
            out.append(("cache", cached.strip()))

        seen, uniq = set(), []
        for src, ck in out:
            fp = self._fp(ck)
            if fp in seen:
                continue
            seen.add(fp)
            uniq.append((src, ck))
        return uniq

    def _cookiecloud_cookie(self, force: bool = False) -> Tuple[str, str]:
        try:
            data, msg = CookieCloudHelper().download()
        except Exception as err:
            return "", f"拉取失败：{type(err).__name__}"
        if not data:
            return "", msg or "未获取到数据"
        for key in ("douban.com", ".douban.com", "www.douban.com", "movie.douban.com"):
            val = data.get(key)
            if not val:
                continue
            cookie = self._normalize_cookie(val)
            if cookie:
                return cookie, f"已取到 {key}（{len(cookie)} 字符）"
        return "", "CookieCloud 中没有 douban.com"

    @staticmethod
    def _normalize_cookie(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            parts = []
            for k, v in value.items():
                if isinstance(v, dict):
                    v = v.get("value")
                if v is None:
                    continue
                parts.append(f"{k}={v}")
            return "; ".join(parts)
        if isinstance(value, list):
            return "; ".join(f"{i.get('name')}={i.get('value')}" for i in value
                             if isinstance(i, dict) and i.get("name"))
        return ""

    @staticmethod
    def _parse_cookie(cookie: str) -> Dict[str, str]:
        pairs: Dict[str, str] = {}
        for part in (cookie or "").split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            if not k:
                continue
            pairs[k] = v.strip().strip('"')
        return pairs

    @staticmethod
    def _cookie_str(pairs: Dict[str, str]) -> str:
        return "; ".join(f"{k}={v}" for k, v in pairs.items() if v)

    @staticmethod
    def _fp(cookie: str) -> str:
        if not cookie:
            return ""
        pairs = DoubanCookieKeeper._parse_cookie(cookie)
        raw = ";".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _source_label(source: Any) -> str:
        return {"manual": "手动配置", "cookiecloud": "CookieCloud", "cache": "本地缓存",
                "none": "无"}.get(str(source or ""), str(source or "—"))

    # ------------------------------------------------------------------ 探测
    def _probe(self, cookie: str) -> Dict[str, Any]:
        pairs = self._parse_cookie(cookie)
        uid = ""
        m = re.match(r"(\d+):", str(pairs.get("dbcl2") or ""))
        if m:
            uid = m.group(1)

        result: Dict[str, Any] = {
            "valid": False, "reason": "", "uid": uid, "nickname": "",
            "cookie": cookie, "signal": "", "details": {},
        }
        if not pairs:
            result["reason"] = "Cookie 为空或格式不可解析"
            return result
        if not pairs.get("dbcl2"):
            result["reason"] = "Cookie 缺少 dbcl2，不是登录态"
            return result
        if str(pairs.get("dbcl2")).lower().startswith("deleted"):
            result["reason"] = "豆瓣已作废登录态（dbcl2=deleted）"
            return result

        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        details: Dict[str, Any] = {}
        deleted_keys: List[str] = []

        # 1) 首页：豆瓣在会话失效时会下发 dbcl2="deleted" / ck="deleted"
        try:
            s = requests.Session()
            s.headers.update(headers)
            s.cookies.update(pairs)
            home = s.get("https://www.douban.com/", timeout=20, allow_redirects=False)
            sc_list = home.raw.headers.getlist("Set-Cookie")
            for sc in sc_list:
                name = sc.split("=", 1)[0].strip()
                if re.match(r'^\s*"?deleted', sc.split("=", 1)[1] if "=" in sc else ""):
                    deleted_keys.append(name)
            pairs = self._merge_setcookies(pairs, sc_list)
            details["首页"] = f"HTTP {home.status_code}"
        except Exception as err:
            result["reason"] = f"访问豆瓣首页失败：{type(err).__name__}"
            result["details"] = details
            return result

        if "dbcl2" in deleted_keys:
            result.update(reason="豆瓣服务端已作废登录态（首页下发 dbcl2=deleted）",
                          details=details, signal="dbcl2=deleted")
            return result

        # 2) 「我的豆瓣」：未登录/风控时 403，登录态通常 302 到个人主页
        mine_status, mine_loc = 0, ""
        try:
            s2 = requests.Session()
            s2.headers.update(headers)
            s2.cookies.update(pairs)
            mine = s2.get("https://www.douban.com/mine/", timeout=20, allow_redirects=False)
            mine_status = mine.status_code
            mine_loc = mine.headers.get("Location") or ""
            pairs = self._merge_setcookies(pairs, mine.raw.headers.getlist("Set-Cookie"))
            details["我的"] = f"HTTP {mine_status}{(' → ' + mine_loc) if mine_loc else ''}"
        except Exception as err:
            details["我的"] = f"失败 {type(err).__name__}"

        result["cookie"] = self._cookie_str(pairs)

        if mine_status == 403:
            result.update(reason="访问「我的豆瓣」返回 403：登录态已失效或被豆瓣风控",
                          details=details, signal="mine=403")
            return result
        loc_low = (mine_loc or "").lower()
        if mine_status in (301, 302, 303, 307) and any(
                k in loc_low for k in ("sec.douban.com", "accounts.douban.com", "passport", "login")):
            result.update(reason="豆瓣把「我的豆瓣」重定向到登录/验证页，登录态已失效",
                          details=details, signal=f"mine={mine_status}→verify")
            return result
        if mine_status == 0:
            result.update(reason="无法确认登录态（「我的豆瓣」请求失败）",
                          details=details, signal="mine=error")
            return result

        # 3) 有效时顺带取昵称（失败不影响判定）
        nickname = ""
        if uid:
            try:
                s3 = requests.Session()
                s3.headers.update(headers)
                s3.cookies.update(pairs)
                people = s3.get(f"https://www.douban.com/people/{uid}/", timeout=20, allow_redirects=False)
                t = re.search(r"<title>(.*?)</title>", people.text or "", re.S)
                if t:
                    nickname = re.sub(r"\s+", " ", t.group(1)).strip()[:40]
                details["个人页"] = f"HTTP {people.status_code}"
            except Exception:
                pass

        # 4) 可选：低频保活请求，捕获豆瓣续期下发的 Cookie
        if self._keepalive:
            try:
                s4 = requests.Session()
                s4.headers.update(headers)
                s4.cookies.update(pairs)
                sub = s4.get(f"https://movie.douban.com/subject/{KEEPALIVE_SUBJECT}/",
                             timeout=20, allow_redirects=False)
                pairs = self._merge_setcookies(pairs, sub.raw.headers.getlist("Set-Cookie"))
                details["保活条目"] = f"HTTP {sub.status_code}"
            except Exception as err:
                details["保活条目"] = f"失败 {type(err).__name__}"

        result.update({
            "valid": True,
            "reason": "登录态有效",
            "nickname": nickname,
            "cookie": self._cookie_str(pairs),
            "signal": f"home=200, mine={mine_status}",
            "details": details,
        })
        return result

    @staticmethod
    def _merge_setcookies(pairs: Dict[str, str], sc_list: List[str]) -> Dict[str, str]:
        merged = dict(pairs)
        for sc in sc_list or []:
            if "=" not in sc:
                continue
            name, rest = sc.split("=", 1)
            name = name.strip()
            value = rest.split(";", 1)[0].strip().strip('"')
            if not name or name.lower() in ("path", "domain", "expires", "max-age"):
                continue
            if value.lower() == "deleted" or "max-age=0" in sc.replace(" ", "").lower():
                merged.pop(name, None)
                continue
            merged[name] = value
        return merged

    # ------------------------------------------------------------------ 同步
    def _target_list(self) -> List[str]:
        return [x.strip() for x in re.split(r"[,，\s]+", self._targets or "") if x.strip()]

    def _sync_targets(self, cookie: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not cookie:
            return out
        pids = self._target_list()
        if not pids:
            return out
        for pid in pids:
            item: Dict[str, Any] = {"synced": False, "reloaded": False, "at": _now(), "note": ""}
            try:
                key = f"plugin.{pid}"
                cfg = self.systemconfig.get(key)
                if not isinstance(cfg, dict):
                    item["note"] = "未安装或没有配置"
                    out[pid] = item
                    continue
                if "cookie" not in cfg:
                    item["note"] = "该插件没有 cookie 配置项"
                    out[pid] = item
                    continue
                if str(cfg.get("cookie") or "").strip() == cookie.strip():
                    item.update(synced=True, note="已是同一份 Cookie")
                    out[pid] = item
                    continue
                new_cfg = dict(cfg)
                new_cfg["cookie"] = cookie
                self.systemconfig.set(key, new_cfg)
                item.update(synced=True, note="已写入配置")
                if self._auto_reload:
                    try:
                        from app.api.endpoints.plugin import register_plugin
                        from app.core.plugin import PluginManager
                        PluginManager().reload_plugin(pid)
                        register_plugin(pid)
                        item.update(reloaded=True, note="已写入配置并重载")
                    except Exception as err:
                        item["note"] = f"已写入配置，重载失败：{type(err).__name__}"
                logger.info(f"豆瓣Cookie保活：同步 Cookie 到 {pid}（{item['note']}）")
            except Exception as err:
                item["note"] = f"失败：{type(err).__name__}: {err}"
                logger.error(f"豆瓣Cookie保活：同步 {pid} 失败 {err}")
            out[pid] = item
        return out

    # ------------------------------------------------------------------ 通知/历史
    def _should_alert(self, state: Dict[str, Any], prev_valid: Any) -> bool:
        if prev_valid is not False:
            return True
        last = state.get("last_alert_at")
        if not last:
            return True
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return True
        return datetime.now() - last_dt >= timedelta(hours=self._alert_interval)

    def _notify_user(self, title: str, text: str):
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:
            logger.error(f"豆瓣Cookie保活：发送通知失败 {err}")
        # 记录提醒时间（invalid 时用于限频）
        try:
            state = self.get_data("state") or {}
            if isinstance(state, dict):
                state["last_alert_at"] = _now()
                state["last_alert_title"] = title
                self.save_data("state", state)
        except Exception:
            pass

    @staticmethod
    def _push_history(state: Dict[str, Any], item: Dict[str, Any]) -> List[Dict[str, Any]]:
        hist = state.get("history") or []
        if not isinstance(hist, list):
            hist = []
        hist = (hist + [item])[-100:]
        return hist
