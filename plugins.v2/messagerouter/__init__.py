import json
import sys
import time
import requests
import importlib
import asyncio
from typing import Any, List, Dict, Tuple, Optional

from app.log import logger
from app.plugins import _PluginBase

# 兼容各种版本的导包路径
try:
    from app.schemas import NotificationType
except ImportError:
    try:
        from app.schemas.types import NotificationType
    except ImportError:
        NotificationType = None

try:
    from app.schemas.types import SystemConfigKey
except ImportError:
    SystemConfigKey = None

try:
    from app.db.systemconfig_oper import SystemConfigOper
except ImportError:
    SystemConfigOper = None

try:
    from app.core.event import eventmanager
except ImportError:
    eventmanager = None

try:
    from app.core.plugin import PluginManager
except ImportError:
    PluginManager = None


class MessageRouter(_PluginBase):
    # 插件名称
    plugin_name = "Vue-插件消息重定向"
    # 插件描述
    plugin_desc = "可深度接管 MoviePilot 底层的通知中心与事件枢纽。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/yuwancumian2009/MoviePilot-Plugins/main/icons/messagerouter.png"
    # 插件版本
    plugin_version = "2.2.2"
    # 插件作者
    plugin_author = "yuwancumian,KoWming"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "messagerouter_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 配置与状态
    _enabled: bool = False
    _block_system: bool = False
    _plugin_mapping_str: str = ""

    # 路由、缓存与日志
    _plugin_routes: Dict[str, Dict[str, str]] = {}
    _tokens_cache: Dict[str, Dict[str, Any]] = {}
    _intercept_logs: List[str] = []

    # 系统通知配置缓存
    _apps_profile_cache: Dict[str, Dict[str, Any]] = {}
    _apps_profile_last_update: int = 0

    # 企业微信接口
    _send_msg_url = "%s/cgi-bin/message/send?access_token=%s"
    _token_url = "%s/cgi-bin/gettoken?corpid=%s&corpsecret=%s"

    # 运行期 Hook 与消息去重缓存
    _active_hooks: List[Tuple[Any, str, str]] = []
    _pushed_msg_cache: Dict[int, float] = {}

    def __init__(self):
        super().__init__()

    @staticmethod
    def _to_bool(value: Any) -> bool:
        """将各种输入值转换为布尔值"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _parse_plugin_routes(self, mapping_text: str) -> Dict[str, Dict[str, str]]:
        """解析路由规则文本为结构化映射"""
        routes: Dict[str, Dict[str, str]] = {}
        for line in str(mapping_text or "").split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = [part.strip() for part in line.split(':')]
            if len(parts) >= 2 and parts[0]:
                routes[parts[0]] = {
                    "type": parts[1],
                    "app": parts[2] if len(parts) > 2 else ""
                }
        return routes

    def _serialize_plugin_routes(self, rules: List[Dict[str, Any]]) -> str:
        """将结构化规则序列化为兼容旧版的文本配置"""
        lines: List[str] = []
        for rule in rules or []:
            plugin = str((rule or {}).get("plugin") or "").strip()
            msg_type = str((rule or {}).get("type") or "").strip()
            app = str((rule or {}).get("app") or "").strip()
            if not plugin:
                continue
            lines.append(f"{plugin}:{msg_type}:{app}")
        return "\n".join(lines)

    def _normalize_route_rules(self, payload: Any) -> List[Dict[str, str]]:
        """标准化前端提交的路由规则"""
        normalized: List[Dict[str, str]] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            plugin = str(item.get("plugin") or item.get("plugin_id") or "").strip()
            msg_type = str(item.get("type") or "").strip()
            app = str(item.get("app") or "").strip()
            if not plugin:
                continue
            normalized.append({
                "plugin": plugin,
                "type": msg_type,
                "app": app
            })
        return normalized

    def _get_route_rules(self) -> List[Dict[str, str]]:
        """获取当前结构化规则列表"""
        rules: List[Dict[str, str]] = []
        for plugin, route in (self._plugin_routes or {}).items():
            rules.append({
                "plugin": str(plugin or ""),
                "type": str((route or {}).get("type") or ""),
                "app": str((route or {}).get("app") or "")
            })
        return rules

    def _get_plugin_options(self) -> List[Dict[str, str]]:
        """获取可选插件列表"""
        options: List[Dict[str, str]] = []
        seen = set()
        try:
            if not PluginManager:
                return options
            manager = PluginManager()
            plugin_ids = []
            if hasattr(manager, "get_running_plugin_ids"):
                plugin_ids = manager.get_running_plugin_ids() or []
            elif hasattr(manager, "get_plugin_ids"):
                plugin_ids = manager.get_plugin_ids() or []

            for pid in plugin_ids:
                pid_str = str(pid or "").strip()
                if not pid_str or pid_str.lower() in seen or pid_str == self.__class__.__name__:
                    continue
                plugin_name = pid_str
                if hasattr(manager, "get_plugin_attr"):
                    plugin_name = manager.get_plugin_attr(pid_str, "plugin_name") or pid_str
                seen.add(pid_str.lower())
                options.append({
                    "title": str(plugin_name),
                    "value": str(plugin_name),
                    "name": str(plugin_name),
                    "id": pid_str
                })
        except Exception as e:
            logger.warn(f"{self.plugin_name}: 获取插件列表失败: {e}")
        return sorted(options, key=lambda item: str(item.get("title") or item.get("value") or "").lower())

    def _get_notification_type_options(self) -> List[Dict[str, str]]:
        """获取可选消息类型列表"""
        options = [{"title": "不修改", "value": ""}]
        for label in ["资源下载", "整理入库", "订阅", "站点", "媒体服务器", "手动处理", "插件", "其它"]:
            options.append({"title": label, "value": label})
        return options

    def _get_wechat_app_options(self) -> List[Dict[str, str]]:
        """获取系统企业微信通知通道列表"""
        return [
            {"title": name, "value": name}
            for name in sorted((self._get_system_wechat_apps() or {}).keys())
        ]

    def _build_overview(self) -> Dict[str, Any]:
        """构建前端概览页面所需数据"""
        self._apps_profile_last_update = 0
        current_apps = self._get_system_wechat_apps()
        rules = []
        for plugin, route in self._plugin_routes.items():
            t_type = route.get("type", "")
            t_app = route.get("app", "")
            desc = []
            if t_type and t_type not in ["", "原类型", "不修改"]:
                desc.append(f"改类型 ➔ [{t_type}]")
            if t_app:
                if t_app in current_apps:
                    desc.append(f"企微直推 ➔ [{t_app}]")
                else:
                    desc.append(f"❌ 企微 [{t_app}] 未在系统中找到")
            if not desc:
                desc.append("无动作")
            rules.append({
                "plugin": plugin,
                "type": t_type,
                "app": t_app,
                "description": " | ".join(desc)
            })

        return {
            "enabled": self._enabled,
            "block_system": self._block_system,
            "plugin_mapping": str(self._plugin_mapping_str or ""),
            "rule_count": len(rules),
            "rules": rules,
            "wechat_apps": current_apps,
            "wechat_app_count": len(current_apps),
            "logs": list(getattr(self, '_intercept_logs', []) or []),
            "hook_count": len(getattr(self, '_active_hooks', []) or []),
        }

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """初始化插件并加载配置，同时挂载消息拦截能力"""
        self.stop_service()
        self._intercept_logs = []
        self._plugin_routes = {}
        self._apps_profile_cache = {}
        self._apps_profile_last_update = 0
        self._active_hooks = []
        self._pushed_msg_cache = {}
        
        self._tokens_cache = self.get_data("wecom_tokens") or {}

        if config:
            self._enabled = self._to_bool(config.get("enabled", False))
            self._block_system = self._to_bool(config.get("block_system", False))
            self._plugin_mapping_str = config.get("plugin_mapping", "")

        self._plugin_routes = self._parse_plugin_routes(self._plugin_mapping_str)

        self._type_map = {}
        if NotificationType:
            self._type_map = {
                "资源下载": getattr(NotificationType, "Download", None),
                "整理入库": getattr(NotificationType, "Organize", getattr(NotificationType, "Library", None)),
                "订阅": getattr(NotificationType, "Subscribe", None),
                "站点": getattr(NotificationType, "SiteMessage", None),
                "媒体服务器": getattr(NotificationType, "MediaServer", None),
                "手动处理": getattr(NotificationType, "Manual", None),
                "插件": getattr(NotificationType, "Plugin", None),
                "其它": getattr(NotificationType, "Other", None),
                "其他": getattr(NotificationType, "Other", None)
            }

        if self._enabled:
            self._add_log(f"🚀 插件启动，正在初始化异步全兼容消息路由系统...")
            self._patch_plugin_base()    
            self._patch_event_bus()      
            self._patch_message_utils()  

    def _get_system_wechat_apps(self) -> Dict[str, Dict[str, Any]]:
        """读取系统内已配置的企业微信通知通道"""
        now = time.time()
        if now - self._apps_profile_last_update < 30 and self._apps_profile_cache:
            return self._apps_profile_cache
        apps = {}
        try:
            if not SystemConfigOper or not SystemConfigKey:
                return apps
            notifies = SystemConfigOper().get(SystemConfigKey.Notifications) or []
            
            for conf in notifies:
                conf_dict = conf.dict() if hasattr(conf, "dict") else (conf.model_dump() if hasattr(conf, "model_dump") else (conf if isinstance(conf, dict) else vars(conf)))
                c_type = str(conf_dict.get("type", "")).lower()
                c_name = str(conf_dict.get("name", ""))
                
                if "wechat" in c_type or "wecom" in c_type:
                    config_data = conf_dict.get("config") or conf_dict
                    if isinstance(config_data, str):
                        try: config_data = json.loads(config_data)
                        except: config_data = {}
                            
                    flat_config = {str(k).lower().replace('_', ''): v for k, v in config_data.items()}
                    corpid = config_data.get("WECHAT_CORPID") or flat_config.get("corpid") or flat_config.get("wechatcorpid")
                    secret = config_data.get("WECHAT_APP_SECRET") or flat_config.get("wechatappsecret") or flat_config.get("corpsecret") or flat_config.get("secret")
                    agentid = config_data.get("WECHAT_APP_ID") or flat_config.get("wechatappid") or flat_config.get("agentid") or flat_config.get("appid")
                    
                    if corpid and secret and str(agentid).strip() != "":
                        apps[c_name] = {"corpid": str(corpid), "secret": str(secret), "appid": int(agentid) if str(agentid).isdigit() else str(agentid), "proxy": config_data.get("WECHAT_PROXY") or flat_config.get("wechatproxy") or flat_config.get("proxy") or "https://qyapi.weixin.qq.com"}
            
            self._apps_profile_cache = apps
            self._apps_profile_last_update = now
        except Exception as e: pass
        return apps

    def get_state(self) -> bool:
        """获取插件启用状态"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册插件命令"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API"""
        return [
            {
                "path": "/config",
                "endpoint": self._api_get_config,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取插件配置"
            },
            {
                "path": "/config",
                "endpoint": self._api_save_config,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "保存插件配置"
            },
            {
                "path": "/overview",
                "endpoint": self._api_get_overview,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取插件概览"
            },
            {
                "path": "/logs",
                "endpoint": self._api_get_logs,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取拦截日志"
            },
            {
                "path": "/status",
                "endpoint": self._api_get_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取状态摘要"
            },
            {
                "path": "/options",
                "endpoint": self._api_get_options,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取路由规则选项"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件服务"""
        return []

    def get_render_mode(self) -> Tuple[str, str]:
        """返回 Vue 联邦渲染模式"""
        return "vue", "dist/assets"

    def _add_log(self, msg: str):
        """写入拦截日志，仅保留最近 50 条"""
        if not hasattr(self, '_intercept_logs'): self._intercept_logs = []
        now = time.strftime("%H:%M:%S", time.localtime())
        self._intercept_logs.insert(0, f"[{now}] {msg}")
        if len(self._intercept_logs) > 50: self._intercept_logs.pop()

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """Vue 模式下返回 None 和初始配置"""
        return None, self._api_get_config()

    def get_page(self) -> List[dict]:
        """Vue 模式下返回空页面定义"""
        return []

    def __get_access_token(self, app_alias: str, current_apps: dict, force: bool = False) -> str:
        app_info = current_apps.get(app_alias)
        if not app_info: return ""
        now_ts = int(time.time())
        if not force and app_alias in self._tokens_cache and self._tokens_cache[app_alias]["expires_at"] > now_ts + 60:
            return self._tokens_cache[app_alias]["access_token"]
        try:
            res = requests.get(self._token_url % (app_info.get("proxy").rstrip('/'), app_info.get("corpid"), app_info.get("secret")), timeout=15)
            if res.status_code == 200 and res.json().get('errcode') == 0:
                self._tokens_cache[app_alias] = {"access_token": res.json().get('access_token'), "expires_at": now_ts + res.json().get('expires_in', 7200)}
                self.save_data("wecom_tokens", self._tokens_cache)
                return self._tokens_cache[app_alias]["access_token"]
        except: pass
        return ""

    def __send_wechat_msg(self, app_alias: str, current_apps: dict, title: str, text: str, image_url: str, userid: str, retry: int = 0):
        app_info = current_apps.get(app_alias)
        token = self.__get_access_token(app_alias, current_apps, force=(retry > 0))
        if not app_info or not token: return

        title_safe = str(title).strip() if title else "系统通知"
        text_safe = str(text).replace("\n\n", "\n").replace('&&', '').strip() if text else ""
        image_url = image_url if image_url and str(image_url).startswith('http') else ""

        req_json = {"touser": userid if userid else "@all", "agentid": app_info.get("appid"), "msgtype": "news" if image_url else "text"}
        if image_url: req_json["news"] = {"articles": [{"title": title_safe, "description": text_safe, "picurl": image_url, "url": ''}]}
        else: req_json["text"] = {"content": f"{title_safe}\n{text_safe}".strip()}

        try:
            res = requests.post(self._send_msg_url % (app_info.get("proxy").rstrip('/'), token), json=req_json, timeout=15)
            if res.status_code == 200:
                if res.json().get('errcode') == 0: self._add_log(f"✅ 直推成功 | 目标: [{app_alias}] | 标题: {title_safe[:20]}...")
                elif res.json().get('errcode') in [42001, 40014] and retry < 2: self.__send_wechat_msg(app_alias, current_apps, title, text, image_url, userid, retry + 1)
                else: self._add_log(f"❌ 企微报错 [{app_alias}]: {res.json().get('errmsg')}")
        except Exception as e: pass

    def _extract_msg_args(self, *args, **kwargs) -> dict:
        result = {"title": "", "text": "", "image": "", "userid": None, "plugin_id": ""}
        def _extract_from_obj(obj):
            try:
                if hasattr(obj, 'title') and hasattr(obj, 'text'):
                    result['title'] = getattr(obj, 'title', '') or ''
                    result['text'] = getattr(obj, 'text', '') or ''
                    result['image'] = getattr(obj, 'image', '') or ''
                    result['userid'] = getattr(obj, 'userid', None)
                    result['plugin_id'] = getattr(obj, 'plugin_id', getattr(obj, 'source', '')) or ''
                    return True
                elif isinstance(obj, dict):
                    msg = obj.get('message')
                    if msg and hasattr(msg, 'title'):
                        result['title'] = getattr(msg, 'title', '') or ''
                        result['text'] = getattr(msg, 'text', '') or ''
                        result['image'] = getattr(msg, 'image', '') or ''
                        result['userid'] = getattr(msg, 'userid', None)
                        result['plugin_id'] = getattr(msg, 'plugin_id', getattr(msg, 'source', '')) or ''
                        return True
                    elif 'title' in obj or 'text' in obj:
                        result['title'] = obj.get('title', '') or ''
                        result['text'] = obj.get('text', '') or ''
                        result['image'] = obj.get('image', '') or ''
                        result['userid'] = obj.get('userid', None)
                        result['plugin_id'] = obj.get('plugin_id', obj.get('source', '')) or ''
                        return True
            except: pass
            return False

        for arg in args:
            if _extract_from_obj(arg): return result
        for v in kwargs.values():
            if _extract_from_obj(v): return result

        result['title'] = kwargs.get('title', '') or ''
        result['text'] = kwargs.get('text', '') or ''
        result['image'] = kwargs.get('image', '') or ''
        result['userid'] = kwargs.get('userid', None)
        result['plugin_id'] = kwargs.get('plugin_id', kwargs.get('source', '')) or ''

        if not result['title'] and args:
            str_args = [a for a in args if isinstance(a, str)]
            if len(str_args) > 0: result['title'] = str_args[0]
            if len(str_args) > 1: result['text'] = str_args[1]
        return result

    def _process_intercept(self, msg_data: dict, args, kwargs, layer_name: str):
        plugin_id_lower = str(msg_data.get('plugin_id') or "").strip().lower()
        title_lower = str(msg_data.get('title') or "").lower()
        text_lower = str(msg_data.get('text') or "").lower()
        
        routes_lower = {k.lower(): v for k, v in self._plugin_routes.items()}
        matched_route_key = None

        if plugin_id_lower and plugin_id_lower in routes_lower:
            matched_route_key = plugin_id_lower
        else:
            for k in routes_lower.keys():
                if (plugin_id_lower and k in plugin_id_lower) or (k in title_lower) or (k in text_lower):
                    matched_route_key = k
                    break

        if matched_route_key:
            route_info = routes_lower[matched_route_key]
            target_app_alias = route_info.get("app", "")
            target_type_str = route_info.get("type", "")

            # 去重：15秒内同标题消息不再重复触发直推和修改
            msg_hash = hash(f"{msg_data['title']}_{msg_data['text']}")
            now = time.time()
            if not hasattr(self, '_pushed_msg_cache'): self._pushed_msg_cache = {}
            self._pushed_msg_cache = {k: v for k, v in self._pushed_msg_cache.items() if now - v < 15}
            already_processed = msg_hash in self._pushed_msg_cache

            current_apps = self._get_system_wechat_apps()
            
            action_taken = False
            is_direct_pushed = False

            # 【通道一】：企微直推
            if target_app_alias and target_app_alias in current_apps:
                is_direct_pushed = True
                if not already_processed:
                    self.__send_wechat_msg(app_alias=target_app_alias, current_apps=current_apps, title=msg_data['title'], text=msg_data['text'], image_url=msg_data['image'], userid=msg_data['userid'])
                    self._add_log(f"🛡️ 命中规则 [{matched_route_key}] ➔ 已接管通知 ({layer_name})")
                    action_taken = True

            # 【通道二】：修改消息类型 (深度递归修改)
            if target_type_str and target_type_str not in ["", "原类型", "不修改"]:
                target_mtype = self._type_map.get(target_type_str)
                if target_mtype is not None and not already_processed:
                    def _change_mtype(obj):
                        changed = False
                        try:
                            if hasattr(obj, 'mtype'): 
                                setattr(obj, 'mtype', target_mtype)
                                changed = True
                        except: pass
                        try:
                            if isinstance(obj, dict):
                                if 'mtype' in obj: 
                                    obj['mtype'] = target_mtype
                                    changed = True
                                if 'message' in obj:
                                    if _change_mtype(obj['message']):
                                        changed = True
                        except: pass
                        return changed

                    type_changed = False
                    for a in args: 
                        if _change_mtype(a): type_changed = True
                    for v in kwargs.values(): 
                        if _change_mtype(v): type_changed = True
                    if 'mtype' in kwargs: 
                        kwargs['mtype'] = target_mtype
                        type_changed = True

                    if type_changed:
                        self._add_log(f"✅ [{layer_name}] 消息类型已修改为 ➔ [{target_type_str}]")
                        action_taken = True

            # 记录缓存状态
            if action_taken and not already_processed:
                self._pushed_msg_cache[msg_hash] = now

            # 【通道三】：拦截原通知 (仅在开启阻断开关且配置了直推时生效)
            if self._block_system and is_direct_pushed:
                if not already_processed:
                    self._add_log(f"🛡️ 阻断广播 | 原消息已被拦截并重置，阻止系统继续推送")
                
                # 物理清空源参数内存
                def _destroy(obj):
                    try:
                        if hasattr(obj, 'title'): setattr(obj, 'title', '')
                        if hasattr(obj, 'text'): setattr(obj, 'text', '')
                        if hasattr(obj, 'targets'): setattr(obj, 'targets', {})
                        if hasattr(obj, 'userid'): setattr(obj, 'userid', None)
                        if hasattr(obj, 'channel'): setattr(obj, 'channel', None)
                    except: pass
                    try:
                        if isinstance(obj, dict):
                            if 'title' in obj: obj['title'] = ''
                            if 'text' in obj: obj['text'] = ''
                            if 'message' in obj: _destroy(obj['message'])
                    except: pass
                
                for a in args: _destroy(a)
                for v in kwargs.values(): _destroy(v)

                return True # 返回 True 彻底阻断底层的后续广播

        return False # 不阻断，带着修改后的类型放行给系统

    def _patch_plugin_base(self):
        try:
            if hasattr(_PluginBase, 'original_post_message'): return
            _PluginBase.original_post_message = _PluginBase.post_message
            def hooked_post_message(plugin_self, *args, **kwargs):
                msg_data = self._extract_msg_args(*args, **kwargs)
                if not msg_data['plugin_id']:
                    p_name = getattr(plugin_self, 'plugin_name', plugin_self.__class__.__name__)
                    p_module = getattr(plugin_self, '__module__', '')
                    msg_data['plugin_id'] = f"{p_name} {p_module}"
                if self._process_intercept(msg_data, args, kwargs, "常规通道"): return True
                return _PluginBase.original_post_message(plugin_self, *args, **kwargs)
            _PluginBase.post_message = hooked_post_message
        except: pass

    def _patch_event_bus(self):
        try:
            if not eventmanager:
                return
            publish_method_name = next((m for m in ['send_event', 'publish_event', 'publish'] if hasattr(eventmanager, m)), None)
            if not publish_method_name or hasattr(eventmanager, 'original_publish_event_router'): return
            original_publish = getattr(eventmanager, publish_method_name)
            setattr(eventmanager, 'original_publish_event_router', original_publish)

            if asyncio.iscoroutinefunction(original_publish):
                async def hooked_publish_event(*args, **kwargs):
                    try:
                        msg_data = self._extract_msg_args(*args, **kwargs)
                        if msg_data['title'] or msg_data['text']:
                            if self._process_intercept(msg_data, args, kwargs, "异步事件总线"):
                                return True
                    except:
                        pass
                    return await getattr(eventmanager, 'original_publish_event_router')(*args, **kwargs)

                setattr(eventmanager, publish_method_name, hooked_publish_event)
            else:
                def hooked_publish_event(*args, **kwargs):
                    try:
                        msg_data = self._extract_msg_args(*args, **kwargs)
                        if msg_data['title'] or msg_data['text']:
                            if self._process_intercept(msg_data, args, kwargs, "事件总线"):
                                return True
                    except:
                        pass
                    return getattr(eventmanager, 'original_publish_event_router')(*args, **kwargs)

                setattr(eventmanager, publish_method_name, hooked_publish_event)
        except: pass

    def _patch_message_utils(self):
        hook_count = 0

        class_candidates = [
            ("app.chain.message", ["MessageChain"]),
            ("app.modules.message", ["MessageModule", "Message"]), 
            ("app.utils.message", ["Message", "MessageUtils"]),
            ("app.helper.message", ["MessageHelper", "Message"]), 
            ("app.core.message", ["Message", "MessageManager"]),
            ("app.core.notification", ["Notification"]),
            ("app.helper.notification", ["NotificationHelper"]),
            ("app.modules.telegram", ["TelegramModule", "Telegram"]),
            ("app.modules.wechat", ["WechatModule", "Wechat"]),
            ("app.modules.bark", ["BarkModule", "Bark"])
        ]
        methods_to_find = ['process', 'add_message', 'send_message', 'send_msg', 'send', 'post_message', 'notify', 'put']

        for mod_name, class_names in class_candidates:
            try:
                mod = importlib.import_module(mod_name)
                for cls_name in class_names:
                    if hasattr(mod, cls_name):
                        target_class = getattr(mod, cls_name)
                        for method_name in methods_to_find:
                            if hasattr(target_class, method_name) and callable(getattr(target_class, method_name)):
                                self._apply_deep_hook(target_class, method_name, mod_name, is_module=False)
                                hook_count += 1
            except Exception:
                pass

        for mod_name, mod in list(sys.modules.items()):
            if not (mod_name.startswith('app.') or mod_name.startswith('plugins.')): continue
            if 'messagerouter' in mod_name.lower(): continue
            
            for func_name in ['post_message', 'send_message', 'send_msg']:
                if hasattr(mod, func_name) and callable(getattr(mod, func_name)):
                    self._apply_deep_hook(mod, func_name, mod_name, is_module=True)
                    hook_count += 1

        self._add_log(f"✅ 底层路由开启：已成功挂载 {hook_count} 个系统与插件发信枢纽")

    def _apply_deep_hook(self, target_obj, method_name, mod_name, is_module=False):
        hook_attr_name = f"_original_{method_name}_router"
        if hasattr(target_obj, hook_attr_name): return
        
        original_method = getattr(target_obj, method_name)
        setattr(target_obj, hook_attr_name, original_method)
        
        if not hasattr(self, '_active_hooks'): self._active_hooks = []
        self._active_hooks.append((target_obj, method_name, hook_attr_name))
        
        if asyncio.iscoroutinefunction(original_method):
            async def hooked_send_msg_async(*args, **kwargs):
                try:
                    msg_data = self._extract_msg_args(*args, **kwargs)
                    if msg_data['title'] or msg_data['text']:
                        display_name = f"{mod_name}.{method_name}" if is_module else f"{target_obj.__name__}.{method_name}"
                        if self._process_intercept(msg_data, args, kwargs, f"底层异步模块: {display_name}"):
                            return True
                except:
                    pass
                return await getattr(target_obj, hook_attr_name)(*args, **kwargs)

            setattr(target_obj, method_name, hooked_send_msg_async)
        else:
            def hooked_send_msg_sync(*args, **kwargs):
                try:
                    msg_data = self._extract_msg_args(*args, **kwargs)
                    if msg_data['title'] or msg_data['text']:
                        display_name = f"{mod_name}.{method_name}" if is_module else f"{target_obj.__name__}.{method_name}"
                        if self._process_intercept(msg_data, args, kwargs, f"底层模块: {display_name}"):
                            return True
                except:
                    pass
                return getattr(target_obj, hook_attr_name)(*args, **kwargs)

            setattr(target_obj, method_name, hooked_send_msg_sync)

    def stop_service(self):
        try:
            if hasattr(_PluginBase, 'original_post_message'):
                _PluginBase.post_message = _PluginBase.original_post_message
                delattr(_PluginBase, 'original_post_message')
        except: pass
        try:
            if eventmanager and hasattr(eventmanager, 'original_publish_event_router'):
                for method in ['send_event', 'publish_event', 'publish']:
                    if hasattr(eventmanager, method):
                        setattr(eventmanager, method, eventmanager.original_publish_event_router)
                        break
                delattr(eventmanager, 'original_publish_event_router')
        except: pass
        try:
            if hasattr(self, '_active_hooks'):
                for target_obj, method_name, hook_attr_name in self._active_hooks:
                    if hasattr(target_obj, hook_attr_name):
                        setattr(target_obj, method_name, getattr(target_obj, hook_attr_name))
                        delattr(target_obj, hook_attr_name)
                self._active_hooks = []
        except: pass
        self._add_log("🛑 插件已停用，所有拦截路由已安全撤销。")

    def _api_get_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "block_system": self._block_system,
            "plugin_mapping": str(self._plugin_mapping_str or ""),
            "route_rules": self._get_route_rules()
        }

    def _api_save_config(self, payload: dict = None) -> Dict[str, Any]:
        payload = payload or {}
        try:
            route_rules = self._normalize_route_rules(payload.get("route_rules"))
            plugin_mapping = payload.get("plugin_mapping", self._plugin_mapping_str or "")
            if route_rules:
                plugin_mapping = self._serialize_plugin_routes(route_rules)
            new_config = {
                "enabled": self._to_bool(payload.get("enabled", self._enabled)),
                "block_system": self._to_bool(payload.get("block_system", self._block_system)),
                "plugin_mapping": str(plugin_mapping or "")
            }
            self.init_plugin(new_config)
            self.update_config(new_config)
            return {"success": True, "msg": "配置已保存", "config": self._api_get_config()}
        except Exception as e:
            logger.error(f"{self.plugin_name}: 保存配置失败: {e}")
            return {"success": False, "msg": str(e)}

    def _api_get_overview(self) -> Dict[str, Any]:
        return self._build_overview()

    def _api_get_logs(self) -> Dict[str, Any]:
        return {"logs": list(getattr(self, '_intercept_logs', []) or [])}

    def _api_get_status(self) -> Dict[str, Any]:
        overview = self._build_overview()
        return {
            "enabled": overview.get("enabled", False),
            "block_system": overview.get("block_system", False),
            "rule_count": overview.get("rule_count", 0),
            "wechat_app_count": overview.get("wechat_app_count", 0),
            "hook_count": overview.get("hook_count", 0),
            "latest_log": overview.get("logs", [None])[0],
        }

    def _api_get_options(self) -> Dict[str, Any]:
        """获取配置页下拉选项"""
        return {
            "plugins": self._get_plugin_options(),
            "notification_types": self._get_notification_type_options(),
            "wechat_apps": self._get_wechat_app_options(),
        }
